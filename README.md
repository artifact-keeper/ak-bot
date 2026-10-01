# ak-bot

<img src="assets/avatar.png" alt="Jev, the ak-bot avatar" width="160" align="right">

Decision bots for the [artifact-keeper](https://github.com/artifact-keeper) org, powered by
TypeSafe's JEV (System One) model. JEV returns typed decisions with probabilities, not text;
the bots turn those into labels, comments, drafted PRs and reruns, and never into merges,
closes or release gates.

## What JEV can and cannot be tuned on

There are no priors, weights or temperature. Each call is `POST /v1/systemone` with a `state`
blob and a map of named questions (`choice`, `score`, `noul`). The things that change the
probabilities are the **state** we pass, the **criteria** wording, the **instructions**, and
the **thresholds** we apply to the returned `probabilities` and `confidence`. Question
definitions live next to each bot with a `QUESTIONS_VERSION` string that is written into every
decision record, so a wording change is a versioned change.

## Bots

| bot | trigger | what JEV decides | auto tier | suggest tier |
|---|---|---|---|---|
| `changelog-drafter` | after Release Preflight | section, user-visible, breaking per undocumented PR | fragment drafted into one PR | drafted and flagged |
| `red-run-classifier` | every 3h | cause, rerun-would-pass, severity per red run | rerun a flake once; issue for a regression | digest comment |
| `issue-triage` | every 6h | type, area, priority, regression per untriaged issue | `type:*`, `registry/*`, `regression` labels | one proposal comment |
| `base-image-bump` | after the weekly watch | urgency, remedy, auto-PR-safe | repoint PR or upstream override request | comment on tracker |
| `pr-review-router` | every 4h | readiness, blocker, risk per external PR | `needs-maintainer-review` + comment | blocker comment |
| `preflight-audit` | after Release Preflight | verdict supported, scope confusion, weakened by skips | (deterministic mismatch opens an issue) | digest comment |

Below the suggest line every bot only records the decision. Thresholds are in
`akbot/decisions.py`, ordered by stakes: labels 0.85/0.60, comments 0.80/0.55, drafted PRs
0.85/0.60, issues 0.85/0.65, reruns 0.90/0.75.

## Idempotence and audit

Every action is recorded as a comment on an `ak-bot ledger` issue in the target repo with an
`ak-bot-id:` marker; bots read that thread before acting, so reruns never repeat. Every JEV
answer, tier and action is also written to `decisions/*.jsonl` and uploaded as a workflow
artifact. That file is what you calibrate from: bin by stated probability, compare with what a
human did afterwards, move the thresholds.

## Running

```
pip install -e .
export GH_TOKEN=...            # read-only is enough for --dry-run
export TYPESAFE_API_KEY=...    # optional with --dry-run (FakeJev: everything lands in skip)
akbot changelog-drafter --dry-run
akbot red-run-classifier --dry-run --since-hours 48
akbot issue-triage --dry-run --limit 5
akbot base-image-bump --dry-run
akbot pr-review-router --dry-run --limit 3
akbot preflight-audit --dry-run --limit 2
```

## Identity and going live

The workflows accept either a **GitHub App** (secrets `AK_BOT_APP_ID` and
`AK_BOT_APP_PRIVATE_KEY`; actions show as `<app>[bot]`) or a **token** (secret `AK_BOT_TOKEN`,
a fine-grained PAT of a machine user). With neither, `github.token` is used, which cannot write
to other repos, so every run is forced to dry-run.

Permissions the identity needs on the target repos: Contents read/write (fragment and repoint
branches), Issues read/write, Pull requests read/write, Actions read/write (reruns), Metadata read.
Install it on `artifact-keeper/artifact-keeper` and `artifact-keeper/trivy` (upstream override
requests).

A scheduled run is live only when the repository variable `AK_BOT_LIVE` names the bot
(comma-separated, e.g. `issue-triage,pr-review-router`) or is `all`. A manual dispatch honours
its `dry_run` input regardless. Until a bot is listed, its schedule keeps running dry and the
decision artifacts still accumulate, which is the intended first stage: log, read, then enable.

## Rollout order

1. Dry runs on schedule for a week; read the decision artifacts.
2. Flip live for `issue-triage` and `pr-review-router` (cheap to undo).
3. Then `changelog-drafter` and `preflight-audit` (they open PRs and issues you review anyway).
4. Then `red-run-classifier` and `base-image-bump`, after checking the flake calls against reruns you did by hand.
