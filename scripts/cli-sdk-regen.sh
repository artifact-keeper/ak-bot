#!/usr/bin/env bash
# Keep artifact-keeper-cli's generated SDK on the newest artifact-keeper-api tag.
#
#   poll      Is there a tag to regenerate from? Writes tag= to GITHUB_OUTPUT.
#   generate  Regenerate in a clone. Runs the CLI's build, so it gets no token.
#   publish   Open or update the PR, or the issue if generation failed.
#
# The App has no workflows permission and this never changes .github/workflows.
# So a branch is only ever added to, never rebuilt on a newer main: that would
# put main's workflow changes in the push, and GitHub would refuse it.
#
# Environment: WORK (directory shared by the steps), GH_TOKEN (poll, publish),
# TAG (optional, a tag to use instead of the newest), DRY_RUN (publish),
# MAINTAINER (publish, optional assignee of the issue).
set -euo pipefail

CLI_REPO=artifact-keeper/artifact-keeper-cli
API_REPO=artifact-keeper/artifact-keeper-api
WORK="${WORK:?WORK is required}"
CLI="$WORK/artifact-keeper-cli"
API="$WORK/artifact-keeper-api"

branch_for() { echo "chore/sdk-regen-$1"; }

poll() {
  local pin newest tag closed
  pin="$(gh api "repos/$CLI_REPO/contents/API_VERSION" -H 'Accept: application/vnd.github.raw' | tr -d '[:space:]')"
  newest="$(gh api --paginate "repos/$API_REPO/tags" --jq '.[].name' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
  echo "API_VERSION is $pin, the newest API tag is $newest"

  tag="${TAG:-}"
  if [ -n "$tag" ]; then
    gh api "repos/$API_REPO/git/ref/tags/$tag" --silent
  else
    if [ "$(printf '%s\n%s\n' "v$pin" "$newest" | sort -V | tail -1)" = "v$pin" ]; then
      echo "Nothing to do"
      return 0
    fi
    # A person closed this PR without merging: that's an answer, so don't reopen it.
    # A manual dispatch with TAG can still force it.
    closed="$(gh pr list -R "$CLI_REPO" --head "$(branch_for "$newest")" --state closed \
      --json number --jq '.[0].number // empty')"
    if [ -n "$closed" ]; then
      echo "::notice::#$closed for $newest was closed; leaving it closed"
      return 0
    fi
    tag="$newest"
  fi
  echo "tag=$tag" >> "$GITHUB_OUTPUT"
  echo "pin=$pin" >> "$GITHUB_OUTPUT"
}

generate() {
  local tag="${TAG:?TAG is required}" version="${TAG#v}"
  mkdir -p "$WORK"
  # The logs go into the PR and the issue, and the toolchain action turns color on.
  export CARGO_TERM_COLOR=never
  git clone -q --filter=blob:none "https://github.com/$CLI_REPO" "$CLI"
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$tag" "https://github.com/$API_REPO" "$API"
  cd "$CLI"

  printf '%s\n' "$version" > API_VERSION
  sed -i "0,/^version = .*/s//version = \"$version\"/" sdk/Cargo.toml

  if ! cargo run -q -p xtask -- generate > "$WORK/generate.log" 2>&1; then
    echo generate > "$WORK/result"
    tail -40 "$WORK/generate.log"
    return 0
  fi
  # Also moves the SDK's version in Cargo.lock, which the CLI's MSRV job needs.
  if ! cargo check -q --workspace --message-format short > "$WORK/check.log" 2>&1; then
    echo check > "$WORK/result"
    tail -40 "$WORK/check.log"
    return 0
  fi
  echo ok > "$WORK/result"
}

# The operations the tag adds and removes, against the spec of the current pin.
operation_diff() {
  local pin="$1" old="$WORK/old-spec.json" new="$API/openapi.json"
  local ops='.paths | to_entries[] | .key as $p | .value | keys[]
    | select(IN("get","put","post","delete","patch","head","options"))
    | "\(ascii_upcase) \($p)"'
  gh api "repos/$API_REPO/contents/openapi.json?ref=v$pin" -H 'Accept: application/vnd.github.raw' > "$old"
  jq -r "$ops" "$old" | sort > "$WORK/old.ops"
  jq -r "$ops" "$new" | sort > "$WORK/new.ops"
  list_ops() {
    local ops
    ops="$(comm "$1" "$WORK/old.ops" "$WORK/new.ops" | sed 's/^/- `/; s/$/`/')"
    echo "${ops:-None.}" | sed -n '1,200p'
  }
  echo "**Operations added:**"; echo; list_ops -13; echo
  echo "**Operations removed:**"; echo; list_ops -23
}

