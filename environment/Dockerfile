# syntax=docker/dockerfile:1
#
# The shipped image.
#
# Everything the run needs is baked in at build time, because the rollout and
# verification phases both have no network. That includes redis-server, both
# interpreters, and every Python dependency.
#
# The image carries the environment, not the grading. The sealed harness arrives
# with the bundle's `tests/` directory at verification time and is deliberately
# absent here: an implementer working in this container must not be able to read
# the cases that grade them.
#
# Two interpreters, and the separation between them is the point. D18 requires
# redis-py to be absent from the interpreter that imports the client package,
# and D25 requires that absence to be a filesystem permission rather than a
# convention, because the attack suite established that a client can find
# redis-py on disk and inject its directory onto sys.path.

# --------------------------------------------------------------------------
# Stage 1: the pinned server binaries.
#
# D12: bookworm ships Redis 7.0 and HEXPIRE requires 7.4, so the binaries come
# from the Redis image by multi-stage copy rather than from apt.
# --------------------------------------------------------------------------
FROM redis:7.4-bookworm AS server

# --------------------------------------------------------------------------
# Stage 2: the image itself.
# --------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

COPY --from=server /usr/local/bin/redis-server /usr/local/bin/redis-server
COPY --from=server /usr/local/bin/redis-cli /usr/local/bin/redis-cli

# `sudo` is what lets the harness user run the oracle interpreter without being
# able to read it. `libssl3` is what redis-server links against.
RUN apt-get update \
 && apt-get install -y --no-install-recommends sudo libssl3 \
 && rm -rf /var/lib/apt/lists/* \
 && redis-server --version

# --------------------------------------------------------------------------
# Users. D25 layer one.
#
# `agent` writes the client package and runs the sealed harness, so `agent` is
# the user whose reach matters: the client package is imported into the harness
# process and inherits exactly this user's filesystem access.
#
# `oracle` owns the interpreter that has redis-py and nothing else.
# --------------------------------------------------------------------------
RUN useradd --create-home --shell /bin/bash agent \
 && useradd --create-home --shell /usr/sbin/nologin oracle

# --------------------------------------------------------------------------
# The harness interpreter: pytest, and no redis-py.
#
# pytest-timeout is pinned because docs/HARNESS.md section 8 sets a 30 second
# per case bound and that bound is declarative. Without the plugin the ini
# setting is inert and the only limit is the verifier's own.
# --------------------------------------------------------------------------
RUN python -m venv /opt/venv-harness \
 && /opt/venv-harness/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv-harness/bin/pip install --no-cache-dir \
        pytest==9.1.1 \
        pytest-timeout==2.4.0 \
 && /opt/venv-harness/bin/python - <<'PY'
import importlib.util, sys
assert importlib.util.find_spec("redis") is None, \
    "redis-py must not be reachable from the harness interpreter"
import pytest, pytest_timeout  # noqa: F401
print("harness interpreter:", sys.version.split()[0], "pytest", pytest.__version__)
PY

# --------------------------------------------------------------------------
# The oracle interpreter: redis-py, and nothing the harness user can read.
# --------------------------------------------------------------------------
RUN python -m venv /opt/venv-oracle \
 && /opt/venv-oracle/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv-oracle/bin/pip install --no-cache-dir redis==8.1.0 \
 && /opt/venv-oracle/bin/python -c \
        "import redis; assert redis.__version__ == '8.1.0', redis.__version__; \
         print('oracle interpreter: redis-py', redis.__version__)"

# D25 layer one, the structural control. The oracle interpreter and everything
# under it belong to `oracle` and are unreadable to anyone else, so a client
# that globs the filesystem for a redis package finds nothing it can open, and
# an injected sys.path entry resolves to a directory it cannot list.
#
# The gate is the directory bit, not the contents. Traversing into a directory
# needs execute permission on it, so 0700 on /opt/venv-oracle alone makes every
# path below it unreachable to anyone else, including a stat.
#
# `chown -Rh` rather than `chown -R`: a virtualenv's bin/python is a symlink to
# the base interpreter, and chown without -h changes the referent. Handing
# /usr/local/bin/python3.12 to the oracle user would take the base interpreter
# away from everyone. The same trap makes `chmod` on bin/* unsafe, so it is not
# done at all.
RUN chown -Rh oracle:oracle /opt/venv-oracle \
 && chmod 0700 /opt/venv-oracle

# The harness still has to be able to *run* the oracle interpreter, which is a
# different permission from being able to read it. One sudo rule, one command,
# no password, and the subprocess runs as `oracle` with `oracle`'s access.
RUN printf 'agent ALL=(oracle) NOPASSWD: /opt/venv-oracle/bin/python\n' \
        > /etc/sudoers.d/oracle-python \
 && chmod 0440 /etc/sudoers.d/oracle-python \
 && printf '#!/bin/sh\nexec sudo -n -u oracle /opt/venv-oracle/bin/python "$@"\n' \
        > /usr/local/bin/oracle-python \
 && chmod 0755 /usr/local/bin/oracle-python

# --------------------------------------------------------------------------
# The workspace: the starter package and the visible tests, and nothing else.
# --------------------------------------------------------------------------
WORKDIR /app
# /app itself must belong to the implementer, not only its contents. WORKDIR
# creates it root-owned and `COPY --chown` changes only what it copies, so
# without this the agent can edit files inside the package but cannot add a
# module beside it or replace the package outright. The oracle stage's
# solve.sh does exactly that, and failed here with "Permission denied" until
# the bundle was verified end to end.
RUN chown agent:agent /app
COPY --chown=agent:agent starter/resp3_wire /app/resp3_wire
COPY --chown=agent:agent visible_tests /app/visible_tests

ENV RESP3_ORACLE_PYTHON=/usr/local/bin/oracle-python \
    RESP3_REDIS_SERVER=/usr/local/bin/redis-server \
    RESP3_CLIENT_PATH=/app \
    PIP_NO_INDEX=1 \
    PYTHONDONTWRITEBYTECODE=1

USER agent

# A build-time proof of the arrangement, so a broken image fails here rather
# than during grading. The harness user must be able to run the oracle
# interpreter and must not be able to read it.
RUN set -e; \
    /opt/venv-harness/bin/python -c \
        "import importlib.util, sys; \
         sys.exit(0 if importlib.util.find_spec('redis') is None else 1)"; \
    oracle-python -c "import redis; print('oracle reachable by proxy:', redis.__version__)"; \
    if /opt/venv-harness/bin/python -c \
        "open('/opt/venv-oracle/pyvenv.cfg')" 2>/dev/null; then \
        echo "FAIL: the harness user can read the oracle interpreter" >&2; exit 1; \
    fi; \
    if ls /opt/venv-oracle >/dev/null 2>&1; then \
        echo "FAIL: the harness user can list the oracle interpreter" >&2; exit 1; \
    fi; \
    echo "D25 layer one holds: agent runs the oracle interpreter, cannot read it"

CMD ["/bin/bash"]
