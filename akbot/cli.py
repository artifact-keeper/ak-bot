from __future__ import annotations

import argparse
import sys

from .context import Context
from .decisions import DecisionLog
from .jev import client_from_env

BOTS = {
    "changelog-drafter": ("akbot.bots.changelog_drafter", ["run_id"]),
    "red-run-classifier": ("akbot.bots.red_run_classifier", ["since_hours", "limit"]),
    "issue-triage": ("akbot.bots.issue_triage", ["limit"]),
    "base-image-bump": ("akbot.bots.base_image_bump", []),
    "pr-review-router": ("akbot.bots.pr_review_router", ["limit"]),
    "preflight-audit": ("akbot.bots.preflight_audit", ["limit"]),
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="akbot", description="JEV-powered decision bots for artifact-keeper")
    p.add_argument("bot", choices=sorted(BOTS))
    p.add_argument("--repo", default="artifact-keeper/artifact-keeper")
    p.add_argument("--dry-run", action="store_true", help="read everything, write nothing; uses FakeJev without a key")
    p.add_argument("--run-id", type=int, default=None)
    p.add_argument("--since-hours", type=float, default=None)
    p.add_argument("--limit", type=int, default=None)
    a = p.parse_args(argv)

    import importlib
    modname, params = BOTS[a.bot]
    mod = importlib.import_module(modname)
    ctx = Context(repo=a.repo, jev=client_from_env(a.dry_run), dry_run=a.dry_run, log=DecisionLog(a.bot, a.dry_run))
    print(f"akbot {a.bot} repo={a.repo} model={ctx.jev.name} dry_run={a.dry_run}")
    kwargs = {k: getattr(a, k) for k in params if getattr(a, k) is not None}
    try:
        return mod.run(ctx, **kwargs)
    finally:
        ctx.log.flush()


if __name__ == "__main__":
    sys.exit(main())
