# Security

SPARK holds a map of a network, an inventory of what runs on it, and
credentials that reach the devices. If you find a way for someone who should
not have that to get it, please tell me privately first.

## Reporting

Use GitHub's private vulnerability reporting for this repository
(**Security → Report a vulnerability**). Please include what you did, what
you saw, and the commit or version you saw it on. You will get an
acknowledgement within a week; a fix, or an honest reason there is not one
yet, within a month for anything that lets a stranger in.

Please do not open a public issue for something exploitable until there is a
fix to point at.

## In scope

- Anything reachable without signing in that should not be.
- Getting past sign-in, or staying signed in after being signed out.
- Reading or changing another instance's data, credentials, or alerts.
- Getting SPARK to send requests, credentials, or traffic somewhere it should not.
- Container escape, or anything that turns SPARK's `NET_RAW` into more than ping.

## Out of scope

- Findings that need the VM, the container, `data/`, or `secret.key` already
  in your hands: `vault.py` says what encryption at rest does and does not
  protect against, and that is the answer.
- The self-signed certificate not being trusted by browsers. It is pinned by
  fingerprint, not trusted by a CA; see `docs/configuration.md`.
- SNMPv2c sending its community in clear text, and anything else that is the
  protocol's own property. SPARK says so in the UI.
- Denial of service against the monitoring of your own network from your own
  network.

## What is already in place

The Authentication section of `docs/configuration.md` describes the current defences; the
project's review history — three rounds so far, each with its findings and
fixes — is in the repository's CHANGELOG under the security headings.
