"""Bot 6: second opinion on release preflight transcripts.

Deterministic checks come first, because they are the ones that matter:
  * a READY run must have uploaded exactly one `release-preflight-<sha>`
    evidence artifact and <sha> must equal the sha in the verdict line;
  * a NOT READY run must have uploaded none;
  * the run conclusion must agree with the verdict.
A hard mismatch opens an issue without asking JEV anything.

Then JEV reads a compact form of the transcript and answers whether the
check lines support the verdict and whether the transcript could be read
as being about a different ref. Low support or high confusion goes to a
digest comment; everything is logged.
"""
from __future__ import annotations

import re

from .. import gh
from ..context import Context, find_or_create_issue
from ..decisions import COMMENT, AUTO, SUGGEST, SKIP, redact
from ..jev import JevError, noul

BOT = "preflight-audit"
QUESTIONS_VERSION = "preflight-audit/q1"
WORKFLOW = "release-preflight.yml"
DIGEST_TITLE = "ak-bot: preflight audit digest"
VERDICT_RE = re.compile(r"(NOT READY|READY) to cut from (\S+)@([0-9a-f]{7,40})")
CHECK_LINE_RE = re.compile(r"^\s*(\d\)\s.*|\[(PASS|FAIL|WARN|OK)\].*|INFRA:.*|NOT MEASURED.*|range:.*|pending sections:.*)$")
ARTIFACT_RE = re.compile(r"^release-preflight-([0-9a-f]{40})$")


def questions() -> dict[str, dict]:
    return {
        "verdict_supported": noul(
            "Do the individual check results in this transcript support the final verdict line exactly as stated?",
            true="every blocking check passed for READY, or at least one failed for NOT READY, and nothing contradicts it",
            false="a check result, an INFRA line or a NOT MEASURED line contradicts or weakens the verdict",
        ),
        "scope_confusion": noul(
            "Could a reader reasonably take this transcript to be a verdict about a different ref or commit than "
            "the one the verdict line names?",
            true="the ref, sha, branch or artifact name disagree or are ambiguous",
            false="ref, sha, branch and artifact all name the same tree",
        ),
        "weakened_by_skips": noul(
            "Was any check skipped, overridden or left unmeasured in a way that makes the verdict weaker than it reads?",
            true="a skip or override materially weakens it", false="no skip or override matters here",
        ),
    }


def deterministic(run: dict, lines: list[str], artifacts: list[dict]) -> tuple[dict, list[str]]:
    facts: dict = {"verdict": None, "audited_ref": None, "audited_sha": None, "evidence_artifacts": []}
    for ln in lines:
        m = VERDICT_RE.search(ln)
        if m:
            facts["verdict"], facts["audited_ref"], facts["audited_sha"] = m.group(1), m.group(2), m.group(3)
    facts["evidence_artifacts"] = [a["name"] for a in artifacts if ARTIFACT_RE.match(a["name"])]
    problems: list[str] = []
    if facts["verdict"] is None:
        return facts, problems          # not auditable; the caller decides, never an issue
    ev = facts["evidence_artifacts"]
    if facts["verdict"] == "READY":
        if len(ev) != 1:
            problems.append(f"READY but {len(ev)} evidence artifact(s) uploaded")
        elif not ARTIFACT_RE.match(ev[0]).group(1).startswith(facts["audited_sha"]):
            # The verdict line prints an abbreviated sha; the artifact carries the full one.
            problems.append(f"evidence artifact names {ev[0][-40:][:10]} but the verdict names {facts['audited_sha'][:10]}")
        if run["conclusion"] != "success":
            problems.append(f"READY but the run concluded {run['conclusion']}")
    else:
        if ev:
            problems.append(f"NOT READY but evidence artifact(s) uploaded: {ev}")
        if run["conclusion"] == "success":
            problems.append("NOT READY but the run concluded success")
    if run["event"] == "schedule" and facts["audited_ref"] not in ("main", "refs/heads/main"):
        problems.append(f"scheduled run audited {facts['audited_ref']}, not main")
    return facts, problems


