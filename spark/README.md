# ⚡ SPARK

Network monitoring for homelabs. Health checks, alerting, and a live map of
every device and service on your network — in one container, with one SQLite
file to back up.

Homelab tooling makes you choose between uptime checkers that know nothing
about your network and enterprise NMS platforms built for a full-time operator.
SPARK aims at the middle: it discovers the network itself and shows you what's
actually running, rather than making you type it all in.

> **Status: increment 2.** Configuration, database, authentication, the web
> shell, and the SNMP collection engine are working. Storage, scheduling, and
> the UI for SNMP data come next. See the roadmap below.
>
> The SNMP engine is reachable today only through `spark-probe`. Nothing polls
> on a schedule yet, nothing is persisted, and the dashboard still shows an
> empty state — the collector is a library plus a CLI, not a running service.

---

## Find out what your gear actually supports

Vendor SNMP documentation is unreliable, and prosumer switches frequently omit
standard MIBs — temperature especially. So don't guess:

```bash
spark-probe 192.168.1.2 -c your-community
```

It reports the device's identity, live CPU/memory/temperature, a capability
matrix of what it does and doesn't answer, and the interface table. Add
`--json` for machine-readable output, `-v v3` with `--username/--auth-key/
--priv-key` for SNMPv3.

Inside Docker:

```bash
docker compose run --rm spark spark-probe 192.168.1.2 -c your-community
```

### What it reports, and what it won't

| Metric | Source, in order | Notes |
|---|---|---|
| Identity, uptime, model, serial | SNMPv2-MIB system group, ENTITY-MIB | Vendor derived from `sysObjectID` |
| Interfaces | IF-MIB, then ifXTable | 64-bit counters preferred; `spark-probe` warns when a device only offers 32-bit ones |
| Memory | HOST-RESOURCES-MIB, then UCD-SNMP-MIB | |
| Temperature | ENTITY-SENSOR-MIB | Absent on most prosumer switches |
| CPU % | HOST-RESOURCES-MIB `hrProcessorLoad` | Often unavailable — see below |
| Load average | UCD-SNMP-MIB `laLoad` | Fallback when CPU % is unavailable |

**CPU percentage is frequently not available, and SPARK will not invent one.**
`hrProcessorLoad` is missing on plenty of devices, and the UCD scalars that used
to serve as the fallback (`ssCpuIdle`, `ssCpuUser`) are deprecated and no longer
answered by modern net-snmp — which covers pfSense, OPNsense, and Linux
appliances. The raw counters that replaced them are cumulative ticks and need
two samples to become a percentage, so that arrives with the polling scheduler.
Until then `cpu_percent` is `None` rather than a fabricated `0`, and the load
average is reported in its own right. A load of 1.4 on a four-core box is not
140% CPU and is not displayed as though it were.

Every metric carries a `sources` entry naming where it came from, because a
device reporting CPU via UCD-SNMP and one reporting it via HOST-RESOURCES are
not measuring quite the same thing.

### UniFi specifics

- SNMP is a **global** setting in UniFi Network (Settings → System), not per-device.
- **UniFi consoles (UDM/UDM-Pro/UDM-SE) do not expose SNMP through the UI.** Your
  switches will answer; the console itself won't. Its health comes from the
  UniFi Network Integration API instead, which is a separate collector.
- **USW Flex and USW Ultra switches don't support SNMP at all.**
- APs have no native SNMP agent.
- Ubiquiti's own docs note their MIBs "are not comprehensive" — expect CPU and
  temperature to be sparse or missing. Run `spark-probe` and see.

---

## Quick start

```bash
git clone <your-repo> spark && cd spark
cp config/spark.yaml config/spark.yaml.bak   # keep a pristine copy
$EDITOR config/spark.yaml                    # set your subnets
docker compose up -d --build
```

Open `http://<host>:9700` and create the admin account when prompted.

Running without Docker:

```bash
pip install -e .
SPARK_CONFIG=./config/spark.yaml SPARK__APP__DATA_DIR=./data spark
```

---

## Configuration

Two layers, on purpose:

| Where | What lives there |
|---|---|
| `config/spark.yaml` | Things needed before the database exists: bind address, data directory, auth mode, subnets |
| Web UI → Settings | Everything you'd change routinely: alert webhook, schedules, scan behaviour, retention |

Any YAML value can be overridden by environment variable, nesting with double
underscores: `SPARK__APP__PORT=9800`, `SPARK__AUTH__MODE=proxy`.

### Three Docker settings that are not optional

```yaml
network_mode: host      # ARP/ICMP sweeps only see the LAN from the host netns
cap_add: [NET_RAW]      # real ICMP; without it ping degrades to TCP probes
volumes: [./data:/data] # spark.db and the session key must survive a rebuild
```

On a bridge network SPARK sits behind NAT and discovery finds **nothing**. This
is the single most common way to end up with an empty dashboard.

### VLANs

Each subnet is declared as either directly attached or routed:

