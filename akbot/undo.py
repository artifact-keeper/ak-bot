"""`akbot undo --run-id N [--dry-run]`: reverse what one run did.

Reads the run's decision records (the workflow artifact first, the `records`
branch second) and reverses each structured effect:

    label    -> remove the labels, if still present
    comment  -> delete the comment
    open     -> comment "retracted", close the issue or PR, delete an ak-bot/* branch
    rerun    -> cannot be undone; printed

Then the ledger comments carrying `ak-bot-run: N` are deleted, so every subject
of that run is eligible again on the next run.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import re
import tempfile

from . import gh
from .context import LEDGER_TITLE_RE

COMMENT_ID_RE = re.compile(r"#issuecomment-(\d+)")
ISSUE_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/(issues|pull)/(\d+)")


def records_for_run(own_repo: str, run_id: int) -> list[dict]:
    recs: list[dict] = []
    with tempfile.TemporaryDirectory() as d:
        gh.gh("run", "download", "-R", own_repo, str(run_id), "-D", d, check=False)
        for f in glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True):
            recs += [json.loads(ln) for ln in open(f) if ln.strip()]
    if recs:
        return [r for r in recs if r.get("run_id") == str(run_id) or r.get("kind") == "run"]
    tree = gh.api(f"repos/{own_repo}/git/trees/records?recursive=1", check=False)
    if isinstance(tree, dict):
        for t in tree.get("tree", []):
            if t["type"] != "blob" or not t["path"].endswith(".jsonl"):
                continue
            data = gh.api(f"repos/{own_repo}/contents/{t['path']}?ref=records", check=False)
            if not isinstance(data, dict):
                continue
            for ln in base64.b64decode(data["content"]).decode().splitlines():
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if rec.get("run_id") == str(run_id):
                    recs.append(rec)
    return recs


def undo_effect(eff: dict, dry_run: bool) -> str:
    kind, args, result = eff.get("kind"), eff.get("args") or [], eff.get("result")
    if kind == "label" and len(args) >= 3:
        repo, number, labels = args[0], int(args[1]), list(args[2])
        if dry_run:
            return f"would remove {labels} from {repo}#{number}"
        gh.remove_labels(repo, number, labels)
        return f"removed {labels} from {repo}#{number}"
    if kind == "comment" and isinstance(result, str):
        m = COMMENT_ID_RE.search(result)
        if not m:
            return f"comment result has no id: {result}"
        repo = args[0] if args and "/" in str(args[0]) else ISSUE_URL_RE.search(result).group(1)
        if dry_run:
            return f"would delete comment {m.group(1)} on {repo}"
        gh.delete_comment(repo, int(m.group(1)))
        return f"deleted comment {m.group(1)} on {repo}"
    if kind == "open" and isinstance(result, str):
        m = ISSUE_URL_RE.search(result)
        if not m:
            return f"open result is not an issue/PR url: {result}"
        repo, number = m.group(1), int(m.group(3))
        if dry_run:
            return f"would retract and close {result}"
        gh.close_issue(repo, number, "Retracted by `akbot undo`.")
        if m.group(2) == "pull":
            head = gh.gh_json("pr", "view", "-R", repo, str(number), "--json", "headRefName") or {}
            if (head.get("headRefName") or "").startswith("ak-bot/"):
                gh.delete_branch(repo, head["headRefName"])
        return f"retracted {result}"
    if kind == "rerun":
        return f"cannot undo a rerun: {result}"
    return f"no undo for {kind}: {eff.get('description')}"


def forget_ledger_entries(repo: str, run_id: int, dry_run: bool) -> int:
    n = 0
    for i in gh.list_issues(repo, state="all"):
        if not LEDGER_TITLE_RE.match(i["title"]):
            continue
        for c in gh.issue_comments(repo, i["number"]):
            if f"ak-bot-run: {run_id}" in (c.get("body") or ""):
                n += 1
                if not dry_run:
                    gh.delete_comment(repo, c["id"])
    return n


def main(a) -> int:
    if not a.run_id:
        print("undo needs --run-id")
        return 2
    own = os.environ.get("AK_BOT_OWN_REPO", "artifact-keeper/ak-bot")
    recs = records_for_run(own, a.run_id)
    effects = [(r.get("subject"), e) for r in recs if r.get("kind") != "run" for e in r.get("effects") or []]
    if not effects:
        print(f"no recorded effects for run {a.run_id} (dry runs record none)")
    for subject, eff in effects:
        print(f"{subject}: {undo_effect(eff, a.dry_run)}")
    n = forget_ledger_entries(a.repo, a.run_id, a.dry_run)
    print(f"{'would forget' if a.dry_run else 'forgot'} {n} ledger entr{'y' if n == 1 else 'ies'} for run {a.run_id}")
    return 0
