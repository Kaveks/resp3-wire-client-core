"""Probe: the container must not reach the network.

The rollout and verification phases both declare `network_mode = none`, so an
image that works only with network is an image that fails at grading.
"""

import socket
import sys

try:
    socket.create_connection(("1.1.1.1", 53), timeout=3)
except OSError as exc:
    print(f"unreachable, as required ({type(exc).__name__})")
else:
    sys.exit("the container reached the network")
