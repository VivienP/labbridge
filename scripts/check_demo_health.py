"""Return success when the demo readiness endpoint answers."""

from __future__ import annotations

import sys
import urllib.request

OK = 200

with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
    if response.status != OK:
        raise SystemExit(1)
