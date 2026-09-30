"""Confidence routing and the decision record.

Three tiers, per TypeSafe's confidence-routing pattern:
    auto     certainty >= auto_at     act without asking
    suggest  certainty >= suggest_at  propose (comment / draft) and let a human decide
    skip     below                    record only

Thresholds are per action, because a label costs nothing to undo and a
rerun costs cluster time. Tune them from the decision records: bin by stated
probability, compare with what the human did, move the line.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from .jev import Answer

AUTO, SUGGEST, SKIP = "auto", "suggest", "skip"


@dataclass(frozen=True)
class Thresholds:
    auto_at: float = 0.85
    suggest_at: float = 0.60

    def tier(self, certainty: float) -> str:
        if certainty >= self.auto_at:
            return AUTO
        if certainty >= self.suggest_at:
            return SUGGEST
        return SKIP


# Stakes-ordered defaults. Reads and labels are cheap; anything that spends
# compute or opens issues needs more certainty; nothing here merges or closes.
LABEL = Thresholds(0.85, 0.60)
COMMENT = Thresholds(0.80, 0.55)
DRAFT_PR = Thresholds(0.85, 0.60)
OPEN_ISSUE = Thresholds(0.85, 0.65)
RERUN = Thresholds(0.90, 0.75)

SECRET_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|"
    r"(?i:bearer\s+)[A-Za-z0-9._\-]{16,}|(?i:(api[_-]?key|token|secret|password)\s*[:=]\s*)\S+)"
)


def redact(text: str) -> str:
    return SECRET_RE.sub("[redacted]", text or "")


@dataclass
class Decision:
    bot: str
    subject: str            # e.g. "PR #4006", "run 36686044764", "issue #4335"
    questions_version: str
    model: str
    answers: dict[str, Any]
    tier: str
    action: str             # what we did (or would do in dry-run)
    dry_run: bool
    ts: float = field(default_factory=time.time)
    url: str = ""

    @staticmethod
    def summarize(a: Answer) -> dict[str, Any]:
        d: dict[str, Any] = {"type": a.type, "certainty": round(a.certainty, 3)}
        if a.type == "choice":
            d["choice"] = a.choice
            d["probabilities"] = {k: round(v, 3) for k, v in a.probabilities.items()}
        elif a.type == "score":
            d["score"] = a.score
            d["probabilities"] = {k: round(v, 3) for k, v in a.probabilities.items()}
        else:
            d["noul"] = round(a.noul or 0.0, 3)
        return d


class DecisionLog:
    def __init__(self, bot: str, dry_run: bool):
        self.bot = bot
        self.dry_run = dry_run
        self.records: list[Decision] = []

    def add(self, subject: str, questions_version: str, model: str, answers: dict[str, Answer],
            tier: str, action: str, url: str = "") -> Decision:
        d = Decision(self.bot, subject, questions_version, model,
                     {k: Decision.summarize(v) for k, v in answers.items()}, tier, action, self.dry_run, url=url)
        self.records.append(d)
        print(f"[{self.bot}] {subject}: {tier} -> {action}")
        return d

    def note(self, subject: str, action: str) -> None:
        """A deterministic (non-JEV) decision, kept in the same record."""
        self.records.append(Decision(self.bot, subject, "n/a", "deterministic", {}, "auto", action, self.dry_run))
        print(f"[{self.bot}] {subject}: {action}")

    def to_jsonl(self) -> str:
        return "".join(json.dumps(asdict(r), sort_keys=True) + "\n" for r in self.records)

    def to_markdown(self) -> str:
        head = f"## ak-bot `{self.bot}`{' (dry run)' if self.dry_run else ''}\n\n"
        if not self.records:
            return head + "Nothing to decide.\n"
        rows = ["| subject | tier | action | answers |", "|---|---|---|---|"]
        for r in self.records:
            ans = "; ".join(
                f"{k}={v.get('choice', v.get('score', v.get('noul')))} ({v['certainty']})"
                for k, v in r.answers.items()
            )
            subj = f"[{r.subject}]({r.url})" if r.url else r.subject
            rows.append(f"| {subj} | {r.tier} | {r.action} | {ans} |")
        return head + "\n".join(rows) + "\n"

    def flush(self, out_dir: str = "decisions") -> None:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{self.bot}-{int(time.time())}.jsonl")
        with open(path, "w") as fh:
            fh.write(self.to_jsonl())
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as fh:
                fh.write(self.to_markdown())
        print(self.to_markdown())
