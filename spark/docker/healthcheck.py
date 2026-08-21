"""Container healthcheck. Kept as a file rather than an inline one-liner so the
quoting stays readable and the port stays configurable."""

import os
import sys
import urllib.request

port = os.environ.get("SPARK__APP__PORT", "9700")

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
    print(exc, file=sys.stderr)
    sys.exit(1)
