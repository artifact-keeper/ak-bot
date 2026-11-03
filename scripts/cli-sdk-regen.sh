#!/usr/bin/env bash
# Keep artifact-keeper-cli's generated SDK and route table on the newest
# artifact-keeper-api tag, with artifact-keeper at the same tag for the route table.
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
BACKEND_REPO=artifact-keeper/artifact-keeper
WORK="${WORK:?WORK is required}"
CLI="$WORK/artifact-keeper-cli"
API="$WORK/artifact-keeper-api"
# `xtask routes` looks for the backend next to the CLI.
BACKEND="$WORK/artifact-keeper"
# What a PR changes in the CLI.
GENERATED=(API_VERSION sdk Cargo.lock ROUTES.json)

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
  cp ROUTES.json "$WORK/old-routes.json"

  printf '%s\n' "$version" > API_VERSION
  sed -i "0,/^version = .*/s//version = \"$version\"/" sdk/Cargo.toml

  if ! cargo run -q -p xtask -- generate > "$WORK/generate.log" 2>&1; then
    echo generate > "$WORK/result"
    tail -40 "$WORK/generate.log"
    return 0
  fi
  # Without this, the CLI's `routes --check` fails on every PR that moves the pin.
  if ! regenerate_routes "$tag" > "$WORK/routes.log" 2>&1; then
    echo routes > "$WORK/result"
    tail -40 "$WORK/routes.log"
    return 0
  fi
  # Also moves the SDK's version in Cargo.lock, which the CLI's MSRV job needs.
  if ! cargo check -q --workspace --message-format short > "$WORK/check.log" 2>&1; then
    echo check > "$WORK/result"
    tail -40 "$WORK/check.log"
    return 0
  fi
  # No --check: a new route with no command is for the PR to say, not a failed run.
  # It builds `ak`, so it comes after the check.
  if ! cargo run -q -p xtask -- coverage > "$WORK/coverage.log" 2>&1; then
    tail -40 "$WORK/coverage.log"
  fi
  echo ok > "$WORK/result"
}

regenerate_routes() {
  local tag="$1"
  git -c advice.detachedHead=false clone -q --depth 1 --filter=blob:none --sparse \
    --branch "$tag" "https://github.com/$BACKEND_REPO" "$BACKEND"
  git -C "$BACKEND" sparse-checkout set backend/src/api backend/src/formats
  cargo run -q -p xtask -- routes
  cargo run -q -p xtask -- routes --check
}

# The routes the tag adds and removes against the pin's ROUTES.json, and what the
# CLI's coverage gate says about this output. The gate is clean on main, so any
# problem here comes from this regeneration.
coverage_diff() {
  local result="$1" routes='.routes[] | "\(.method) \(.path)"'
  if [ "$result" = routes ]; then
    echo "**Routes:** not read, because \`xtask routes\` failed, so \`ROUTES.json\` is still the old one."
    return 0
  fi
  jq -r "$routes" "$WORK/old-routes.json" | sort > "$WORK/old.routes"
  jq -r "$routes" "$CLI/ROUTES.json" | sort > "$WORK/new.routes"
  list_routes() {
    local found
    found="$(comm "$1" "$WORK/old.routes" "$WORK/new.routes" | sed 's/^/- `/; s/$/`/')"
    echo "${found:-None.}" | sed -n '1,200p'
  }
  echo "**Routes added:**"; echo; list_routes -13; echo
  echo "**Routes removed:**"; echo; list_routes -23; echo
  if [ "$result" != ok ]; then
    echo "**Coverage gate:** not run, because the CLI doesn't compile."
  elif grep -q '^Coverage gate: OK' "$WORK/coverage.log"; then
    echo "**Coverage gate:** OK."
  elif grep -q '^## Problems' "$WORK/coverage.log"; then
    echo "**Coverage gate:** fails until each of these is dealt with. A new route needs a command in \`src/registry.rs\`, or an exclusion with a reason, which this workflow never adds."
    echo
    sed -n '/^## Problems/,$p' "$WORK/coverage.log" | sed -n '2,200p' | cut -c1-300
  else
    echo "**Coverage gate:** didn't run. See the workflow run."
  fi
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
  case "$result" in
    generate) log="$WORK/generate.log" ;;
    routes) log="$WORK/routes.log" ;;
    *) log="$WORK/check.log" ;;
  esac

  if [ "$result" = ok ] && git diff --quiet -- "${GENERATED[@]}"; then
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
    if [ "$result" != generate ]; then
      coverage_diff "$result" | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
    fi
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
  git add "${GENERATED[@]}"
  git commit -q -m "chore(sdk): regenerate from API $version"
  local generated
  generated="$(git rev-parse HEAD)"
  if git fetch -q origin "$branch:refs/remotes/origin/$branch" 2>/dev/null; then
    git checkout -q -B "$branch" "origin/$branch"
    git checkout -q "$generated" -- "${GENERATED[@]}"
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

$(case "$result" in
  ok) echo "\`cargo check --workspace\` passes on this output." ;;
  routes) printf 'This is a draft because `xtask routes` fails, so `ROUTES.json` is still the old one and `routes --check` will fail. `cargo check` was not run. Closes #%s when merged.\n\n```\n%s\n```' \
    "$issue" "$(tail -20 "$log" | cut -c1-300)" ;;
  *) printf 'This is a draft because `cargo check --workspace` fails, so the commands need changes for the new SDK. Closes #%s when merged.\n\n```\n%s\n```' \
    "$issue" "$(grep -E '^(error|[^ ]+: error)' "$log" | head -40 | cut -c1-300)" ;;
esac)

$(operation_diff "$pin")

$(coverage_diff "$result")

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
