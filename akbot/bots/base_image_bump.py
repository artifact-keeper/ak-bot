"""Bot 4: decide what to do about the pinned base image tracker.

The weekly watch (scheduled-base-image-watch.yml) opens or refreshes one
tracking issue and stops there. This bot reads that issue and decides:

    repoint_to_newer_tag  a newer sibling tag exists -> auto tier opens the
                          digest + VERSION bump PR (the only two edits the
                          tracker's own remedy names)
    upstream_override     no newer tag -> auto tier opens the override request
                          in the sibling repo; suggest tier comments on the tracker
    suppress              never automated; comment only

Digests resolve through the public registry token endpoint, so no packages
scope is needed on the token.
"""
from __future__ import annotations

import json
import re
import urllib.request

from .. import gh
from ..context import Context
from ..decisions import DRAFT_PR, OPEN_ISSUE, COMMENT, AUTO, SUGGEST, SKIP
from ..jev import choice, noul, score

BOT = "base-image-bump"
QUESTIONS_VERSION = "base-image-bump/q1"
TITLE_KEY = "Pinned base image watch"
DOCKERFILE = "docker/Dockerfile.scanner-adapter"
VERSION_FILE = "docker/scanner-adapter/VERSION"
IMAGE_RE = re.compile(r"^\s{2}(ghcr\.io/\S+)\s*$")
DIGEST_RE = re.compile(r"pinned digest\s*:\s*(sha256:[0-9a-f]{64})")
NEWEST_RE = re.compile(r"newest tag\s*:\s*(\S+)\s*\(registry ([^)]+)\)\s*(?:—|-)\s*(.*)$")
CVE_RE = re.compile(r"-\s+(CRITICAL|HIGH)\s+(CVE-\d{4}-\d+)\s+in\s+(\S+)\s+(\S+)\s+\(fixed in ([^)]+)\)")
RUN_RE = re.compile(r"actions/runs/(\d+)")


def questions() -> dict[str, dict]:
    return {
        "urgency": score(
            "Given the findings, how urgently must the pinned base image move?",
            ["no action needed", "watch; act only if it persists next week",
             "act this week", "act now: it will block the next Docker Publish"],
        ),
        "remedy": choice(
            "Which remedy fits the facts? The publish gate scans with --ignore-unfixed, so only findings "
            "with a fixed version block it.",
            {
                "repoint_to_newer_tag": "a newer published tag of the sibling image exists; move the digest to it "
                                        "and bump the adapter VERSION",
                "upstream_override": "the pinned digest is already the newest tag, so the fix must first be built "
                                     "upstream in the sibling repository and tagged",
                "suppress": "no fixed dependency exists to build against, so a .trivyignore suppression is honest",
                "none_of_the_above": "the report does not support a remedy",
            },
        ),
        "auto_pr_safe": noul(
            "Is it safe to open the repoint pull request without a human first: the newer tag is named, its "
            "digest resolves, and the only edits are one FROM digest line and the VERSION patch bump?",
            true="mechanical and reversible", false="needs a human to look first",
        ),
    }


def parse_report(body: str) -> dict:
    rep: dict = {"images": [], "run_id": None}
    m = RUN_RE.search(body or "")
    if m:
        rep["run_id"] = int(m.group(1))
    cur = None
    for ln in (body or "").splitlines():
        im = IMAGE_RE.match(ln)
        if im:
            cur = {"image": im.group(1), "pinned_digest": None, "newest_tag": None, "newest_registry_tag": None,
                   "pinned_is_newest": None, "findings": []}
            rep["images"].append(cur)
            continue
        if cur is None:
            continue
        dm = DIGEST_RE.search(ln)
        if dm:
            cur["pinned_digest"] = dm.group(1)
        nm = NEWEST_RE.search(ln)
        if nm:
            cur["newest_tag"], cur["newest_registry_tag"] = nm.group(1), nm.group(2)
            cur["pinned_is_newest"] = nm.group(3).strip().lower().startswith("pinned")
        cm = CVE_RE.search(ln)
        if cm:
            cur["findings"].append({"severity": cm.group(1), "cve": cm.group(2), "package": cm.group(3),
                                    "installed": cm.group(4), "fixed_in": cm.group(5)})
    return rep