publish() {
  local tag="${TAG:?TAG is required}" pin="${PIN:?PIN is required}" version="${TAG#v}"
  local result branch title issue_title body issue="" pr bot_id log
  result="$(cat "$WORK/result")"
  branch="$(branch_for "$tag")"
  title="chore(sdk): regenerate from API $version"
  issue_title="SDK regen to $tag needs manual fixes"

  cd "$CLI"
  if [ "$result" = generate ]; then
    log="$WORK/generate.log"
  else
    log="$WORK/check.log"
  fi

  if [ "$result" = ok ] && git diff --quiet -- API_VERSION sdk Cargo.lock; then
    echo "::notice::Regenerating from $tag changes nothing"
    return 0
  fi
  if [ -n "$(git status --porcelain -- .github/workflows)" ]; then
    echo "::error::The regeneration changed .github/workflows, which this must never do"
    git status --porcelain -- .github/workflows
    return 1
  fi
  if [ "${DRY_RUN:-true}" != false ]; then
    echo "Dry run: result=$result; would open or update '$title' from $branch"
    git status --short
    return 0
  fi

  if [ "$result" != ok ]; then
    local issue_body
    issue_body="Regenerating the SDK from \`$tag\` failed at \`$result\`.

\`\`\`
$(tail -60 "$log" | cut -c1-300)
\`\`\`

Workflow run: ${RUN_URL:-unknown}"
    issue="$(ISSUE_TITLE="$issue_title" gh issue list -R "$CLI_REPO" --state open \
      --search "\"$issue_title\" in:title" --json number,title \
      --jq '.[] | select(.title == env.ISSUE_TITLE) | .number' | head -1)"
    if [ -n "$issue" ]; then
      gh issue comment -R "$CLI_REPO" "$issue" --body "$issue_body"
    else
      issue="$(gh issue create -R "$CLI_REPO" --title "$issue_title" --body "$issue_body" \
        ${MAINTAINER:+--assignee "$MAINTAINER"})"
      issue="${issue##*/}"
    fi
    # Nothing was generated, so there's nothing to review in a PR.
    if [ "$result" = generate ]; then
      return 0
    fi
  fi

  gh auth setup-git
  bot_id="$(gh api "users/${BOT_SLUG:?BOT_SLUG is required}%5Bbot%5D" --jq .id)"
  git config user.name "${BOT_SLUG}[bot]"
  git config user.email "$bot_id+${BOT_SLUG}[bot]@users.noreply.github.com"

  # Save the generated files, then put them on the branch: a new one from main,
  # or the existing one with a commit on top.
  git add API_VERSION sdk Cargo.lock
  git commit -q -m "chore(sdk): regenerate from API $version"
  local generated
  generated="$(git rev-parse HEAD)"
  if git fetch -q origin "$branch:refs/remotes/origin/$branch" 2>/dev/null; then
    git checkout -q -B "$branch" "origin/$branch"
    git checkout -q "$generated" -- API_VERSION sdk Cargo.lock
    if git diff --cached --quiet; then
      echo "The branch already has this content"
    else
      git commit -q -m "chore(sdk): regenerate from API $version"
      git push -q origin "$branch"
    fi
  else
    git checkout -q -B "$branch"
    git push -q origin "$branch"
  fi

  body="Regenerates the SDK from \`$tag\` (from \`v$pin\`). Opened by the \`cli-sdk-regen\` workflow in artifact-keeper/ak-bot, which adds to this branch if the output changes. It never changes \`.github/workflows\`.

$(if [ "$result" = ok ]; then echo "\`cargo check --workspace\` passes on this output."; else
  printf 'This is a draft because `cargo check --workspace` fails, so the commands need changes for the new SDK. Closes #%s when merged.\n\n```\n%s\n```' \
    "$issue" "$(grep -E '^(error|[^ ]+: error)' "$log" | head -40 | cut -c1-300)"; fi)

$(operation_diff "$pin")

Before merging: if the minor changed, update \`NOT_REQUIRED_ON_PREVIOUS_MINOR\` in xtask and \`src/registry.rs\`, and bump the CLI version."

  gh label create sdk-regen -R "$CLI_REPO" --force --color 0e8a16 --description "Opened by the SDK regeneration workflow"
  gh label create needs-human -R "$CLI_REPO" --force --color d93f0b --description "The bot can't finish this"

  pr="$(gh pr list -R "$CLI_REPO" --head "$branch" --state open --json number --jq '.[0].number // empty')"
  if [ -z "$pr" ]; then
    local draft=()
    if [ "$result" != ok ]; then draft=(--draft); fi
    pr="$(gh pr create -R "$CLI_REPO" --base main --head "$branch" --title "$title" --body "$body" \
      --label sdk-regen "${draft[@]}")"
    pr="${pr##*/}"
  else
    gh pr edit -R "$CLI_REPO" "$pr" --body "$body" --add-label sdk-regen
    if [ "$result" = ok ]; then
      gh pr ready -R "$CLI_REPO" "$pr" 2>/dev/null || true
    else
      gh pr ready --undo -R "$CLI_REPO" "$pr" 2>/dev/null || true
    fi
  fi
  if [ "$result" = ok ]; then
    gh pr edit -R "$CLI_REPO" "$pr" --remove-label needs-human
  else
    gh pr edit -R "$CLI_REPO" "$pr" --add-label needs-human
  fi
  echo "::notice::PR: https://github.com/$CLI_REPO/pull/$pr (result=$result)"
}

"${1:?usage: $0 <poll|generate|publish>}"
