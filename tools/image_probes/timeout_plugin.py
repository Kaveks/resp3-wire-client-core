"""Probe: the per case bound is enforced rather than documentary.

`docs/HARNESS.md` section 8 sets 30 s per case and `harness/pytest.ini` declares
it. Without pytest-timeout installed that declaration is inert.
"""

from importlib.metadata import PackageNotFoundError, version
import sys

try:
    print(f"pytest-timeout {version('pytest-timeout')}, pytest {version('pytest')}")
except PackageNotFoundError as exc:
    sys.exit(f"missing from the harness interpreter: {exc}")
