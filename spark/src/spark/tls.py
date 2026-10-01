"""TLS of SPARK's own (review finding #25).

On the LAN as shipped, the password at login and the session cookie on every
request crossed the wire in clear, readable by anything on-path -- a
compromised IoT device on the same VLAN is the homelab-realistic case. For a
tool whose own docstring calls it the most valuable target on the LAN, that
was the largest credential-theft path left.

So SPARK serves HTTPS by default, with a certificate it makes for itself on
first start. A self-signed certificate is not trusted by browsers, and it is
not meant to be: it is pinned the way SPARK asks people to pin TrueNAS's and
Proxmox's -- the SHA-256 fingerprint is in the log, the browser shows the same
one, and a person compares them once. What it gives is a private channel with
the box the fingerprint belongs to, which is what the LAN case needs.

Operators with a real certificate (a CA, or one their devices already trust)
point `app.tls.cert` and `app.tls.key` at it; a reverse proxy that terminates
TLS in front sets `app.tls: off`.

The certificate: EC P-256 (fast handshakes on a small VM), ten years, SAN of
`localhost`, the loopback addresses, the instance name if it is a valid host
name, and the addresses this machine has at generation time. It is made once
and kept; a changed address means a browser sees the same fingerprint it
already accepted, only under a name not on the certificate, which is fine for
a pinned self-signed certificate and is documented as such.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CERT_NAME = "cert.pem"
KEY_NAME = "key.pem"
VALID_DAYS = 3650

_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def fingerprint(cert_pem: bytes) -> str:
    """SHA-256 of the DER certificate, as browsers show it: AA:BB:..."""
    from cryptography import x509

    der = x509.load_pem_x509_certificate(cert_pem).public_bytes(_der())
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def _der():  # type: ignore[no-untyped-def]
    from cryptography.hazmat.primitives.serialization import Encoding

    return Encoding.DER


def local_addresses() -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The addresses this machine has now, best effort, loopback last.

    Two sources, because neither is complete on its own: the host name's
    resolution (what /etc/hosts and DNS say this machine is) and the address
    the kernel would use to reach a private network (a UDP connect sends
    nothing, it only picks a source). With host networking these are the VM's
    own addresses, which is what a browser on the LAN will type.
    """
    found: list = []

    def add(raw: str) -> None:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return
        if address not in found:
            found.append(address)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            add(info[4][0])
    except (socket.gaierror, OSError):
        pass
    for probe in ("10.255.255.255", "192.168.255.255", "172.31.255.255"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                add(s.getsockname()[0])
        except OSError:
            continue
    found = [a for a in found if not a.is_loopback]
    found += [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]
    return found


def make_self_signed(instance_name: str, addresses=None) -> tuple[bytes, bytes]:  # type: ignore[no-untyped-def]
    """(certificate PEM, private key PEM) for this install."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat,
    )
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    names: list[x509.GeneralName] = [x509.DNSName("localhost")]
    label = (instance_name or "").strip().lower()
    if _HOSTNAME.match(label) and label != "localhost":
        names.append(x509.DNSName(label))
    for address in (addresses if addresses is not None else local_addresses()):
        names.append(x509.IPAddress(address))

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (instance_name or "SPARK")[:64])])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(Encoding.PEM),
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()),
    )


def _write_private(path: Path, data: bytes) -> bool:
    """Create the file 0600 from the first byte; False if it already existed."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return True


def ensure_self_signed(tls_dir: Path, instance_name: str) -> tuple[Path, Path, bool]:
    """The certificate and key files, made if they do not exist yet.

    Returns (cert path, key path, made now). Both or neither: a key without
    its certificate (or the reverse) is treated as absent and both are made
    again, since a certificate for a different key serves nothing.
    """
    tls_dir.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = tls_dir / CERT_NAME, tls_dir / KEY_NAME
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path, False
    for stale in (cert_path, key_path):
        if stale.exists():
            stale.unlink()
    cert_pem, key_pem = make_self_signed(instance_name)
    if not _write_private(key_path, key_pem):
        # Lost the race to another process starting at the same moment; its
        # pair is the pair -- but only once its certificate has landed too.
        return cert_path, key_path, False
    _write_private(cert_path, cert_pem)
    return cert_path, key_path, True


def announce(cert_path: Path, host: str, port: int, made_now: bool) -> None:
    """Say which certificate SPARK is serving, so it can be checked in the browser."""
    where = "localhost" if host in ("0.0.0.0", "::", "") else host
    banner = "=" * 72
    log.warning(
        "\n%s\n"
        "  SPARK is serving HTTPS with %s certificate.\n"
        "  Open https://%s:%s -- the browser will warn once, because the\n"
        "  certificate is self-signed. Before accepting it, check that the\n"
        "  SHA-256 fingerprint it shows is this one:\n\n"
        "      %s\n\n"
        "  The same fingerprint every start, until %s is deleted.\n"
        "%s",
        banner, "a new self-signed" if made_now else "its self-signed",
        where, port, fingerprint(cert_path.read_bytes()), cert_path, banner,
    )
