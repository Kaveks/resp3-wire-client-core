# Submission metadata

Single source of truth for the draft form. `task.toml` is derived from this
file. Never edit either independently.

Status: figures provisional until step 9 measures build and verifier time.

    title                    RESP3 wire client core
    workingSlug              resp3-wire-client-core
    collectionFamily         Library clone
    taskFamily               feature_development
    verifierFamily           programmatic
    networkRequirements      none

## resourceEstimate

    cpuMillis            4000
    memoryMb             8192
    storageMb            8192
    gpuCount             0
    agentTimeoutSec      14400   (4h; floor is 7200)
    verifierTimeoutSec   1800

Memory is set at 8 GB because channel 4 feeds a 64 MB payload under
`tracemalloc` across 3 trials. Storage covers the image plus both virtualenvs.
All figures sit well under the 8 CPU / 65536 MB / 40960 MB ceiling, and
`task.toml` must request these or less, never more.

## Unconfirmed

Score emission. The Harbor format's mechanism for a continuous score from
`tests/test.sh` is not yet verified. `run.py` will emit both a pytest exit code
and a JSON score file until this is confirmed. Channel weights are realized
through case counts (50/20/20/10 of 100), which degrades correctly under either
mechanism.

## Prose fields

Not yet written. They are drafted after the implementation exists, so that
`difficultyExplanation` and `verificationStrategy` describe what was built
rather than what was planned.
