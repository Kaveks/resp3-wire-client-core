# resp3-wire-client-core

A Redis client library implemented directly on the wire protocol using only
the Python standard library, plus a scoring test harness that grades an
implementation across four independent channels.

## Layout

    docs/           design contracts, frozen once ratified
    spec/           the task specification handed to an implementer
    reference/      the reference implementation
    starter/        stubs an implementer starts from
    harness/        scoring harness, four channels plus orchestrator
    visible_tests/  subset shipped alongside the starter
    attacks/        deliberate exploit attempts against the harness
    tools/          development scripts
    environment/    Dockerfile for the shipped image
    build/          generated output, not tracked

## Development

    source venv/bin/activate
    ./tools/redis_dev.sh up
    python tools/check_stdlib_only.py

Local Redis runs in Docker on port 6399. The shipped image bakes in its own
server because it cannot use compose and has no runtime network.