```yaml
- name: Servers
  cidr: 192.168.30.0/24
  vlan: 30
  attached: false
```

This matters more than it looks. ARP only works on directly-attached L2
segments, and device identity keys on MAC address. Across a router SPARK can
ping a host but cannot learn its MAC, so devices on routed VLANs fall back to
IP-based identity — which breaks the moment DHCP hands out a different address.

Two ways to fix it: give the SPARK VM an interface in each VLAN, or wait for
SNMP collection, which reads MAC-to-IP off the switch for every VLAN at once.

---

## Authentication

Default is a single admin account with a password: Argon2id hash, a random
256-bit session token stored only as a SHA-256 hash, an HTTP-only `SameSite=Lax`
cookie, and a rate-limited login endpoint. A failed login costs the same Argon2
work whether or not the username exists, so the login form cannot be used to
enumerate accounts.

The cookie carries an opaque token and is not itself signed — a database leak
hands over no usable sessions, and revocation is a row update. (`itsdangerous`
is still listed as a dependency and `secret_key` is still written to the data
directory; neither is used, and both should go.)

SPARK ends up holding a map of your entire network, an inventory of every
service on it, and references to credentials that reach your Docker hosts. That
makes it the highest-value target on the LAN, which is why there's no
"it's internal, skip the login" mode.

To put it behind Authelia, Tailscale, or Cloudflare Access instead:

```yaml
auth:
  mode: proxy
  proxy:
    header: Remote-User
    trusted_proxies: [192.168.1.5]
```

SPARK refuses to start in proxy mode with an empty `trusted_proxies`. Trusting
an identity header from any source is forgeable by anything on the network —
worse than no auth, because it looks like security.

---

## Roadmap

| # | Increment | Status |
|---|---|---|
| 1 | Foundation — config, schema, auth, dashboard shell | ✅ done |
| 2 | SNMP collection engine + capability probe | ✅ done |
| 3 | Metric storage, polling scheduler, device pages | next |
| 4 | UniFi Network API collector (console CPU/temp, uplink topology) | planned |
| 5 | Check engine — ping, TCP, HTTP, DNS, hysteresis | planned |
| 6 | Discovery — subnet sweep, Docker inventory, port scan | planned |
| 7 | Service map — tree and filterable list views | planned |
| 8 | Alerting — Discord, dependency suppression, quiet hours | planned |

Topology collection (LLDP, MAC tables, ARP) is already in the OID catalogue and
gets surfaced when the map is built.

## Tests

```bash
pip install -e ".[dev]"
./tests/local_agent.sh start   # a local net-snmp agent to test against
pytest -q
./tests/local_agent.sh stop

python smoke_test.py           # end-to-end check of the web app and auth
```

`pytest` is the developer suite; `smoke_test.py` is a standalone "did my
install work" script that needs no test framework.

---

## Design notes

Three schema decisions that are expensive to change later, documented here so
they don't get "simplified" away:

**Devices are keyed on MAC, not IP.** DHCP reassigns addresses. A tool keyed on
IP silently loses a device's entire history the first time a lease churns.

**Incidents are rows, not a query.** Deriving "was it down, and for how long"
from raw check results at read time gets painful fast, and it's the question you
ask most.

**`service` and `target` are separate tables.** A service is a fact about the
network, discovered whether or not you care. A target is a decision to watch
something. Merge them and you either monitor everything you find (noise) or lose
the inventory of what you chose to ignore.

One more, in the check engine: **hysteresis is not optional.** A target needs N
consecutive failures before it changes state. This single detail is the
difference between a tool you trust and one you mute within a week.

---

## Project layout

```
src/spark/
  config.py     YAML + env config loading
  models.py     full Phase 1 schema, UTC datetime and enum column types
  db.py         engine, sessions, migration runner
  auth.py       Argon2 passwords, sessions, proxy mode
  cli.py        spark-probe
  main.py       app factory and entry point
  collectors/
    base.py     collector Protocol and the normalised result shapes
    oids.py     numeric OID catalogue and capability probes
    snmp.py     the SNMP collector
  web/          routes and dependencies
  templates/    Jinja templates
  static/       hand-written CSS, no build step

tests/
  test_snmp.py    pure-function tests, plus live tests that skip without an agent
  local_agent.sh  starts a throwaway net-snmp agent on 127.0.0.1:11161
smoke_test.py     end-to-end walk through setup, login, and the auth gate
```

Two conventions worth knowing before editing:

- **Timestamps.** Columns use `UTCDateTime`, not `DateTime(timezone=True)`.
  SQLite has no offset, so the latter silently returns naive datetimes and the
  first `utcnow() - stored` raises `TypeError`. Storage format is unchanged.
- **Enums.** Columns use `enum_column()` and the enums are `enum.StrEnum`, so
  values are stored lowercase, read back as members, and format as `down`
  rather than `HealthStatus.DOWN` in an alert message.

No npm, no bundler, no Alembic. Clone it and read it top to bottom.
