"""Bot 1: draft CHANGELOG fragments for the PRs release preflight check 5 lists
as undocumented.

Division of labour, stated plainly:
  * the GATE (scripts/ci/release-preflight.sh check 5) decides what is missing;
    this bot reads its transcript rather than re-deriving the rules;
  * JEV decides the SECTION, whether the change is USER-VISIBLE at all, and
    whether it is BREAKING -- typed decisions with probabilities;
  * the bullet PROSE is templated from the PR title and body. JEV does not
    write text, and this bot does not pretend otherwise: every draft is
    labelled as one in the PR it opens, for a human to edit before merging.

The commit it makes is `docs(changelog): ...` touching only
changes/unreleased/**, so it is itself exempt from check 5.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import gh
from ..context import Context
from ..decisions import DRAFT_PR, SKIP, redact
from ..jev import choice, noul

BOT = "changelog-drafter"
QUESTIONS_VERSION = "changelog-drafter/q1"
SECTIONS = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
WORKFLOW = "release-preflight.yml"

UNDOC_HEAD = "are not described by the pending section:"
FORWARD_HEAD = "reference work that is NOT in"
RANGE_RE = re.compile(r"commits in (\S+)\.\.HEAD")
ITEM_RE = re.compile(r"^\s*- #(\d+)\s+(.*)$")
DETAIL_RE = re.compile(r"^\s*not exempt: (.*)$")
VERDICT_RE = re.compile(r"(NOT READY|READY) to cut from (\S+)@([0-9a-f]{7,40})")
CONVENTIONAL_RE = re.compile(r"^[a-z]+(\([^)]*\))?!?:\s*", re.I)
TRAILING_PR_RE = re.compile(r"\s*\(#\d+\)\s*$")
LINK_LINE_RE = re.compile(r"^\s*(close[sd]?|fix(e[sd])?|resolve[sd]?|refs?)\b.*#\d+", re.I)
FRAG_NAME_RE = re.compile(r"^([0-9]+)-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


def questions() -> dict[str, dict]:
    return {
        "section": choice(
            "Which Keep a Changelog section describes what this merged pull request changes for "
            "someone who installs, operates, publishes to or consumes from the shipped product? "
            "Pick none_of_the_above only when nothing such a person receives changes.",
            {
                "Added": "a capability, endpoint, package format, option or integration that did not exist before",
                "Changed": "existing behaviour, defaults, output, limits or performance changed on purpose",
                "Deprecated": "something still works but is announced for removal",
                "Removed": "a capability, endpoint or option no longer exists",
                "Fixed": "incorrect behaviour, a crash, a regression, a wrong response or a data problem corrected",
                "Security": "a vulnerability, unsafe default, token or permission gap, or dependency advisory addressed",
                "none_of_the_above": "only CI, tests, internal refactoring, developer documentation or repository "
                                     "housekeeping changed: nothing a user of the product receives is different",
            },
        ),
        "user_visible": noul(
            "Does this change alter anything a person who runs the product or talks to it with a package "
            "manager would notice: behaviour, wire protocol, HTTP responses, configuration, packaging, "
            "security posture, or the documentation that ships with it?",
            true="a user of the shipped product could observe the difference",
            false="only contributors and CI observe the difference",
        ),
        "breaking": noul(
            "Would an operator or client have to change something when upgrading because of this change: "
            "a configuration key, an API shape, a default, or a migration with manual steps?",
            true="upgrading requires action or changes an expectation",
            false="upgrading is transparent",
        ),
    }


@dataclass
class Undocumented:
    pr: int
    subject: str
    detail: str = ""


@dataclass
class Preflight:
    run_id: int
    range: str = ""
    verdict: str = ""
    audited_ref: str = ""
    audited_sha: str = ""
    undocumented: list[Undocumented] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def parse_preflight(run_id: int, lines: list[str]) -> Preflight:
    pf = Preflight(run_id)
    mode = None
    for ln in lines:
        if UNDOC_HEAD in ln:
            mode = "undoc"
            m = RANGE_RE.search(ln)
            if m:
                pf.range = m.group(1) + "..HEAD"
            continue
        if FORWARD_HEAD in ln:
            mode = "forward"
            continue
        m = VERDICT_RE.search(ln)
        if m:
            pf.verdict, pf.audited_ref, pf.audited_sha = m.group(1), m.group(2), m.group(3)
            mode = None
            continue
        if mode is None:
            continue
        stripped = ln.strip()
        if stripped.startswith("->") or stripped.startswith("[") or not stripped:
            mode = None
            continue
        im = ITEM_RE.match(ln)
        if im:
            if mode == "undoc":
                pf.undocumented.append(Undocumented(int(im.group(1)), im.group(2).strip()))
            else:
                pf.unresolved.append(f"#{im.group(1)} {im.group(2).strip()}")
            continue
        dm = DETAIL_RE.match(ln)
        if dm and mode == "undoc" and pf.undocumented:
            pf.undocumented[-1].detail = dm.group(1).strip()
    return pf


# --- prose templating (deliberately mechanical) --------------------------------

def lead_sentence(title: str) -> str:
    t = TRAILING_PR_RE.sub("", CONVENTIONAL_RE.sub("", title.strip())).strip().rstrip(".")
    return (t[:1].upper() + t[1:]) if t else "Change"


def description_from_body(body: str) -> str:
    """First real paragraph of the PR body, lightly cleaned.

    Headings, link lines, checklists, tables and HTML comments are dropped;
    list items are kept as their own candidates rather than merged into a
    paragraph; bold markers are stripped. The first prose paragraph of at
    least 40 characters wins, a list item is the fallback. This is a draft
    seed for a human, not a summary.
    """
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    paras: list[str] = []
    items: list[str] = []
    cur: list[str] = []

    def flush():
        if cur:
            paras.append(" ".join(cur))
            cur.clear()

    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            flush()
            continue
        if s.startswith("#") or LINK_LINE_RE.match(s) or s.startswith("- [") or s.startswith("|"):
            flush()
            continue
        if re.match(r"^([-*+]|\d+[.)])\s+", s):
            flush()
            items.append(re.sub(r"^([-*+]|\d+[.)])\s+", "", s))
            continue
        cur.append(s)
    flush()

    def clean(p: str) -> str:
        p = p.replace("**", "").replace("__", "")
        p = re.sub(r"\s+", " ", p).strip()
        if len(p) > 400:
            cut = p[:400]
            end = max(cut.rfind(". "), cut.rfind("; "))
            p = (cut[: end + 1] if end > 120 else cut).strip()
        return p

    for p in paras + items:
        c = clean(p)
        if len(c) >= 40:
            return c
    return ""


def slugify(text: str, words: int = 6) -> str:
    parts = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(parts[:words]) or "change"


def render_fragment(section: str, lead_ref: int, pr: int, lead: str, desc: str, breaking: bool) -> tuple[str, str]:
    refs = [lead_ref] + ([pr] if pr != lead_ref else [])
    refs_txt = ", ".join(f"#{r}" for r in refs)
    desc = desc or ("What was wrong, why, and what changed. Drafted by ak-bot from the pull request; "
                    "rewrite this sentence before merging.")
    body = f"- **{lead}** ({refs_txt}). {desc}"
    if breaking:
        body += "\n\n  Upgrade note: this change may require operator action; confirm and describe it before release."
    text = f"---\nsection: {section}\nissues: [{refs_txt}]\n---\n{body}\n"
    name = f"{lead_ref}-{slugify(lead)}.md"
    return name, text


def validate_fragment(name: str, text: str) -> list[str]:
    """Mirror of scripts/ci/changelog-fragments.py's rules, enough to refuse a bad draft."""
    errs = []
    if not FRAG_NAME_RE.match(name):
        errs.append(f"bad file name {name}")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        errs.append("no front matter")
        return errs
    meta = dict(ln.split(":", 1) for ln in m.group(1).splitlines() if ":" in ln)
    meta = {k.strip(): v.strip() for k, v in meta.items()}
    if meta.get("section") not in SECTIONS:
        errs.append(f"section {meta.get('section')!r} invalid")
    issues = re.findall(r"#\d+", meta.get("issues", ""))
    if not issues:
        errs.append("no issues")
    body = m.group(2)
    for ref in issues:
        if ref not in body:
            errs.append(f"{ref} not cited in body")
    if len([ln for ln in body.splitlines() if ln.startswith("- ")]) != 1:
        errs.append("body must have exactly one top-level bullet")
    return errs


