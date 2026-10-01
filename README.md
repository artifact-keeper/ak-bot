# ak-bot

<img src="assets/avatar.png" alt="Jev, the ak-bot avatar" width="160" align="right">

Decision bots for the [artifact-keeper](https://github.com/artifact-keeper) org, powered by
TypeSafe's JEV (System One) model. JEV returns typed decisions with probabilities, not text;
the bots turn those into labels, comments, drafted PRs, issues and reruns, and never into
merges, closes, approvals or release gates.

## What JEV can and cannot be tuned on

There are no priors, weights or temperature. Each call is `POST /v1/systemone` with a `state`
blob and a map of named questions (`choice`, `score`, `noul`). What changes the probabilities
is the **state** we pass, the **criteria** wording, the **instructions**, and the
**thresholds** we apply to the answers. Question definitions live next to each bot with a
`QUESTIONS_VERSION`; `tests/test_questions_lock.py` refuses a reworded question whose version
was not bumped, and the version plus a hash of the wording is written into every decision record.

## Bots

| bot | schedule (UTC) | what JEV decides | auto tier | suggest tier |
|---|---|---|---|---|
| `issue-triage` | every 6h | type, area, priority, regression per untriaged issue | `type:*`, `registry/*`, `regression` labels | one proposal comment (type/area only; priority is never commented) |
| `pr-review-router` | every 4h | readiness, blocker, risk per external PR | `needs-maintainer-review` + comment | blocker comment, edited in place on later pushes |
| `changelog-drafter` | 21:00 weekdays + 03:00 catch-up | section, user-visible, breaking per undocumented PR | fragments drafted into one PR | drafted and flagged |
| `preflight-audit` | 21:30 weekdays + 03:30 catch-up | verdict supported, scope confusion, weakened by skips | (a deterministic evidence mismatch opens an issue) | digest comment |
| `red-run-classifier` | every 3h, 8h lookback | cause, rerun-would-pass, severity per red run | rerun an allowlisted flake once; issue per (workflow, branch) for a regression | digest comment |
| `base-image-bump` | Wed 15:00 + 21:00 | urgency, remedy, auto-PR-safe | repoint PR, or upstream override request | comment on the tracker |

The schedules bracket when the upstream workflows actually finish (Release Preflight completes
17:30 to 20:10 UTC despite its 13:17 cron; the base image watch 12:00 to 13:30 Wednesdays).
A second slot costs nothing: a run with nothing new makes no JEV call.

**Routing.** Every answer is reduced to one 0 to 1 number: for a `choice` the probability JEV
put on the option it chose; for a `score` the mass on the rubric levels that matter
(`mass_at_least`); for a `noul` the distance from 0.5. Tiers in `akbot/decisions.py`, by stakes:

| action | auto | suggest |
|---|---|---|
| label | 0.85 | 0.60 |
| comment on a bot-owned digest | 0.80 | 0.55 |
| comment addressed to a contributor | 0.90 | 0.70 |
| draft PR, open issue | 0.85 | 0.60 / 0.65 |
| rerun | 0.90 | 0.75 |

Below the suggest line a bot only records. Reruns are additionally limited to an allowlist of
test workflows; publish and release workflows are never rerun by the bot.

## Memory, audit, undo

- **Ledger.** Every live action is one comment on `ak-bot ledger YYYY-MM` in the target repo
  with an `ak-bot-id` marker and the run id. Bots read this and last month's ledger (open or
  closed) before acting, so reruns never repeat; older ledgers are closed. Delete a comment to
  make its subject eligible again.
- **Decision records.** Every run writes `decisions/*.jsonl` (answers, tier, action, structured
  effects, JEV usage) and commits it to the `records` branch of this repo, plus a workflow
  artifact. The trailing `kind: run` line is the heartbeat and the usage line.
- **Undo a run.** `akbot undo --run-id <ak-bot run id> [--dry-run]` removes the labels, deletes
  the comments, retracts and closes anything that run opened, and forgets its ledger entries.
- **Calibrate.** `python3 scripts/calibrate.py` bins records older than ten days by stated
  probability and compares with what humans did (label still present, PR merged, rerun passed).
  Move a threshold only from that output. Thumbs-down on a bot comment is the cheapest signal;
  please use it.

## Running locally

```
pip install -e .
export GH_TOKEN=...            # read-only is enough for --dry-run
export TYPESAFE_API_KEY=...    # optional with --dry-run (fake model: everything skips)
akbot issue-triage --dry-run --limit 5
akbot red-run-classifier --dry-run --since-hours 48
akbot preflight-audit --dry-run --limit 2
akbot watchdog --dry-run
```

Only repos in `AK_BOT_ALLOWED_REPOS` (default: artifact-keeper and trivy) are accepted. Each
bot has a per-run JEV call cap (`--max-jev-calls`, `AK_BOT_MAX_JEV_CALLS`); the sum of caps
times runs per day is asserted under 400 by `tests/test_budget.py`.

## Identity and going live

The workflows accept a **GitHub App** (secrets `AK_BOT_APP_ID`, `AK_BOT_APP_PRIVATE_KEY`;
`scripts/create-github-app.py` creates one through the manifest flow) or a **token**
(`AK_BOT_TOKEN`). Each workflow mints a token scoped to exactly the repositories and
permissions that bot needs. Install the App on `artifact-keeper` and `trivy` only. With neither
secret, `github.token` is used and every run is forced dry.

Repository variables on ak-bot:

| variable | meaning |
|---|---|
| `AK_BOT_LIVE` | comma-separated bot names whose *scheduled* runs may write, or `all`. Unset: everything dry. |
| `AK_BOT_HALT` | any value: no writes and no JEV calls on the next run of every bot. The kill switch. |
| `AK_BOT_MAINTAINER` | GitHub login the watchdog assigns its issue to. |
| `AK_BOT_MAX_JEV_CALLS` | optional global per-run cap override. |
| `TYPESAFE_MODEL` | pin a dated model id instead of `jev-latest` once calibrated. |

A manual dispatch honours its own `dry_run` input regardless of `AK_BOT_LIVE`.

**Stopping.** Soft: `gh variable set AK_BOT_HALT -R artifact-keeper/ak-bot --body "why"`.
Hard: `gh workflow disable -R artifact-keeper/ak-bot <bot>` for each bot. An in-flight run can
still write for up to 20 minutes.

**Watchdog.** A daily workflow re-enables disabled schedules and maintains one `ak-bot watchdog`
issue in this repo: three failed runs in a row, a bot with no run record within twice its
cadence, or a bot listed in `AK_BOT_LIVE` whose last run was dry or on the fake model. It
closes the issue when everything passes. A bot workflow that fails also files or updates an
`ak-bot: <bot> failed` issue here.

## Rollout order

1. Secrets in, `AK_BOT_LIVE` unset: schedules run dry with the real model. Read the records.
2. One supervised live dispatch of `issue-triage` with `limit=5`; check the labels and the ledger.
3. `AK_BOT_LIVE=issue-triage`, then add `pr-review-router`.
4. `changelog-drafter` and `preflight-audit` (they open PRs and issues you review anyway).
5. `red-run-classifier` and `base-image-bump` last, after `scripts/calibrate.py` shows the
   flake calls agreeing with reruns you did by hand.

## Changing a question

Bump the bot's `QUESTIONS_VERSION`, run `RELOCK=1 python -m unittest tests.test_questions_lock`,
remove the bot from `AK_BOT_LIVE` until the new version has enough scored records, and note the
change below. Thresholds were earned by the previous wording; they are not inherited.

| version | date | why |
|---|---|---|
| `*/q1` | 2026-09-30 | initial wording |
