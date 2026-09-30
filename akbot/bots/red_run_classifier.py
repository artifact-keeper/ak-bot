"""Bot 2: classify every red scheduled/main run and route it.

    flake or timeout, rerun likely to pass  -> rerun once (auto tier only)
    regression / config drift               -> issue (auto) or digest comment (suggest)
    infrastructure / expected red           -> digest comment
    anything below the suggest line         -> ledger only

Deterministic pre-checks run first and are put in the state JEV sees: a job
that ended `cancelled` after running up to its `timeout-minutes` is a timeout,
and a `Notify on failure` step that was skipped on that cancel means nobody
was told -- the exact shape scheduled-tests.yml has had for three nights.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import gh
from ..context import Context, find_or_create_issue
from ..decisions import OPEN_ISSUE, COMMENT, RERUN, AUTO, SUGGEST, SKIP, redact
from ..jev import choice, noul, score

BOT = "red-run-classifier"
QUESTIONS_VERSION = "red-run-classifier/q1"
DIGEST_TITLE = "ak-bot: red run digest"
RED = {"failure", "cancelled", "timed_out"}
EVENTS = {"schedule", "push", "workflow_dispatch"}


def questions() -> dict[str, dict]:
    return {
        "cause": choice(
            "What most likely caused this GitHub Actions run to end without success? Use the failed step, "
            "the log tail, the run history for this workflow and the timing facts.",
            {
                "infrastructure": "a runner, network, registry, quota, token or third-party service failed; "
                                  "the code under test never reached a verdict",
                "flake": "the tests ran and something failed for a timing, ordering or resource reason that a "
                         "rerun on the same commit would most likely not reproduce",
                "regression": "the code or configuration under test is genuinely broken by a recent change",
                "timeout": "the job ran until its time limit and was cancelled, which hides whether it would have passed",
                "expected_red": "the workflow is a watch or gate reporting a real external condition (a CVE, a "
                                "bookkeeping gap, a stale pin) and is doing its job",
                "none_of_the_above": "the evidence does not support any of these",
            },
        ),
        "rerun_would_pass": noul(
            "If this run were retried on the same commit with nothing changed, would it most likely succeed?",
            true="the failure is transient", false="the failure is reproducible or external",
        ),
        "severity": score(
            "How much does this red matter to cutting the next release?",
            ["no effect on shipping", "slows a maintainer down", "blocks a release step until fixed",
             "release-critical and time-sensitive"],
        ),
    }


def timing_facts(run: dict, jobs: list[dict]) -> dict:
    facts: dict = {"jobs": []}
    for j in jobs:
        if j.get("conclusion") not in RED:
            continue
        started, done = j.get("startedAt"), j.get("completedAt")
        minutes = None
        if started and done:
            minutes = round((datetime.fromisoformat(done.replace("Z", "+00:00"))
                             - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() / 60, 1)
        steps = j.get("steps") or []
        red_steps = [s["name"] for s in steps if s.get("conclusion") in RED]
        notify_skipped = any("notify" in (s.get("name") or "").lower() and s.get("conclusion") == "skipped"
                             for s in steps)
        facts["jobs"].append({
            "name": j.get("name"), "conclusion": j.get("conclusion"), "minutes": minutes,
            "red_steps": red_steps, "notify_step_skipped": notify_skipped,
            "looks_like_timeout": j.get("conclusion") == "cancelled" and minutes is not None and minutes >= 40,
        })
    return facts


def collect(ctx: Context, since_hours: float, limit: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    out = []
    for r in gh.list_runs(ctx.repo, limit=limit):
        if r["status"] != "completed" or r["conclusion"] not in RED or r["event"] not in EVENTS:
            continue
        if datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) < cutoff:
            continue
        if ctx.ledger.has(f"red-run-{r['databaseId']}"):
            continue
        out.append(r)
    return out


def history(ctx: Context, workflow_name: str) -> list[str]:
    try:
        return [r["conclusion"] or r["status"] for r in gh.list_runs(ctx.repo, workflow_name, limit=10)]
    except gh.GhError:
        return []


def decide(ctx: Context, run: dict) -> tuple[dict, object]:
    rid = run["databaseId"]
    jobs = gh.run_jobs(ctx.repo, rid)
    facts = timing_facts(run, jobs)
    log = gh.run_log(ctx.repo, rid, failed_only=True)
    tail = [redact(ln[:300]) for ln in log if ln.strip()][-120:]
    meta = gh.api(f"repos/{ctx.repo}/actions/runs/{rid}", check=False) or {}
    state = {
        "run": {"id": rid, "workflow": run["workflowName"], "event": run["event"], "branch": run["headBranch"],
                "conclusion": run["conclusion"], "attempt": meta.get("run_attempt", 1), "created": run["createdAt"]},
        "timing": facts,
        "recent_conclusions_for_this_workflow": history(ctx, run["workflowName"]),
        "failed_log_tail": tail,
    }
    return state, ctx.jev.ask(state, questions())


def act(ctx: Context, run: dict, state: dict, res) -> None:
    rid = run["databaseId"]
    cause, rerun, sev = res["cause"], res["rerun_would_pass"], res["severity"]
    subject = f"run {rid} ({run['workflowName']})"
    marker = f"red-run-{rid}"
    attempt = state["run"]["attempt"]
    notify_gap = any(j["notify_step_skipped"] for j in state["timing"]["jobs"])
    summary = (f"`{run['workflowName']}` on `{run['headBranch']}` ended **{run['conclusion']}** "
               f"([run {rid}]({run['url']})). JEV: cause `{cause.choice}` "
               f"(p={cause.probabilities.get(cause.choice, 0):.2f}, confidence {cause.certainty:.2f}), "
               f"rerun would pass {rerun.noul:.2f}, severity {sev.score:.1f}/3.")
    if notify_gap:
        summary += ("\n\nDeterministic finding: a *Notify on failure* step was **skipped** because the job was "
                    "cancelled, not failed. `if: failure()` does not fire on a timeout; use "
                    "`if: failure() || cancelled()`.")

    if cause.choice in ("flake", "timeout") and rerun.yes:
        tier = RERUN.tier(min(cause.certainty, rerun.certainty))
        if tier == AUTO and attempt == 1 and cause.choice == "flake":
            action = ctx.act(f"rerun failed jobs of {rid}", gh.rerun_failed, ctx.repo, rid)
        elif tier in (AUTO, SUGGEST):
            action = ctx.act("append to digest", digest, ctx, summary + "\n\nNot rerun automatically"
                             + (" (already a retry)." if attempt > 1 else " (timeouts are rerun by hand: they cost 45 minutes)."))
        else:
            action = "ledger only"
    elif cause.choice == "regression":
        tier = OPEN_ISSUE.tier(cause.certainty)
        if tier == AUTO:
            labels = [l for l in ("ci", "type:bug") if l in gh.repo_labels(ctx.repo)]
            body = summary + "\n\n<details><summary>failed log tail</summary>\n\n```\n" + \
                   "\n".join(state["failed_log_tail"][-60:]) + "\n```\n</details>\n\n_Opened by ak-bot red-run-classifier._"
            action = ctx.act("open issue", gh.create_issue, ctx.repo,
                             f"ci: {run['workflowName']} is red on {run['headBranch']} ({cause.choice})", body, labels)
        elif tier == SUGGEST:
            action = ctx.act("append to digest", digest, ctx, summary)
        else:
            action = "ledger only"
    else:
        tier = COMMENT.tier(cause.certainty)
        action = ctx.act("append to digest", digest, ctx, summary) if tier != SKIP else "ledger only"
        if notify_gap and tier == SKIP:
            action = ctx.act("append to digest (notify gap)", digest, ctx, summary)
    ctx.log.add(subject, QUESTIONS_VERSION, res.model, res.answers, tier, action, url=run["url"])
    ctx.ledger.record(marker, f"red-run-classifier: {subject}: {tier} -> {action}")


def digest(ctx: Context, text: str) -> None:
    n = find_or_create_issue(ctx, DIGEST_TITLE, ["automated", "ci"],
                             "Rolling digest of red scheduled/main runs that ak-bot classified but did not act on. "
                             "One comment per run. Close and it will be recreated.")
    gh.comment(ctx.repo, n, text)


def run(ctx: Context, since_hours: float = 26.0, limit: int = 80) -> int:
    runs = collect(ctx, since_hours, limit)
    if not runs:
        print("no new red runs")
        return 0
    for r in runs:
        state, res = decide(ctx, r)
        act(ctx, r, state, res)
    return 0