# --- decide -----------------------------------------------------------------------

@dataclass
class Draft:
    pr: int
    subject: str
    section: str
    section_p: float
    visible_p: float
    breaking_p: float
    tier: str
    name: str = ""
    text: str = ""
    note: str = ""


def decide_one(ctx: Context, item: Undocumented, existing_names: set[str]) -> Draft:
    pr = gh.pr_view(ctx.repo, item.pr)
    diff = gh.pr_diff(ctx.repo, item.pr)
    closes = [i["number"] for i in pr.get("closingIssuesReferences") or []]
    state = {
        "pull_request": {
            "number": pr["number"],
            "title": pr["title"],
            "body": redact((pr.get("body") or "")[:4000]),
            "author": (pr.get("author") or {}).get("login"),
            "labels": [l["name"] for l in pr.get("labels") or []],
            "closes_issues": closes,
            "changed_files": [f["path"] for f in (pr.get("files") or [])][:80],
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
        },
        "release_gate_note": item.detail or "the release gate found no CHANGELOG entry citing this PR",
        "diff_excerpt": redact(diff),
    }
    res = ctx.jev.ask(state, questions())
    sec, vis, brk = res["section"], res["user_visible"], res["breaking"]
    lead_ref = closes[0] if closes else item.pr
    d = Draft(item.pr, item.subject, sec.choice or "", sec.probabilities.get(sec.choice or "", 0.0),
              vis.noul or 0.0, brk.noul or 0.0, SKIP)

    if sec.choice == "none_of_the_above" or not vis.yes:
        d.tier = DRAFT_PR.tier(min(sec.certainty, vis.certainty))
        d.note = ("probably ships nothing to a user; the gate still needs either a fragment or the commit "
                  "reshaped into an exempt form, which is a human call")
        action = f"list as needs-human ({d.note.split(';')[0]})"
    elif any(n.startswith(f"{lead_ref}-") for n in existing_names):
        d.tier = SKIP
        d.note = f"a fragment leading with #{lead_ref} already exists; its lead reference may be wrong"
        action = "skip: fragment exists"
    else:
        d.tier = DRAFT_PR.tier(sec.certainty)
        if d.tier != SKIP:
            d.name, d.text = render_fragment(
                sec.choice, lead_ref, item.pr, lead_sentence(pr["title"]),
                description_from_body(pr.get("body") or ""), brk.yes and brk.certainty >= 0.6)
            errs = validate_fragment(d.name, d.text)
            if errs:
                d.tier, d.note = SKIP, "draft failed validation: " + "; ".join(errs)
        action = f"draft {d.name} as {sec.choice}" if d.name else f"list only ({d.tier})"
        if d.tier == "suggest" and d.name:
            d.note = "section confidence below the auto line; check the section"
    ctx.log.add(f"PR #{item.pr}", QUESTIONS_VERSION, res.model, res.answers, d.tier, action, url=pr.get("url", ""))
    return d


