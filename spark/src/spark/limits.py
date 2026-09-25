"""How big any one input may be. One place, so the page and the server agree.

Every form field has a length cap on the server (`Form(max_length=...)`) and,
where a person types into it, the same cap as `maxlength` on the page -- so
the browser stops you at the limit and the server refuses anything that did
not come from the form. Nothing here is about injection: queries are
parameterised, output is escaped and nothing runs a shell. These keep a
1 MB "name" out of the database and off every page that lists it.
"""

from __future__ import annotations

# SQLite's INTEGER is a signed 64-bit value. A larger number in a URL or a
# form reaches the database driver as an OverflowError -- a 500 -- rather than
# "no such row", so ids are range-checked before they get that far.
MAX_ID = 2**63 - 1

NAME = 64          # device, target, subnet and profile names
ADDRESS = 255      # a host name is at most 253; a URL for an HTTP check fits
PARAMS = 4096      # a target's JSON params
USERNAME = 64
# Argon2 hashes whatever it is given; a megabyte password is a megabyte of
# hashing per login attempt. Long enough for any passphrase manager.
PASSWORD = 1024
SECRET = 1024      # community strings and v3 keys; snmp_config says 255 nicely
WEBHOOK = 512      # a Discord webhook URL is about 120 characters
URL = 2048         # next= and back=, which are paths on this site
TIMEZONE = 64      # the longest IANA name is 32
SHORT = 32         # numbers, times, versions, protocol names, checkboxes

# The largest request body accepted at all, checked before any route runs.
# The biggest real form is a target with 4 KB of params; this is 16x that.
BODY = 64 * 1024


def as_id(value: object) -> int | None:
    """A row id from a form value, or None if it is not one that could exist."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= MAX_ID else None
