"""Per-run context shared by every bot: target repo, model, dry-run flag,
decision log, and the ledger that makes actions idempotent."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import gh
from .decisions import DecisionLog

LEDGER_TITLE = "ak-bot ledger"
LEDGER_LABELS = ["automated"]
MARKER_RE = re.compile(r"ak-bot-id:\s*([A-Za-z0-9._-]+)")


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

    def _open_ledgers(self) -> list[dict]:
        hits = gh.issue_search(self.repo, f'"{LEDGER_TITLE}" in:title label:automated', limit=20)
        return [h for h in hits if h["title"].startswith(LEDGER_TITLE) and h["state"].lower() == "open"]

    def number(self) -> int | None:
        """This month's ledger, creating it on first live write."""
        if self._number is None:
            want = f"{LEDGER_TITLE} {month_key()}"
            for h in self._open_ledgers():
                if h["title"] == want:
                    self._number = h["number"]
                    break
        return self._number

    def seen(self) -> set[str]:
        if self._seen is None:
            self._seen = set()
            keep = {f"{LEDGER_TITLE} {month_key()}", f"{LEDGER_TITLE} {previous_month_key()}", LEDGER_TITLE}
            for h in self._open_ledgers():
                if h["title"] not in keep:
                    self._close_old(h)
                    continue
                for c in gh.issue_comments(self.repo, h["number"]):
                    self._seen.update(MARKER_RE.findall(c.get("body") or ""))
        return self._seen

    def _close_old(self, h: dict) -> None:
        if self.dry_run:
            print(f"[ledger:dry-run] would close old ledger #{h['number']} ({h['title']})")
            return
        gh.comment(self.repo, h["number"], "Rolled over: this ledger is older than last month and is no longer read.")
        gh.gh("issue", "close", "-R", self.repo, str(h["number"]))

    def has(self, marker: str) -> bool:
        return marker in self.seen()

    def record(self, marker: str, text: str) -> None:
        self.seen().add(marker)
        line = f"{text}\n\n<sub>ak-bot-id: {marker}</sub>"
        if self.dry_run:
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

    def act(self, description: str, fn, *args) -> str:
        """Run a write through `gh` unless dry-run; return what happened."""
        if self.dry_run:
            return f"would {description}"
        fn(*args)
        return description


def find_or_create_issue(ctx: "Context", title: str, labels: list[str], body: str) -> int:
    for h in gh.issue_search(ctx.repo, f'"{title}" in:title'):
        if h["title"] == title and h["state"].lower() == "open":
            return h["number"]
    url = gh.create_issue(ctx.repo, title, body, [l for l in labels if l in gh.repo_labels(ctx.repo)])
    return int(url.rstrip("/").rsplit("/", 1)[-1])