def pr_body(pf: Preflight, drafts: list[Draft], run_url: str) -> str:
    out = [f"Release preflight [run {pf.run_id}]({run_url}) reported `{pf.verdict}` for `{pf.audited_ref}@{pf.audited_sha[:10]}` "
           f"and listed merged PRs in `{pf.range}` that no pending CHANGELOG entry cites.",
           "",
           "Each fragment below is a **draft**: the section, user-visibility and breaking calls were made by JEV "
           "(probabilities shown); the bullet text was templated from the PR title and body. Edit the prose before merging.",
           "", "| PR | section (p) | user-visible (p) | breaking (p) | tier | fragment |", "|---|---|---|---|---|---|"]
    for d in drafts:
        if d.name:
            out.append(f"| #{d.pr} | {d.section} ({d.section_p:.2f}) | {d.visible_p:.2f} | {d.breaking_p:.2f} | {d.tier} | `{d.name}` |")
    needs = [d for d in drafts if not d.name]
    if needs:
        out += ["", "### Needs a human call", ""]
        for d in needs:
            out.append(f"- #{d.pr} `{d.subject[:80]}` — {d.section} ({d.section_p:.2f}), user-visible {d.visible_p:.2f}: {d.note}")
    if pf.unresolved:
        out += ["", "### Entries the gate could not reconcile (not drafted)", ""]
        out += [f"- {u}" for u in pf.unresolved]
    out += ["", "_Opened by ak-bot changelog-drafter._"]
    return "\n".join(out)


