"""Bot 3: label untriaged issues.

One JEV call per issue, four questions in parallel: type, area, priority,
regression. Labels are applied at the auto tier, proposed in one comment at
the suggest tier, and only logged below that. Priority is never applied
automatically: p0/p1 is a maintainer's call and the label carries weight.
"""
from __future__ import annotations

import re

from .. import gh
from ..context import Context
from ..decisions import LABEL, AUTO, SUGGEST, SKIP, redact
from ..jev import JevError, choice, noul, score

BOT = "issue-triage"
QUESTIONS_VERSION = "issue-triage/q1"
TYPE_LABELS = {
    "bug": "type:bug", "enhancement": "type:enhancement", "documentation": "type:documentation",
    "question": "type:question", "security": "type:security", "chore": "type:chore",
}
PRIORITY_LEVELS = [
    "p3: nice to have; nobody is blocked",
    "p2: a user is inconvenienced and a workaround exists",
    "p1: a user is blocked or data is wrong and there is no workaround",
    "p0: data loss, a security exposure, or the product is down for users",
]


def area_options(labels: set[str]) -> dict[str, str]:
    opts = {}
    for l in sorted(labels):
        if l.startswith("registry/"):
            opts[l.split("/", 1)[1]] = f"the {l.split('/', 1)[1]} package format handler or its remote/virtual proxying"
    opts.update({
        "core": "repositories, storage, auth, tokens, permissions, scanning, replication or the REST API in general",
        "web-ui": "the web frontend",
        "ci": "this repository's CI, release tooling or tests, not the product",
        "none_of_the_above": "cannot tell from the report",
    })
    return opts


def questions(labels: set[str]) -> dict[str, dict]:
    return {
        "type": choice(
            "What kind of issue is this report, judged by what the reporter is asking for?",
            {
                "bug": "something the product does is wrong, crashes, corrupts or contradicts its documentation",
                "enhancement": "a request for behaviour or capability the product does not have",
                "documentation": "the docs are wrong, missing or unclear; the product behaves as intended",
                "question": "the reporter is asking how to do something, not reporting a defect",
                "security": "a vulnerability, permission bypass, secret exposure or unsafe default",
                "chore": "maintenance of the repository itself: dependencies, CI, tooling, refactoring",
                "none_of_the_above": "spam, empty, or not about this project",
            },
        ),
        "area": choice("Which part of Artifact Keeper does this issue concern?", area_options(labels)),
        "priority": score("How urgently should a maintainer act on this report?", PRIORITY_LEVELS),
        "regression": noul("Does the reporter say or show that this worked in an earlier release?",
                           true="it worked before and broke", false="no claim that it used to work"),
    }


AREA_LABELS = {"core", "web-ui", "ci"}


def is_untriaged(issue: dict) -> bool:
    names = {l["name"] for l in issue.get("labels") or []}
    if any(n.startswith("type:") or n.startswith("registry/") or n in AREA_LABELS for n in names):
        return False          # a human or the bot has placed it; do not re-propose after ledger rollover
    if "automated" in names:
        return False
    author = issue.get("author") or {}
    login = author.get("login", "")
    if author.get("is_bot") or login.endswith("[bot]") or login.startswith("app/"):
        return False
    return True


def decide(ctx: Context, issue: dict, labels: set[str]):
    state = {
        "issue": {
            "number": issue["number"], "title": issue["title"],
            "body": redact((issue.get("body") or "")[:6000]),
            "existing_labels": [l["name"] for l in issue.get("labels") or []],
            "author": (issue.get("author") or {}).get("login"),
            "created": issue.get("createdAt"),
        },
        "project": "Artifact Keeper: a Rust artifact registry with 45+ package formats, proxy/virtual repos, "
                   "security scanning, mesh replication.",
    }
    return ctx.jev.ask(state, questions(labels))


def act(ctx: Context, issue: dict, res, labels: set[str]) -> None:
    n = issue["number"]
    typ, area, pri, reg = res["type"], res["area"], res["priority"], res["regression"]
    apply, propose = [], []

    def route(ans, label: str | None):
        if not label or label not in labels:
            return
        t = LABEL.tier(ans.certainty)
        if t == AUTO:
            apply.append(label)
        elif t == SUGGEST:
            propose.append(f"`{label}` ({ans.certainty:.2f})")

    route(typ, TYPE_LABELS.get(typ.choice or ""))
    area_label = None
    if area.choice and area.choice != "none_of_the_above":
        area_label = area.choice if area.choice in ("core", "web-ui", "ci") else f"registry/{area.choice}"
    route(area, area_label)
    if reg.yes:
        route(reg, "regression")
    # Priority from probability mass, not a rounded point estimate: propose p1
    # when the mass on {p1, p0} clears the suggest line, p0 when p0 alone holds
    # a majority. round() would turn 1.5 into p1 and 2.5 into p1 as well.
    if pri.probabilities:
        high = pri.mass_at_least(2)
        if high >= LABEL.suggest_at:
            level = 3 if pri.mass_at_least(3) >= 0.5 else 2
            propose.append(f"`priority:p{3 - level}` (mass {high:.2f}, never auto-applied)")
    elif pri.score is not None and pri.certainty >= LABEL.suggest_at and pri.score >= 2:
        propose.append(f"`priority:p{3 - min(3, int(pri.score + 0.5))}` ({pri.certainty:.2f}, never auto-applied)")

    tier = AUTO if apply else (SUGGEST if propose else SKIP)
    actions = []
    if apply:
        actions.append(ctx.act(f"label {', '.join(apply)}", gh.add_labels, ctx.repo, n, apply))
    # A comment goes to the reporter's inbox. Post one only when a type or
    # area label is proposed; a priority-only proposal is maintainer metadata
    # and lives in the ledger and the decision record instead.
    label_props = [x for x in propose if not x.startswith("`priority:")]
    if label_props:
        body = ("ak-bot triage proposal (not applied; probability in parentheses): " + ", ".join(propose)
                + "\n\n<sub>Apply or ignore; ak-bot will not repeat this. React with 👎 if it is wrong.</sub>")
        actions.append(ctx.act("comment proposal", gh.comment, ctx.repo, n, body))
    elif propose:
        actions.append("priority proposal recorded, not commented: " + ", ".join(propose))
    ctx.log.add(f"issue #{n}", QUESTIONS_VERSION, res.model, res.answers, tier,
                "; ".join(actions) or "ledger only", url=issue.get("url", ""))
    ctx.ledger.record(f"triage-{n}", f"issue-triage: #{n}: {tier}: {'; '.join(actions) or 'nothing'}")


def run(ctx: Context, limit: int = 30) -> int:
    labels = gh.repo_labels(ctx.repo)
    todo = [i for i in gh.list_issues(ctx.repo) if is_untriaged(i) and not ctx.ledger.has(f"triage-{i['number']}")]
    todo.sort(key=lambda i: i["number"], reverse=True)
    for issue in todo[:limit]:
        try:
            act(ctx, issue, decide(ctx, issue, labels), labels)
        except (gh.GhError, JevError, KeyError, TypeError, ValueError) as e:
            # Skip this subject only, and leave no ledger marker so it is retried.
            ctx.log.note(f"issue #{issue['number']}", f"skipped: {type(e).__name__}: {str(e)[:200]}")
    if not todo:
        print("nothing to triage")
    return 0
