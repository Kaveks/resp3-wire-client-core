"""Probe: the filesystem hunt that defeated interpreter separation finds nothing.

This is the attack from `attacks/wrap_redis_py` reduced to its first move. It
globs for a redis package belonging to any interpreter and reports what it can
open. Under D25 layer one the oracle interpreter's directory is unreadable, so
the glob cannot descend into it and returns nothing.
"""

import glob
import sys

PATTERNS = (
    "/opt/**/site-packages/redis/__init__.py",
    "/usr/**/site-packages/redis/__init__.py",
    "/home/**/site-packages/redis/__init__.py",
    "/app/**/redis/__init__.py",
)

found = []
for pattern in PATTERNS:
    try:
        found.extend(glob.glob(pattern, recursive=True))
    except OSError:
        continue
if found:
    sys.exit(f"the filesystem hunt found {found}")
print("the filesystem hunt found nothing to inject")