def run(ctx: Context, run_id: int | None = None) -> int:
    if run_id is None:
        runs = [r for r in gh.list_runs(ctx.repo, WORKFLOW, limit=10, branch="main")
                if r["status"] == "completed" and r["conclusion"] in ("success", "failure")]
        if not runs:
            print("no completed preflight run on main")
            return 0
        run_id = runs[0]["databaseId"]
        run_url = runs[0]["url"]
    else:
        run_url = f"https://github.com/{ctx.repo}/actions/runs/{run_id}"
    marker = f"changelog-{run_id}"
    if ctx.ledger.has(marker):
        print(f"run {run_id} already handled")
        return 0
    pf = parse_preflight(run_id, gh.run_log(ctx.repo, run_id))
    if not pf.undocumented:
        ctx.log.note(f"run {run_id}", f"verdict {pf.verdict or 'unknown'}; no undocumented PRs listed")
        return 0
    listing = gh.api(f"repos/{ctx.repo}/contents/changes/unreleased", check=False)
    existing = {x["name"] for x in listing} if isinstance(listing, list) else set()
    drafts = [decide_one(ctx, item, existing) for item in pf.undocumented]
    to_write = [d for d in drafts if d.name]
    if not to_write:
        ctx.log.note(f"run {run_id}", "nothing drafted; everything needs a human call")
        ctx.ledger.record(marker, f"changelog-drafter: run {run_id}, no drafts")
        return 0
    branch = f"ak-bot/changelog-{run_id}"
    title = f"docs(changelog): draft fragments for {len(to_write)} undocumented PR(s) in {pf.range}"
    body = pr_body(pf, drafts, run_url)
    labels = [l for l in ("no-issue-required", "automated", "release-process") if l in gh.repo_labels(ctx.repo)]
    if ctx.dry_run:
        for d in to_write:
            print(f"--- would write changes/unreleased/{d.name}\n{d.text}")
        print(f"--- would open PR on {branch}: {title}\n{body}")
        ctx.ledger.record(marker, f"changelog-drafter: dry run for {run_id}")
        return 0
    base = gh.branch_sha(ctx.repo, "main")
    if not base:
        raise RuntimeError("cannot resolve main")
    gh.ensure_branch(ctx.repo, branch, base)
    for d in to_write:
        gh.put_file(ctx.repo, branch, f"changes/unreleased/{d.name}", d.text,
                    f"docs(changelog): add fragment for #{d.pr} (ak-bot draft)")
    url = gh.open_pr(ctx.repo, branch, "main", title, body, labels)
    ctx.ledger.record(marker, f"changelog-drafter: opened {url} with {len(to_write)} fragment(s)")
    print(url)
    return 0
