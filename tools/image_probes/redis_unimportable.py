"""Probe: redis-py must not be importable by the interpreter running this.

D25 layer one. This is the interpreter that imports the client package, so the
client inherits exactly this reach.
"""

import importlib.util
import sys

spec = importlib.util.find_spec("redis")
if spec is not None:
    sys.exit(f"redis-py is importable by this interpreter: {spec.origin}")
print("redis-py is not importable by this interpreter")
