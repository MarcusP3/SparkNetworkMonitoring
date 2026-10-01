"""Container healthcheck. Kept as a file rather than an inline one-liner so the
quoting stays readable and the port stays configurable.

SPARK serves HTTPS by default with a self-signed certificate (app.tls: auto),
or plain HTTP behind a proxy (app.tls: off). The check does not read the
config; it tries HTTPS without verification -- this is loopback, and the
question is "is the process answering", not "is the certificate trusted" --
and falls back to HTTP, so the same check is right for both."""

import os
import ssl
import sys
import urllib.request

port = os.environ.get("SPARK__APP__PORT", "9700")

unverified = ssl.create_default_context()
unverified.check_hostname = False
unverified.verify_mode = ssl.CERT_NONE

errors = []
for scheme, context in (("https", unverified), ("http", None)):
    try:
        with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/healthz", timeout=4,
                                    context=context) as response:
            sys.exit(0 if response.status == 200 else 1)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure means try the other scheme
        errors.append(f"{scheme}: {exc}")

print("; ".join(errors), file=sys.stderr)
sys.exit(1)
