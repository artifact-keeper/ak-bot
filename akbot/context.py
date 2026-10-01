"""Per-run context shared by every bot: target repo, model, dry-run flag,
decision log, and the ledger that makes actions idempotent."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from . import gh
from .decisions import DecisionLog

LEDGER_TITLE = "ak-bot ledger"
LEDGER_LABELS = ["automated"]
LEDGER_TITLE_RE = re.compile(r"^ak-bot ledger( \d{4}-\d{2})?$")
MARKER_RE = re.compile(r"ak-bot-id:\s*([A-Za-z0-9._-]+)")


def halted() -> str:
    """The kill switch: a non-empty AK_BOT_HALT means no writes, anywhere."""
    return os.environ.get("AK_BOT_HALT", "")


def month_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts))


def previous_month_key(ts: float | None = None) -> str:
    t = time.gmtime(ts)
    y, m = (t.tm_year, t.tm_mon - 1) if t.tm_mon > 1 else (t.tm_year - 1, 12)
    return f"{y:04d}-{m:02d}"


class Ledger:
    """Rolling monthly issues whose comments record every action taken.

    A bot writes `ak-bot-id: <kind>-<id>` when it acts and checks the set
    before acting, so reruns never repeat a comment, a rerun or a PR.

    The ledger rolls monthly: actions are written to `ak-bot ledger YYYY-MM`,
    markers are read from every OPEN ledger issue (normally this month's and
    last month's, plus the original untitled one until it is closed), and a
    ledger older than last month is closed on sight. Nothing needs history
    beyond that: triaged issues carry their labels, PRs close, red runs are
    only considered within hours, preflight audits only look at recent runs.
    Reading comments is exact; GitHub search is not, and has indexing lag.
    """

    def __init__(self, repo: str, dry_run: bool):
        self.repo = repo
        self.dry_run = dry_run
        self._number: int | None = None
        self._seen: set[str] | None = None

    def _ledgers(self) -> list[dict]:
        """Every ledger issue, open or closed, by exact title shape.

        Open ones come from the list endpoint (exact, no lag). Closed ones come
        from one search call: a human closing the month's ledger must not reset
        the bot's memory, so closed ledgers of the kept months are still read.
        """
        found: dict[int, dict] = {}
        for h in gh.api(f"repos/{self.repo}/issues?state=open&per_page=100", paginate=True) or []:
            if "pull_request" not in h and LEDGER_TITLE_RE.match(h["title"]):
                found[h["number"]] = {"number": h["number"], "title": h["title"], "state": "open"}
        try:
            for h in gh.issue_search(self.repo, f'"{LEDGER_TITLE}" in:title state:closed', limit=20):
                if LEDGER_TITLE_RE.match(h["title"]) and h["number"] not in found:
                    found[h["number"]] = {"number": h["number"], "title": h["title"], "state": "closed"}
        except gh.GhError as e:
            print(f"[ledger] closed-ledger search failed, reading open ledgers only: {e}")
        return list(found.values())

    def number(self) -> int | None:
        """This month's ledger: an open one, else a closed one reopened, else None."""
        if self._number is None:
            want = f"{LEDGER_TITLE} {month_key()}"
            mine = [h for h in self._ledgers() if h["title"] == want]
            open_ = [h for h in mine if h["state"] == "open"]
            if open_:
                self._number = open_[0]["number"]
            elif mine and not self.dry_run:
                gh.gh("issue", "reopen", "-R", self.repo, str(mine[0]["number"]))
                self._number = mine[0]["number"]
        return self._number

    def seen(self) -> set[str]:
        if self._seen is None:
            self._seen = set()
            keep = {f"{LEDGER_TITLE} {month_key()}", f"{LEDGER_TITLE} {previous_month_key()}", LEDGER_TITLE}
            for h in self._ledgers():
                if h["title"] not in keep:
                    if h["state"] == "open":
                        self._close_old(h)
                    continue
                for c in gh.issue_comments(self.repo, h["number"]):
                    self._seen.update(MARKER_RE.findall(c.get("body") or ""))
        return self._seen

    def _close_old(self, h: dict) -> None:
        if self.dry_run or halted():
            print(f"[ledger:dry-run] would close old ledger #{h['number']} ({h['title']})")
            return
        gh.comment(self.repo, h["number"], "Rolled over: this ledger is older than last month and is no longer read.")
        gh.gh("issue", "close", "-R", self.repo, str(h["number"]))

    def has(self, marker: str) -> bool:
        return marker in self.seen()

    def record(self, marker: str, text: str) -> None:
        self.seen().add(marker)
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        line = f"{text}\n\n<sub>ak-bot-id: {marker}" + (f" · ak-bot-run: {run_id}" if run_id else "") + "</sub>"
        if self.dry_run or halted():
            print(f"[ledger:dry-run] {marker}: {text.splitlines()[0][:100]}")
            return
        n = self.number()
        if n is None:
            url = gh.create_issue(
                self.repo, f"{LEDGER_TITLE} {month_key()}",
                "Audit trail for ak-bot actions this month. Each comment is one action with its `ak-bot-id` "
                "marker; bots read this and last month's ledger to stay idempotent, and close older ones. "
                "Delete a comment to make its subject eligible again.",
                [l for l in LEDGER_LABELS if l in gh.repo_labels(self.repo)],
            )
            self._number = n = int(url.rstrip("/").rsplit("/", 1)[-1])
        gh.comment(self.repo, n, line)


@dataclass
class Context:
    repo: str
    jev: object
    dry_run: bool
    log: DecisionLog
    ledger: Ledger = field(init=False)
    org: str = field(init=False)

    def __post_init__(self):
        self.org = self.repo.split("/")[0]
        self.ledger = Ledger(self.repo, self.dry_run)

    _issue_cache: dict = field(default_factory=dict, init=False, repr=False)

    def act(self, description: str, fn, *args) -> str:
        """Run a write through `gh` unless dry-run or halted; return what happened.

        Every write is also appended to the decision log as a structured
        effect (kind, args, result) so `akbot undo --run-id` can reverse it.
        If `fn` returns a string (a URL), it is appended to the description.
        """
        if self.dry_run or halted():
            return f"would {description}"
        out = fn(*args)
        self.log.pending_effects.append({
            "kind": description.split()[0], "description": description,
            "args": [a if isinstance(a, (str, int, float, list)) else str(a)[:200] for a in args],
            "result": out if isinstance(out, (str, int, list)) else None,
        })
        return f"{description} {out}" if isinstance(out, str) and out else description


def find_or_create_issue(ctx: "Context", title: str, labels: list[str], body: str) -> int:
    """Exact-title lookup on the open issue list (no search lag), memoized per
    run so a batch of digest comments lands on one issue."""
    if title in ctx._issue_cache:
        return ctx._issue_cache[title]
    for h in gh.api(f"repos/{ctx.repo}/issues?state=open&per_page=100", paginate=True) or []:
        if "pull_request" not in h and h["title"] == title:
            ctx._issue_cache[title] = h["number"]
            return h["number"]
    url = gh.create_issue(ctx.repo, title, body, [l for l in labels if l in gh.repo_labels(ctx.repo)])
    n = int(url.rstrip("/").rsplit("/", 1)[-1])
    ctx._issue_cache[title] = n
    return n
