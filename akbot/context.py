"""Per-run context shared by every bot: target repo, model, dry-run flag,
decision log, and the ledger that makes actions idempotent."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import gh
from .decisions import DecisionLog

LEDGER_TITLE = "ak-bot ledger"
LEDGER_LABELS = ["automated", "pinned"]   # `pinned` keeps the stale bot away
MARKER_RE = re.compile(r"ak-bot-id:\s*(\S+)")


class Ledger:
    """One long-lived issue whose comments record every action taken.

    A bot writes `ak-bot-id: <kind>-<id>` when it acts, and checks the set
    before acting, so reruns of a workflow never repeat a comment, a rerun or
    a PR. Reading comments is exact; GitHub search is not, and has indexing
    lag, so it is not used for this.
    """

    def __init__(self, repo: str, dry_run: bool):
        self.repo = repo
        self.dry_run = dry_run
        self._number: int | None = None
        self._seen: set[str] | None = None
        self._pending: list[str] = []

    def number(self) -> int | None:
        if self._number is None:
            hits = gh.issue_search(self.repo, f'"{LEDGER_TITLE}" in:title label:automated')
            for h in hits:
                if h["title"] == LEDGER_TITLE and h["state"].lower() == "open":
                    self._number = h["number"]
                    break
        return self._number

    def seen(self) -> set[str]:
        if self._seen is None:
            self._seen = set()
            n = self.number()
            if n:
                for c in gh.issue_comments(self.repo, n):
                    self._seen.update(MARKER_RE.findall(c.get("body") or ""))
        return self._seen

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
                self.repo, LEDGER_TITLE,
                "Audit trail for ak-bot actions. Each comment is one action with its `ak-bot-id` marker; "
                "bots read this thread to stay idempotent. Do not close.",
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
