#!/usr/bin/env python3
"""Calibration report: stated probability vs. what humans did afterwards.

    python3 scripts/calibrate.py [--own-repo artifact-keeper/ak-bot] [--min-age-days 10] [--records DIR]

Reads every decision record on the `records` branch (or a local directory of
*.jsonl), drops fake-model and too-recent records, resolves an outcome per
record from GitHub, bins by the probability the bot stated, and prints
observed accuracy per bin, per (bot, question, questions_version, model).
The last section recommends auto/suggest lines from the data, or says the
sample is too small. Resolvers:

    issue-triage      type / area choice   -> is the predicted label on the issue now?
    pr-review-router  readiness            -> was the PR merged?
    red-run-classifier rerun_would_pass    -> did attempt 2 of the run succeed?
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from akbot import gh  # noqa: E402

BINS = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.01]
TYPE_LABELS = {"bug": "type:bug", "enhancement": "type:enhancement", "documentation": "type:documentation",
               "question": "type:question", "security": "type:security", "chore": "type:chore"}
NUM_RE = re.compile(r"#(\d+)|run (\d+)")


def load(own_repo: str, local: str | None) -> list[dict]:
    recs = []
    if local:
        for f in glob.glob(os.path.join(local, "**", "*.jsonl"), recursive=True):
            recs += [json.loads(ln) for ln in open(f) if ln.strip()]
        return recs
    tree = gh.api(f"repos/{own_repo}/git/trees/records?recursive=1", check=False)
    for t in (tree or {}).get("tree", []):
        if t["type"] == "blob" and t["path"].endswith(".jsonl"):
            data = gh.api(f"repos/{own_repo}/contents/{t['path']}?ref=records", check=False)
            if isinstance(data, dict):
                recs += [json.loads(ln) for ln in base64.b64decode(data["content"]).decode().splitlines() if ln.strip()]
    return recs


_label_cache: dict[tuple[str, int], set[str]] = {}


def labels_now(repo: str, number: int) -> set[str]:
    key = (repo, number)
    if key not in _label_cache:
        data = gh.api(f"repos/{repo}/issues/{number}", check=False) or {}
        _label_cache[key] = {l["name"] for l in data.get("labels") or []}
    return _label_cache[key]


def resolve(rec: dict, repo: str) -> list[tuple[str, float, int]]:
    """(question, stated_p, outcome) triples for one record; outcome 1 = right."""
    out = []
    m = NUM_RE.search(rec.get("subject", ""))
    if not m:
        return out
    number = int(m.group(1) or m.group(2))
    ans = rec.get("answers") or {}
    bot = rec["bot"]
    if bot == "issue-triage":
        now = labels_now(repo, number)
        if "type" in ans and ans["type"].get("choice") in TYPE_LABELS:
            a = ans["type"]
            out.append(("type", a["probabilities"].get(a["choice"], 0), int(TYPE_LABELS[a["choice"]] in now)))
        if "area" in ans and ans["area"].get("choice") not in (None, "none_of_the_above"):
            a = ans["area"]
            lbl = a["choice"] if a["choice"] in ("core", "web-ui", "ci") else f"registry/{a['choice']}"
            out.append(("area", a["probabilities"].get(a["choice"], 0), int(lbl in now)))
    elif bot == "pr-review-router" and "readiness" in ans:
        pr = gh.api(f"repos/{repo}/pulls/{number}", check=False) or {}
        if pr.get("state") == "closed":
            a = ans["readiness"]
            p = sum(v for k, v in (a.get("probabilities") or {}).items() if k.isdigit() and int(k) >= 3)
            out.append(("readiness", p, int(bool(pr.get("merged_at")))))
    elif bot == "red-run-classifier" and "rerun_would_pass" in ans:
        run = gh.api(f"repos/{repo}/actions/runs/{number}", check=False) or {}
        if int(run.get("run_attempt") or 1) > 1:
            out.append(("rerun_would_pass", ans["rerun_would_pass"].get("noul") or 0,
                        int(run.get("conclusion") == "success")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--own-repo", default="artifact-keeper/ak-bot")
    ap.add_argument("--repo", default="artifact-keeper/artifact-keeper")
    ap.add_argument("--records", default=None, help="local directory of *.jsonl instead of the records branch")
    ap.add_argument("--min-age-days", type=float, default=10)
    a = ap.parse_args()
    cutoff = time.time() - a.min_age_days * 86400
    recs = [r for r in load(a.own_repo, a.records)
            if r.get("kind") != "run" and r.get("answers") and not str(r.get("model", "")).startswith("fake")
            and float(r.get("ts", 0)) < cutoff]
    groups: dict[tuple, list[tuple[float, int]]] = defaultdict(list)
    for r in recs:
        for q, p, y in resolve(r, a.repo):
            groups[(r["bot"], q, r.get("questions_version"), r.get("model"))].append((p, y))
    if not groups:
        print(f"no resolvable records older than {a.min_age_days} days")
        return 0
    for key, pairs in sorted(groups.items()):
        print(f"\n## {key[0]} / {key[1]}  ({key[2]}, {key[3]})  n={len(pairs)}\n")
        print("| bin | n | mean p | observed | gap |\n|---|---|---|---|---|")
        rec_auto = rec_suggest = None
        for lo, hi in zip(BINS, BINS[1:]):
            b = [(p, y) for p, y in pairs if lo <= p < hi]
            if not b:
                continue
            mp = sum(p for p, _ in b) / len(b)
            obs = sum(y for _, y in b) / len(b)
            print(f"| [{lo:.2f}, {min(hi, 1):.2f}) | {len(b)} | {mp:.2f} | {obs:.2f} | {obs - mp:+.2f} |")
            if len(b) >= 20 and obs >= 0.95 and rec_auto is None:
                rec_auto = lo
            if len(b) >= 20 and obs >= 0.75 and rec_suggest is None:
                rec_suggest = lo
        print(f"\nrecommended: auto_at={rec_auto if rec_auto is not None else 'n too small'}, "
              f"suggest_at={rec_suggest if rec_suggest is not None else 'n too small'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
