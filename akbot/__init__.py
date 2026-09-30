"""ak-bot: probabilistic decision bots for artifact-keeper, powered by TypeSafe's JEV.

Every bot follows the same shape:

    collect()  -> gather state through `gh` (read-only)
    decide()   -> one JEV call per subject, pure, testable with FakeJev
    act()      -> take the tier-appropriate action through `gh`, or print in dry-run

JEV never gates anything. It answers "what should we do about this?" with a
probability distribution; the caller-side thresholds in `akbot.decisions`
decide whether that answer is acted on, suggested, or merely logged.
"""