def resolve_digest(image: str, tag: str) -> str | None:
    """Manifest-list digest of ghcr.io/<repo>:<tag> via the anonymous pull token."""
    host, _, path = image.partition("/")
    if host != "ghcr.io":
        return None
    try:
        with urllib.request.urlopen(f"https://ghcr.io/token?scope=repository:{path}:pull", timeout=10) as r:
            token = json.loads(r.read())["token"]
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{path}/manifests/{tag}", method="HEAD",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.oci.image.index.v1+json, "
                               "application/vnd.docker.distribution.manifest.list.v2+json, "
                               "application/vnd.oci.image.manifest.v1+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.headers.get("Docker-Content-Digest")
    except Exception:
        return None


def bump_patch(version: str) -> str:
    parts = version.strip().split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def find_tracker(ctx: Context) -> dict | None:
    for i in gh.list_issues(ctx.repo):
        if TITLE_KEY in i["title"]:
            return i
    return None


def run(ctx: Context) -> int:
    tracker = find_tracker(ctx)
    if not tracker:
        print("no open base image tracker; nothing to do")
        return 0
    rep = parse_report(tracker["body"])
    marker = f"base-image-{rep['run_id'] or tracker['updatedAt'][:10]}"
    if ctx.ledger.has(marker):
        print("this watch run already handled")
        return 0
    if not rep["images"]:
        ctx.log.note(f"issue #{tracker['number']}", "tracker body did not parse; skipping")
        return 0
    img = rep["images"][0]
    new_digest = None
    if img["newest_tag"] and img["pinned_is_newest"] is False:
        new_digest = resolve_digest(img["image"], img["newest_tag"])
    past = gh.gh_json("pr", "list", "-R", ctx.repo, "--state", "merged", "--search", "scanner-adapter in:title",
                      "--limit", "5", "--json", "number,title,mergedAt") or []
    state = {
        "tracker": {"number": tracker["number"], "created": tracker["createdAt"], "updated": tracker["updatedAt"]},
        "image": img,
        "newer_tag_digest_resolved": bool(new_digest),
        "publish_gate_flags": "--severity CRITICAL,HIGH --ignore-unfixed --ignorefile .trivyignore",
        "previous_repoint_prs": [{"title": p["title"], "merged": p["mergedAt"][:10]} for p in past],
    }
    res = ctx.jev.ask(state, questions())
    urg, rem, safe = res["urgency"], res["remedy"], res["auto_pr_safe"]
    subject = f"issue #{tracker['number']}"
    n = tracker["number"]
    rec = (f"ak-bot base-image-bump: urgency {urg.score:.1f}/3 ({urg.certainty:.2f}), remedy `{rem.choice}` "
           f"(p={rem.probabilities.get(rem.choice, 0):.2f}), auto-PR safe {safe.noul:.2f}.")

    if rem.choice == "repoint_to_newer_tag" and new_digest:
        tier = DRAFT_PR.tier(min(rem.certainty, safe.certainty)) if safe.yes else SUGGEST
        if tier == AUTO:
            action = ctx.act("open repoint PR", open_repoint_pr, ctx, img, new_digest, n)
        elif tier == SUGGEST:
            action = ctx.act("comment recommendation", gh.comment, ctx.repo, n,
                             rec + f"\n\nRecommended: repoint `{DOCKERFILE}` to `{img['image']}:{img['newest_tag']}` "
                                   f"(`{new_digest}`) and bump `{VERSION_FILE}`.")
        else:
            action = "ledger only"
    elif rem.choice == "upstream_override":
        tier = OPEN_ISSUE.tier(min(rem.certainty, urg.certainty)) if (urg.score or 0) >= 2 else COMMENT.tier(rem.certainty)
        sibling = f"{ctx.org}/{img['image'].split('/')[-1]}"
        cves = ", ".join(f"{f['cve']} ({f['package']} -> {f['fixed_in']})" for f in img["findings"])
        if tier == AUTO and (urg.score or 0) >= 2:
            action = ctx.act(f"open override request in {sibling}", open_upstream_issue, ctx, sibling, img, n)
        elif tier in (AUTO, SUGGEST):
            action = ctx.act("comment recommendation", gh.comment, ctx.repo, n,
                             rec + f"\n\nRecommended: open the dependency override in `{sibling}` for {cves}, tag the "
                                   f"next -rN, then repoint here.")
        else:
            action = "ledger only"
    else:
        tier = COMMENT.tier(rem.certainty)
        action = ctx.act("comment recommendation", gh.comment, ctx.repo, n,
                         rec + "\n\nNo automated remedy applies; a maintainer should decide.") if tier != SKIP else "ledger only"
    ctx.log.add(subject, QUESTIONS_VERSION, res.model, res.answers, tier, action, url=tracker.get("url", ""))
    ctx.ledger.record(marker, f"base-image-bump: {subject}: {tier} -> {action}")
    return 0


