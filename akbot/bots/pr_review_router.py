"""Bot 5: route external contributor PRs.

Advisory only. At the auto tier a PR judged ready gets the
`needs-maintainer-review` label and a one-line comment; otherwise the single
most likely blocker is named in a comment. Re-evaluated on every new head
commit (the ledger marker carries the head sha). Never merges, closes,
approves or requests changes.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .. import gh
from ..context import Context
from ..decisions import LABEL, COMMENT, AUTO, SUGGEST, SKIP, redact
from ..jev import choice, score

BOT = "pr-review-router"
QUESTIONS_VERSION = "pr-review-router/q1"
READY_LABEL = "needs-maintainer-review"
LINK_RE = re.compile(r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*(?:[\w.-]+/[\w.-]+)?#\d+", re.I)
TEST_PATH_RE = re.compile(r"(^|/)(tests?|spec|__tests__|e2e)(/|$)|_test\.|\.test\.|\.spec\.")
BLOCKER_TEXT = {
    "missing_linked_issue": "the PR body needs a `Closes #N` line (the Require Linked Issue gate)",
    "ci_red": "CI is red on the current head",
    "no_tests": "the change has no test coverage for the behaviour it changes",
    "too_large": "the diff mixes unrelated concerns and would review faster split up",
    "merge_conflict": "the branch conflicts with main",
    "unclear_description": "the description does not say what was wrong, why, and what changed",
}


def questions() -> dict[str, dict]:
    return {
        "readiness": score(
            "How ready is this external pull request for a maintainer's review?",
            ["not reviewable: no description, broken, or clearly unfinished",
             "needs work first: a blocker the contributor can fix without a maintainer",
             "nearly ready: a small gap a maintainer can note during review",
             "ready for maintainer review"],
        ),
        "blocker": choice(
            "What is the single most important thing standing between this PR and a review? Pick none when nothing is.",
            {**{k: v for k, v in BLOCKER_TEXT.items()}, "none": "nothing blocks a review"},
        ),
        "risk": score(
            "How risky is the change to ship?",
            ["docs or comments only", "low: isolated change with tests",
             "medium: touches a format handler, proxy or storage path",
             "high: touches auth, tokens, security scanning, migrations, storage integrity or release tooling"],
        ),
    }


def checks_summary(pr: dict) -> list[str]:
    out = []
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("context") or "?"
        out.append(f"{name}={c.get('conclusion') or c.get('state') or '?'}")
    return sorted(set(out))


def build_state(ctx: Context, pr: dict) -> dict:
    files = [f["path"] for f in pr.get("files") or []]
    body = pr.get("body") or ""
    created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
    return {
        "pull_request": {
            "number": pr["number"], "title": pr["title"], "body": redact(body[:4000]),
            "author": (pr.get("author") or {}).get("login"), "labels": [l["name"] for l in pr.get("labels") or []],
            "additions": pr.get("additions"), "deletions": pr.get("deletions"), "changed_files": files[:80],
            "days_open": (datetime.now(timezone.utc) - created).days,
            "has_linked_issue": bool(LINK_RE.search(re.sub(r"<!--.*?-->", "", body, flags=re.S))),
            "touches_tests": any(TEST_PATH_RE.search(p) for p in files),
            "checks": checks_summary(pr), "review_decision": pr.get("reviewDecision") or "none",
            "mergeable": pr.get("mergeable"),
        },
        "diff_excerpt": redact(gh.pr_diff(ctx.repo, pr["number"], max_chars=8000)),
    }


def act(ctx: Context, pr: dict, res) -> None:
    n = pr["number"]
    ready, blocker, risk = res["readiness"], res["blocker"], res["risk"]
    marker = f"pr-router-{n}-{pr['headRefOid'][:10]}"
    labels = gh.repo_labels(ctx.repo)
    risk_txt = f"risk {risk.score:.1f}/3"
    if (ready.score or 0) >= 2.5 and blocker.choice == "none":
        tier = LABEL.tier(min(ready.certainty, blocker.certainty))
        if tier == AUTO:
            acts = []
            if READY_LABEL in labels and READY_LABEL not in [l["name"] for l in pr.get("labels") or []]:
                acts.append(ctx.act(f"label {READY_LABEL}", gh.add_labels, ctx.repo, n, [READY_LABEL]))
            acts.append(ctx.act("comment ready", gh.comment, ctx.repo, n,
                                f"ak-bot: this looks ready for maintainer review ({risk_txt}, "
                                f"readiness {ready.score:.1f}/3 at confidence {ready.certainty:.2f})."))
            action = "; ".join(acts)
        else:
            action = "ledger only"
    elif blocker.choice and blocker.choice != "none":
        tier = COMMENT.tier(blocker.certainty)
        if tier in (AUTO, SUGGEST):
            hedge = "" if tier == AUTO else "possibly "
            action = ctx.act("comment blocker", gh.comment, ctx.repo, n,
                             f"ak-bot: before a maintainer review, {hedge}{BLOCKER_TEXT[blocker.choice]} "
                             f"(confidence {blocker.certainty:.2f}; {risk_txt}). Push a new commit and this is re-evaluated.")
        else:
            action = "ledger only"
    else:
        tier, action = SKIP, "ledger only"
    ctx.log.add(f"PR #{n}", QUESTIONS_VERSION, res.model, res.answers, tier, action, url=pr.get("url", ""))
    ctx.ledger.record(marker, f"pr-review-router: #{n}@{pr['headRefOid'][:10]}: {tier} -> {action}")


def run(ctx: Context, limit: int = 20) -> int:
    members = gh.org_members(ctx.org)
    todo = []
    for pr in gh.list_prs(ctx.repo):
        author = (pr.get("author") or {}).get("login", "")
        if pr.get("isDraft") or author in members or author.endswith("[bot]") or author == "app/dependabot":
            continue
        if ctx.ledger.has(f"pr-router-{pr['number']}-{pr['headRefOid'][:10]}"):
            continue
        todo.append(pr)
    for pr in todo[:limit]:
        act(ctx, pr, ctx.jev.ask(build_state(ctx, pr), questions()))
    if not todo:
        print("no external PRs to route")
    return 0
