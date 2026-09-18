"""MAC OUI prefix to vendor.

A curated table rather than the full IEEE registry, for the same reason the SNMP
collector ships numeric OIDs rather than a MIB compiler: the registry is a
multi-megabyte file that needs fetching, parsing and keeping current, and the
answer it gives for the twenty vendors actually present in a homelab is the
same answer this table gives.

An unknown prefix returns None and the UI says "unknown". That is honest and
costs nothing; guessing from a partial match would not be.
"""

from __future__ import annotations

# Keyed on the first three octets, lowercase, colon-separated.
OUI_VENDORS: dict[str, str] = {
    # Ubiquiti
    "24:5a:4c": "Ubiquiti", "68:d7:9a": "Ubiquiti", "78:8a:20": "Ubiquiti",
    "74:83:c2": "Ubiquiti", "b4:fb:e4": "Ubiquiti", "dc:9f:db": "Ubiquiti",
    "f0:9f:c2": "Ubiquiti", "fc:ec:da": "Ubiquiti", "e0:63:da": "Ubiquiti",
    "44:d9:e7": "Ubiquiti", "d0:21:f9": "Ubiquiti", "80:2a:a8": "Ubiquiti",
    "18:e8:29": "Ubiquiti", "9c:05:d6": "Ubiquiti", "ac:8b:a9": "Ubiquiti",
    # Raspberry Pi
    "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi",
    "e4:5f:01": "Raspberry Pi", "d8:3a:dd": "Raspberry Pi",
    "2c:cf:67": "Raspberry Pi",
    # Virtualisation — very common in a homelab, and worth naming precisely
    "52:54:00": "QEMU/KVM virtual",      # Proxmox guests land here
    "00:16:3e": "Xen virtual",
    "00:50:56": "VMware", "00:0c:29": "VMware", "00:05:69": "VMware",
    "08:00:27": "VirtualBox",
    "02:42:ac": "Docker container",
    "00:15:5d": "Hyper-V virtual",
    # NAS
    "00:11:32": "Synology", "90:09:d0": "Synology",
    "00:08:9b": "QNAP", "24:5e:be": "QNAP",
    # Apple
    "00:1b:63": "Apple", "3c:07:54": "Apple", "a4:83:e7": "Apple",
    "f0:18:98": "Apple", "8c:85:90": "Apple", "bc:d0:74": "Apple",
    "dc:a9:04": "Apple", "d0:81:7a": "Apple",
    # Intel NICs
    "00:1b:21": "Intel", "00:1e:67": "Intel", "a0:36:9f": "Intel",
    "3c:fd:fe": "Intel", "b4:96:91": "Intel",
    # Networking vendors
    "00:1a:8c": "Cisco", "00:26:99": "Cisco",
    "6c:3b:6b": "MikroTik", "48:8f:5a": "MikroTik", "dc:2c:6e": "MikroTik",
    "00:1d:7e": "Cisco/Linksys",
    "50:c7:bf": "TP-Link", "ac:84:c6": "TP-Link", "c4:6e:1f": "TP-Link",
    "00:1f:33": "Netgear", "a0:40:a0": "Netgear", "9c:3d:cf": "Netgear",
    "b0:39:56": "Netgear",
    "00:24:01": "D-Link", "1c:af:f7": "D-Link",
    "00:0d:b9": "PC Engines",
    # Media and IoT that tend to show up unlabelled
    "b8:27:eb ": "Raspberry Pi",
    "18:b4:30": "Nest", "64:16:66": "Nest",
    "d0:73:d5": "LIFX",
    "cc:50:e3": "Espressif (ESP32)", "24:0a:c4": "Espressif (ESP32)",
    "a4:cf:12": "Espressif (ESP32)", "3c:61:05": "Espressif (ESP8266)",
    "00:17:88": "Philips Hue",
    "b0:c5:54": "D-Link/Amcrest camera",
    "00:80:f0": "Panasonic",
    "54:af:97": "TCL/Roku", "b8:3e:59": "Roku", "cc:6d:a0": "Roku",
    "00:04:4b": "NVIDIA",
    "f4:f5:d8": "Google", "1c:f2:9a": "Google", "30:fd:38": "Google",
    "44:07:0b": "Google",
    "ec:fa:bc": "Espressif", "68:c6:3a": "Espressif",
}


def normalise(mac: str | None) -> str | None:
    """Lowercase colon form, or None if it isn't a MAC."""
    if not mac:
        return None
    cleaned = mac.strip().lower().replace("-", ":").replace(".", ":")
    parts = [p for p in cleaned.split(":") if p]
    if len(parts) == 6 and all(len(p) <= 2 for p in parts):
        try:
            return ":".join(f"{int(p, 16):02x}" for p in parts)
        except ValueError:
            return None
    hex_only = "".join(c for c in cleaned if c in "0123456789abcdef")
    if len(hex_only) == 12:
        return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
    return None


def lookup(mac: str | None) -> str | None:
    """Vendor for a MAC, or None if the prefix isn't in the table."""
    normalised = normalise(mac)
    if normalised is None:
        return None
    return OUI_VENDORS.get(normalised[:8])


def is_locally_administered(mac: str | None) -> bool:
    """Is this a randomised or software-assigned address.

    Bit 1 of the first octet. Phones randomise their MAC per network by
    default now, so these appear, vanish, and never come back under the same
    address. Worth flagging in the UI rather than presenting as a stable device
    that keeps disappearing.
    """
    normalised = normalise(mac)
    if normalised is None:
        return False
    try:
        return bool(int(normalised[:2], 16) & 0b10)
    except ValueError:
        return False