def open_repoint_pr(ctx: Context, img: dict, new_digest: str, tracker: int) -> None:
    base = gh.branch_sha(ctx.repo, "main")
    tag = img["newest_tag"]
    branch = f"ak-bot/base-image-{re.sub(r'[^a-z0-9.-]', '-', tag.lower())}"
    if not gh.ensure_branch(ctx.repo, branch, base):
        return
    df, _ = gh.get_file(ctx.repo, DOCKERFILE, branch)
    old_line = f"{img['image']}@{img['pinned_digest']}"
    if old_line not in df:
        raise RuntimeError(f"{DOCKERFILE} no longer pins {old_line}")
    gh.put_file(ctx.repo, branch, DOCKERFILE, df.replace(old_line, f"{img['image']}@{new_digest}"),
                f"chore(scanner-adapter): repoint {img['image'].split('/')[-1]} base image to {tag}")
    ver, _ = gh.get_file(ctx.repo, VERSION_FILE, branch)
    newver = bump_patch(ver)
    gh.put_file(ctx.repo, branch, VERSION_FILE, newver + "\n", f"chore(scanner-adapter): publish {newver}")
    cves = "\n".join(f"- {f['severity']} {f['cve']} in {f['package']} {f['installed']} (fixed in {f['fixed_in']})"
                     for f in img["findings"])
    body = (f"Fixes #{tracker}\n\nThe weekly watch found the pinned `{img['image']}` digest carries fixed findings:\n\n"
            f"{cves}\n\nThis repoints `{DOCKERFILE}` to `{tag}` (`{new_digest}`) and bumps `{VERSION_FILE}` to "
            f"`{newver}` so the adapter republishes under a new exact tag.\n\n_Opened by ak-bot base-image-bump; "
            f"the digest was resolved from ghcr.io at open time._")
    labels = [l for l in ("automated", "type:security") if l in gh.repo_labels(ctx.repo)]
    print(gh.open_pr(ctx.repo, branch, "main", f"chore(scanner-adapter): rebuild on {tag} and publish {newver}", body, labels))


def open_upstream_issue(ctx: Context, sibling: str, img: dict, tracker: int) -> None:
    cve_ids = [f["cve"] for f in img["findings"]]
    existing = gh.issue_search(sibling, " ".join(cve_ids)) if cve_ids else []
    if existing:
        gh.comment(ctx.repo, tracker, f"ak-bot: an override request already exists upstream: {existing[0]['url']}")
        return
    body = ("The digest-pinned image consumed by artifact-keeper/artifact-keeper#%d carries fixed findings under "
            "`--severity CRITICAL,HIGH --ignore-unfixed`:\n\n%s\n\nPlease build with the fixed versions and tag "
            "the next `-rN`; artifact-keeper will repoint the digest.\n\n_Opened by ak-bot._") % (
        tracker, "\n".join(f"- {f['severity']} {f['cve']} in {f['package']} {f['installed']} (fixed in {f['fixed_in']})"
                           for f in img["findings"]))
    url = gh.create_issue(sibling, f"Dependency override needed: {', '.join(cve_ids)}", body, [])
    gh.comment(ctx.repo, tracker, f"ak-bot opened the upstream override request: {url}")
