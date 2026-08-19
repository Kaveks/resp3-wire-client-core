"""Probe: the image an implementer works in carries neither the sealed harness
nor the reference implementation.

`CLAUDE.md`: the sealed harness is never visible to an implementer, and nothing
shipped into the image may reference sealed content. The reference is worse
still, because it is the answer.

Both arrive at verification time with the bundle's `tests/` and `solution/`
directories, which is why the Dockerfile copies from neither. This asserts the
outcome rather than trusting the layout, because the cost of being wrong is an
implementer reading the cases that grade them.

Matching is on content, not on filenames. An earlier version compared bare
filenames and reported `pygments/lexers/resource.py` as a sealed channel: the
harness's module names are ordinary words, and a third-party package is free to
use them. Content markers are specific to the thing being looked for.
"""

import os
import sys

# Strings that appear in the sealed harness and nowhere an implementer should
# see. The channel marker is on every scored module; the comparator's is on the
# one function the chunking channel compares through.
SEALED_MARKERS = (
    "pytest.mark.channel(",
    "def strict_describe(",
    "RESP3_ORACLE_PYTHON",
)

# Present in the reference implementation and absent from the starter stubs, so
# finding it means the answer shipped.
REFERENCE_MARKERS = ("_release_if_drained",)

ROOTS = ("/app", "/opt", "/home", "/srv", "/tmp")
# The probes are mounted in for this check and are not image content.
EXCLUDE = ("/opt/probes",)

findings = []
scanned = 0

for root in ROOTS:
    if not os.path.isdir(root):
        continue
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        if any(dirpath == skip or dirpath.startswith(skip + "/") for skip in EXCLUDE):
            dirnames[:] = []
            continue
        for name in filenames:
            if not name.endswith((".py", ".sh", ".ini", ".cfg", ".toml")):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    body = handle.read()
            except OSError:
                continue
            scanned += 1
            for marker in SEALED_MARKERS:
                if marker in body:
                    findings.append(f"sealed harness content in {path} ({marker!r})")
            for marker in REFERENCE_MARKERS:
                if marker in body:
                    findings.append(f"reference implementation in {path} ({marker!r})")

if findings:
    for finding in findings:
        print(finding, file=sys.stderr)
    sys.exit(f"{len(findings)} leak(s) into the implementer's image")

print(f"no sealed harness and no reference in the image ({scanned} files scanned)")
