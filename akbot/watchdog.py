"""`akbot watchdog`: is the bot itself alive, and is it doing what AK_BOT_LIVE says?

Runs against ak-bot's own repository with github.token. It files, updates and
closes ONE issue, "ak-bot watchdog", assigned to the maintainer, so the issue
list is the status page and a notification is impossible to miss. Checks:

  * every bot workflow is still enabled (GitHub disables schedules on a public
    repo after 60 days without activity; the records commits prevent that,
    and this re-enables on sight);
  * the last three runs of a bot did not all fail;
  * a run record exists within 2x the bot's cadence (read from the `records`
    branch, where every run commits its decisions);
  * a bot listed in AK_BOT_LIVE did not just run dry or on the fake model.
"""
from __future__ import annotations

import base64
import json
import os
import time

from . import gh

WATCHDOG_TITLE = "ak-bot watchdog"
CADENCE_H = {"issue-triage": 6, "pr-review-router": 4, "red-run-classifier": 3,
             "changelog-drafter": 36, "preflight-audit": 36, "base-image-bump": 24 * 8}
WORKFLOW_FILES = {bot: f"{bot}.yml" for bot in CADENCE_H}


def latest_run_record(own_repo: str, bot: str) -> dict | None:
    tree = gh.api(f"repos/{own_repo}/git/trees/records?recursive=1", check=False)
    if not isinstance(tree, dict):
        return None
    paths = sorted(t["path"] for t in tree.get("tree", [])
                   if t["type"] == "blob" and t["path"].startswith("records/") and f"/{bot}-" in t["path"])
    if not paths:
        return None
    data = gh.api(f"repos/{own_repo}/contents/{paths[-1]}?ref=records", check=False)
    if not isinstance(data, dict):
        return None
    text = base64.b64decode(data["content"]).decode()
    for ln in reversed(text.splitlines()):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") == "run":
            return rec
    return None


def check(own_repo: str, live_list: str, halt: str) -> list[str]:
    problems: list[str] = []
    live = {x.strip().lower() for x in live_list.split(",") if x.strip()}
    wfs = gh.gh_json("workflow", "list", "-R", own_repo, "--all", "--json", "name,state,path") or []
    for wf in wfs:
        if wf["path"].rsplit("/", 1)[-1] in WORKFLOW_FILES.values() and wf["state"] != "active" and not halt:
            problems.append(f"workflow `{wf['name']}` is {wf['state']}; re-enabling")
            gh.gh("workflow", "enable", "-R", own_repo, wf["name"], check=False)
    for bot, hours in CADENCE_H.items():
        runs = [r for r in gh.list_runs(own_repo, WORKFLOW_FILES[bot], limit=3) if r["status"] == "completed"]
        if runs and all(r["conclusion"] == "failure" for r in runs):
            problems.append(f"`{bot}`: last {len(runs)} run(s) failed ({runs[0]['url']})")
        rec = latest_run_record(own_repo, bot)
        if rec is None:
            problems.append(f"`{bot}`: no run record on the records branch")
            continue
        age_h = (time.time() - float(rec.get("ts", 0))) / 3600
        if age_h > 2 * hours and not halt:
            problems.append(f"`{bot}`: last run record is {age_h:.0f}h old (cadence {hours}h)")
        if (bot in live or "all" in live) and (rec.get("dry_run") or str(rec.get("model", "")).startswith("fake")):
            problems.append(f"`{bot}`: listed in AK_BOT_LIVE but its last run was dry or on the fake model "
                            f"(identity={rec.get('identity') or 'none'}, model={rec.get('model')})")
    return problems


def upsert_issue(own_repo: str, problems: list[str], assignee: str, dry_run: bool) -> str:
    existing = None
    for i in gh.list_issues(own_repo):
        if i["title"] == WATCHDOG_TITLE:
            existing = i
            break
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    if not problems:
        if existing:
            if dry_run:
                return f"would close #{existing['number']}"
            gh.close_issue(own_repo, existing["number"], f"All checks pass as of {stamp}. Closing.")
            return f"closed #{existing['number']}"
        return "healthy, no issue"
    body = f"Checked {stamp}:\n\n" + "\n".join(f"- {p}" for p in problems) + \
           "\n\nThis issue is maintained by `akbot watchdog`; it closes itself when every check passes."
    if dry_run:
        return "would " + ("comment on" if existing else "open") + " the watchdog issue:\n" + body
    if existing:
        gh.comment(own_repo, existing["number"], body)
        return f"commented on #{existing['number']}"
    args = ["issue", "create", "-R", own_repo, "--title", WATCHDOG_TITLE, "--body", body]
    if assignee:
        args += ["--assignee", assignee]
    return "opened " + gh.gh(*args).strip()


def main(a) -> int:
    own = os.environ.get("AK_BOT_OWN_REPO", "artifact-keeper/ak-bot")
    problems = check(own, os.environ.get("AK_BOT_LIVE", ""), os.environ.get("AK_BOT_HALT", ""))
    out = upsert_issue(own, problems, os.environ.get("AK_BOT_MAINTAINER", ""), a.dry_run)
    print(f"watchdog: {len(problems)} problem(s); {out}")
    for p in problems:
        print(f"::warning title=ak-bot watchdog::{p}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("## ak-bot watchdog\n\n" + ("\n".join(f"- {p}" for p in problems) or "All checks pass.") + "\n")
    return 0
