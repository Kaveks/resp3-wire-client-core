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
    storageMb            4096
    gpuCount             0
    agentTimeoutSec      14400   (4h; floor is 7200)
    verifierTimeoutSec   900

Memory stays at 8 GB because channel 4 feeds a 64 MB payload under `tracemalloc`
across 3 trials, and the deep-nesting case builds a frame past CPython's
recursion limit. Storage drops to 4 GB: the built image measures 203 MB, so 4 GB
carries the image, both virtualenvs, and the agent's own working files with a
wide margin.

The verifier timeout drops to 900 s. A measured offline run takes 31.1 s, so 900
is a 29x margin against the observed figure, which covers a loaded sandbox
without reserving time nothing will use.

Measured on 2026-08-19:

    cold build, --no-cache    119.9 s
    offline verifier run       31.1 s
    image size                  203 MB

Build plus a 4-hour rollout plus verification sits far inside the 50,400 s
per-trial ceiling. All figures are under the 8 CPU / 65536 MB / 40960 MB sandbox
limits, and `task.toml` must request these or less, never more.

## Unconfirmed

Score emission. The Harbor format's mechanism for a continuous score from
`tests/test.sh` is not yet verified. `run.py` will emit both a pytest exit code
and a JSON score file until this is confirmed. Channel weights are realized
through case counts (65/26/26/13 of 130, the 50/20/20/10 ratios scaled per D19), which degrades correctly under either
mechanism.

## Prose fields

Not yet written. They are drafted after the implementation exists, so that
`difficultyExplanation` and `verificationStrategy` describe what was built
rather than what was planned.