def run(ctx: Context, limit: int = 3) -> int:
    runs = [r for r in gh.list_runs(ctx.repo, WORKFLOW, limit=limit + 5)
            if r["status"] == "completed" and r["conclusion"] in ("success", "failure")][:limit]
    for r in runs:
        try:
            audit_one(ctx, r)
        except (gh.GhError, JevError, KeyError, TypeError, ValueError) as e:
            ctx.log.note(f"preflight run {r['databaseId']}", f"skipped: {type(e).__name__}: {str(e)[:200]}")
    if not runs:
        print("no preflight runs")
    return 0


def audit_one(ctx: Context, r: dict) -> None:
    rid = r["databaseId"]
    marker = f"preflight-audit-{rid}"
    if ctx.ledger.has(marker):
        return
    lines = gh.run_log(ctx.repo, rid)
    subject = f"preflight run {rid}"
    if not lines:
        ctx.log.note(subject, "log unavailable; not audited (will retry)")
        return
    facts, problems = deterministic(r, lines, gh.run_artifacts(ctx.repo, rid))
    if facts["verdict"] is None:
        ctx.log.note(subject, "no verdict line in the transcript; not auditable")
        ctx.ledger.record(marker, f"preflight-audit: {subject}: no verdict line")
        return
    if problems:
        body = (f"Release preflight [run {rid}]({r['url']}) on `{r['headBranch']}` is internally inconsistent:\n\n"
                + "\n".join(f"- {p}" for p in problems)
                + f"\n\nVerdict line: `{facts['verdict']} {facts['audited_ref']}@{facts['audited_sha']}`; "
                  f"evidence artifacts: {facts['evidence_artifacts'] or 'none'}.\n\n_Opened by ak-bot preflight-audit._")
        labels = [l for l in ("release-process", "ci", "priority:p1") if l in gh.repo_labels(ctx.repo)]
        action = ctx.act("open issue", gh.create_issue, ctx.repo,
                         f"release: preflight run {rid} verdict does not match its evidence", body, labels)
        ctx.log.note(subject, action)
        ctx.ledger.record(marker, f"preflight-audit: {subject}: deterministic mismatch -> {action}")
        return
    state = {
        "run": {"id": rid, "event": r["event"], "branch": r["headBranch"], "head_sha": r["headSha"],
                "conclusion": r["conclusion"]},
        "facts": facts,
        "transcript_check_lines": [redact(ln.strip()) for ln in lines if CHECK_LINE_RE.match(ln)][:120],
    }
    res = ctx.jev.ask(state, questions())
    sup, conf, weak = res["verdict_supported"], res["scope_confusion"], res["weakened_by_skips"]
    # Each noul is a probability of a problem (for `sup`, of the verdict being
    # unsupported). Route on the certainty of the worst one, the same |2p-1|
    # scale every other bot uses, and only when it leans towards a problem.
    bad = max(1 - (sup.noul if sup.noul is not None else 1.0), conf.noul or 0.0, weak.noul or 0.0)
    tier = COMMENT.tier(abs(2 * bad - 1)) if bad > 0.5 else SKIP
    text = (f"[run {rid}]({r['url']}) `{facts['verdict']}` for `{facts['audited_ref']}@{facts['audited_sha'][:10]}`: "
            f"verdict supported {sup.noul_or_nan:.2f}, scope confusion {conf.noul_or_nan:.2f}, "
            f"weakened by skips {weak.noul_or_nan:.2f}. Deterministic checks passed.")
    action = ctx.act("append to digest", digest, ctx, text) if tier in (AUTO, SUGGEST) else "ledger only"
    ctx.log.add(subject, QUESTIONS_VERSION, res.model, res.answers, tier, action, url=r["url"])
    ctx.ledger.record(marker, f"preflight-audit: {subject}: {tier} -> {action}")


def digest(ctx: Context, text: str) -> None:
    n = find_or_create_issue(ctx, DIGEST_TITLE, ["automated", "release-process"],
                             "Preflight transcripts whose verdict JEV rated as weakly supported or ambiguous. "
                             "Deterministic mismatches get their own issue instead.")
    gh.comment(ctx.repo, n, text)
