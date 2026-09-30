"""Everything that touches GitHub goes through the `gh` CLI.

Read calls work with any token. Write calls (branches, files, PRs, labels,
comments) need a token with write access to the target repo: in Actions that
is the `AK_BOT_TOKEN` secret, exported as GH_TOKEN by each workflow.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from typing import Any

LOG_PREFIX_RE = re.compile(r"^[^\t]*\t[^\t]*\t\d{4}-\d{2}-\d{2}T[0-9:.]+Z ?")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class GhError(RuntimeError):
    pass


def gh(*args: str, input: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(["gh", *args], input=input, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])}... failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    return proc.stdout


def gh_json(*args: str) -> Any:
    out = gh(*args)
    return json.loads(out) if out.strip() else None


def api(path: str, method: str = "GET", body: dict | None = None, jq: str | None = None,
        paginate: bool = False, check: bool = True) -> Any:
    args = ["api", path, "-X", method, "-H", "Accept: application/vnd.github+json"]
    if paginate:
        args += ["--paginate", "--slurp"]
    if jq:
        args += ["--jq", jq]
    stdin = None
    if body is not None:
        args += ["--input", "-"]
        stdin = json.dumps(body)
    out = gh(*args, input=stdin, check=check)
    if not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out
    if paginate and isinstance(data, list) and data and isinstance(data[0], list):
        data = [x for page in data for x in page]
    return data


# --- workflow runs ------------------------------------------------------------

def list_runs(repo: str, workflow: str | None = None, limit: int = 30, **filters: str) -> list[dict]:
    args = ["run", "list", "-R", repo, "--limit", str(limit), "--json",
            "databaseId,name,workflowName,conclusion,status,event,headBranch,headSha,createdAt,updatedAt,url"]
    if workflow:
        args += ["--workflow", workflow]
    for k, v in filters.items():
        args += [f"--{k}", v]
    return gh_json(*args) or []


def run_jobs(repo: str, run_id: int) -> list[dict]:
    data = gh_json("run", "view", "-R", repo, str(run_id), "--json", "jobs")
    return (data or {}).get("jobs", [])


def run_log(repo: str, run_id: int, failed_only: bool = False) -> list[str]:
    """Log lines with the `job<TAB>step<TAB>timestamp ` prefix and ANSI removed."""
    flag = "--log-failed" if failed_only else "--log"
    raw = gh("run", "view", "-R", repo, str(run_id), flag, check=False)
    return [ANSI_RE.sub("", LOG_PREFIX_RE.sub("", ln)).rstrip() for ln in raw.splitlines()]


def run_artifacts(repo: str, run_id: int) -> list[dict]:
    data = api(f"repos/{repo}/actions/runs/{run_id}/artifacts")
    return (data or {}).get("artifacts", [])


def rerun_failed(repo: str, run_id: int) -> None:
    gh("run", "rerun", "-R", repo, str(run_id), "--failed")


# --- issues and PRs -----------------------------------------------------------

def list_issues(repo: str, limit: int = 200, state: str = "open") -> list[dict]:
    return gh_json("issue", "list", "-R", repo, "--state", state, "--limit", str(limit), "--json",
                   "number,title,body,labels,createdAt,updatedAt,author,url") or []


def issue_search(repo: str, query: str, limit: int = 50) -> list[dict]:
    return gh_json("issue", "list", "-R", repo, "--search", query, "--state", "all",
                   "--limit", str(limit), "--json", "number,title,state,labels,url") or []


def list_prs(repo: str, limit: int = 100) -> list[dict]:
    return gh_json("pr", "list", "-R", repo, "--state", "open", "--limit", str(limit), "--json",
                   "number,title,body,author,isDraft,labels,createdAt,updatedAt,additions,deletions,"
                   "changedFiles,files,reviewDecision,mergeable,statusCheckRollup,headRefOid,url") or []


def pr_view(repo: str, number: int) -> dict:
    return gh_json("pr", "view", "-R", repo, str(number), "--json",
                   "number,title,body,author,labels,files,closingIssuesReferences,mergedAt,mergeCommit,url,"
                   "additions,deletions,changedFiles")


def pr_diff(repo: str, number: int, max_chars: int = 12000) -> str:
    text = gh("pr", "diff", "-R", repo, str(number), check=False)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [diff truncated at {max_chars} chars]"
    return text


def issue_comments(repo: str, number: int) -> list[dict]:
    return api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True) or []


def comment(repo: str, number: int, body: str) -> None:
    gh("issue", "comment", "-R", repo, str(number), "--body", body)


def add_labels(repo: str, number: int, labels: list[str]) -> None:
    if labels:
        gh("issue", "edit", "-R", repo, str(number), "--add-label", ",".join(labels))


def create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    args = ["issue", "create", "-R", repo, "--title", title, "--body", body]
    if labels:
        args += ["--label", ",".join(labels)]
    return gh(*args).strip()


def repo_labels(repo: str) -> set[str]:
    return {l["name"] for l in (gh_json("label", "list", "-R", repo, "--limit", "200", "--json", "name") or [])}


def org_members(org: str) -> set[str]:
    try:
        return {m["login"] for m in (api(f"orgs/{org}/members?per_page=100", paginate=True) or [])}
    except GhError:
        return set()


# --- branches, files, PRs through the contents API (no clone needed) ----------

def branch_sha(repo: str, branch: str) -> str | None:
    data = api(f"repos/{repo}/git/ref/heads/{branch}", check=False)
    if isinstance(data, dict) and "object" in data:
        return data["object"]["sha"]
    return None


def ensure_branch(repo: str, branch: str, from_sha: str) -> bool:
    """Create `branch` at `from_sha`; return False when it already exists."""
    if branch_sha(repo, branch):
        return False
    api(f"repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": from_sha})
    return True


def get_file(repo: str, path: str, ref: str) -> tuple[str, str] | None:
    data = api(f"repos/{repo}/contents/{path}?ref={ref}", check=False)
    if isinstance(data, dict) and data.get("type") == "file":
        return base64.b64decode(data["content"]).decode(), data["sha"]
    return None


def put_file(repo: str, branch: str, path: str, content: str, message: str) -> None:
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    existing = get_file(repo, path, branch)
    if existing:
        body["sha"] = existing[1]
    api(f"repos/{repo}/contents/{path}", "PUT", body)


def open_pr(repo: str, head: str, base: str, title: str, body: str, labels: list[str]) -> str:
    args = ["pr", "create", "-R", repo, "--head", head, "--base", base, "--title", title, "--body", body]
    if labels:
        args += ["--label", ",".join(labels)]
    return gh(*args).strip()
