from __future__ import annotations

import argparse
import importlib
import os
import sys

from . import gh
from .context import Context, halted
from .decisions import DecisionLog
from .jev import BudgetExceeded, client_from_env

BOTS = {
    "changelog-drafter": ("akbot.bots.changelog_drafter", ["run_id"], 15),
    "red-run-classifier": ("akbot.bots.red_run_classifier", ["since_hours", "limit"], 10),
    "issue-triage": ("akbot.bots.issue_triage", ["limit"], 30),
    "base-image-bump": ("akbot.bots.base_image_bump", [], 2),
    "pr-review-router": ("akbot.bots.pr_review_router", ["limit"], 20),
    "preflight-audit": ("akbot.bots.preflight_audit", ["limit"], 3),
}
TOOLS = {"watchdog": "akbot.watchdog", "undo": "akbot.undo"}

# The repos a bot may write to. The App token is scoped to these in the
# workflows as well; this is the second lock on the same door, for a local run
# or a dispatch that types a different repo name.
ALLOWED_REPOS = set(filter(None, os.environ.get(
    "AK_BOT_ALLOWED_REPOS", "artifact-keeper/artifact-keeper,artifact-keeper/trivy").split(",")))
OWN_REPO = os.environ.get("AK_BOT_OWN_REPO", "artifact-keeper/ak-bot")


def env_int(name: str) -> int:
    """An unset workflow variable arrives as an empty string, not as absence."""
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="akbot", description="JEV-powered decision bots for artifact-keeper")
    p.add_argument("bot", choices=sorted(BOTS) + sorted(TOOLS))
    p.add_argument("--repo", default="artifact-keeper/artifact-keeper")
    p.add_argument("--dry-run", action="store_true", help="read everything, write nothing; uses FakeJev without a key")
    p.add_argument("--run-id", type=int, default=None)
    p.add_argument("--since-hours", type=float, default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-jev-calls", type=int, default=None, help="per-run cap (default per bot, or AK_BOT_MAX_JEV_CALLS)")
    a = p.parse_args(argv)

    if a.bot in TOOLS:
        mod = importlib.import_module(TOOLS[a.bot])
        return mod.main(a)

    if a.repo not in ALLOWED_REPOS:
        print(f"::error::{a.repo} is not in AK_BOT_ALLOWED_REPOS ({', '.join(sorted(ALLOWED_REPOS))}); refusing")
        return 2
    if halted():
        # The kill switch stops writes AND JEV spend: nothing runs at all.
        print(f"::warning title=ak-bot halted::AK_BOT_HALT is set ({halted()}); {a.bot} did not run")
        return 0

    modname, params, default_cap = BOTS[a.bot]
    mod = importlib.import_module(modname)
    cap = a.max_jev_calls or env_int("AK_BOT_MAX_JEV_CALLS") or default_cap
    jev = client_from_env(a.dry_run, max_calls=cap)
    log = DecisionLog(a.bot, a.dry_run)
    ctx = Context(repo=a.repo, jev=jev, dry_run=a.dry_run, log=log)
    print(f"akbot {a.bot} repo={a.repo} model={jev.name} dry_run={a.dry_run} max_jev_calls={cap}")
    if jev.name.startswith("fake:"):
        print("::notice::no TYPESAFE_API_KEY: the fake model answers and every decision lands in skip")
    kwargs = {k: getattr(a, k) for k in params if getattr(a, k) is not None}
    rc = 0
    try:
        rc = mod.run(ctx, **kwargs)
    except BudgetExceeded as e:
        print(f"::warning title=JEV budget::{e}; remaining subjects wait for the next run")
    finally:
        log.run_meta = {"model": jev.model or jev.name, **jev.summary()}
        if jev.missing_field_answers:
            print(f"::warning title=JEV answers::{jev.missing_field_answers} answer(s) lacked a probability or "
                  f"confidence field and were routed to skip")
        log.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
