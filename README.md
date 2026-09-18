# ⚡ SPARK

**Network monitoring for homelabs.** Health checks, alerting, and a live map of
every device and service on your network — in one container, with one SQLite
file to back up.

Homelab tooling makes you choose between uptime checkers that know nothing about
your network and enterprise NMS platforms built for a NOC with a full-time
operator. SPARK aims at the middle: it discovers the network itself and shows
you what is actually running, rather than making you type it all in.

- **Version** 0.1.0 · **Python** 3.12+ · **Runtime** one Docker container on a
  dedicated Linux VM · **Storage** a single SQLite file

---

## Status

SPARK is built in increments, and this table is the honest account of what each
one has actually delivered. Anything marked *not yet* does nothing at all today.

| Area | State |
|---|---|
| Configuration, database, authentication, web shell | ✅ working |
| Check engine — ping, TCP, HTTP(S), DNS | ✅ working |
| Live-updating pages (server-sent events) | ✅ working |
| Device discovery — ICMP/ARP sweep, MAC identity, vendor lookup | ✅ working |
| Device inventory — naming, review state, watch-in-one-click | ✅ working |
| Scan schedule — on/off and interval, set on the Devices page | ✅ working |
| Subnet management and VLAN tags (`/settings`) | ✅ working |
| Subnet filter on the Devices page | ✅ working |
| History retention — nightly downsample and prune | ✅ working |
| Hysteresis, incident tracking, dependency suppression | ✅ working |
| Target management UI (`/targets`) | ✅ working |
| SNMP collection | ⚠️ library and `spark-probe` CLI only — nothing is polled on a schedule or persisted |
| Alerting (Discord) | ❌ not yet |
| Discovery — subnet sweep, Docker inventory, port scan | ❌ not yet |
| Service map, topology | ❌ not yet |

In practice: SPARK can tell you something is down, but it cannot yet tell *you*
— you have to look at the page. That is the next increment.

---

## Contents

- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Monitoring](#monitoring)
- [SNMP](#snmp)
- [Authentication](#authentication)
- [Development](#development)
- [Roadmap](#roadmap)
- [Design decisions](#design-decisions)

---

## Quick start

Requires Docker with the Compose plugin, on a Linux host with a NIC on the
network you want to watch.

The application lives in the `spark/` subdirectory, not the repository root.
Every `docker compose` and `pytest` command runs from there.

```bash
git clone https://github.com/MarcusP3/SparkNetworkMonitoring.git
cd SparkNetworkMonitoring/spark
cp config/spark.yaml config/spark.yaml.orig   # keep a pristine copy
$EDITOR config/spark.yaml                     # set your first subnet (seed only)
docker compose up -d --build
```

Open `http://<host>:9700` and create the admin account when prompted. The
password minimum is 12 characters.

Verify it came up:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9700/healthz   # 200
docker compose logs --tail=20                                            # "ready ... polling N target(s)"
```

Without Docker, for development:

```bash
pip install -e .
SPARK_CONFIG=./config/spark.yaml SPARK__APP__DATA_DIR=./data spark
```

> **Docker Desktop for Mac and Windows will not work** for the discovery
> features. Its host networking operates at layer 4, so ARP (layer 2) and real
> ICMP (layer 3) never reach your LAN. Checks over TCP, HTTP and DNS work fine
> there, which makes it usable for development but not for deployment.

---

## Configuration

Two layers, deliberately:

| Where | What lives there |
|---|---|
| `config/spark.yaml` | Things needed *before the database exists*: bind address, data directory, auth mode. Its `network.subnets` block seeds the database once and is then ignored |
| Web UI | Everything you would change routinely: targets, subnets and VLAN tags, the scan schedule, and later the alert webhook and retention |

Any YAML value can be overridden by environment variable, nesting with double
underscores: `SPARK__APP__PORT=9800`, `SPARK__AUTH__MODE=proxy`.

### Dependencies and the supply chain

15 direct dependencies, 38 packages in the full closure, no npm and no build
step. Everything is pinned by version **and SHA-256 hash** in
`requirements.lock`, and the image installs from it with `--require-hashes`.

That matters more than the list does. Against `>=` constraints a build resolves
fresh from PyPI every time, so you never build the same image twice and have no
record of what shipped — and a single hijacked maintainer account is enough to
put code on your network. With hashes, an artifact has to match bytes recorded
in this repository or the build fails having installed nothing. Verified by
corrupting a hash and watching `pip` refuse.

Adding a dependency means regenerating the lock in the same commit:

```bash
uv pip compile pyproject.toml --generate-hashes --python-version 3.12 -o requirements.lock
```

`tests/test_supply_chain.py` fails if `pyproject.toml` and the lock disagree, so
this cannot rot quietly. Check for known advisories with `pip-audit -r
requirements.lock`.

The base image is pinned by digest for the same reason: `python:3.12-slim` is
rebuilt regularly and points at different bytes over time. Re-pin it when you
want a newer base, deliberately, as a commit.

### Docker settings that are not optional

```yaml
network_mode: host      # ARP/ICMP sweeps only see the LAN from the host netns
cap_add: [NET_RAW]      # real ICMP; without it ping degrades to TCP probes
volumes: [./data:/data] # spark.db and the session key must survive a rebuild
```

On a bridge network SPARK sits behind NAT and discovery finds **nothing**. This
is the single most common way to end up with an empty dashboard.

### Networks and device identity

Subnets live in the database and are managed at `/settings` — add, rename,
retag and remove them in the browser, no restart. The `network.subnets` block
in `spark.yaml` is a **seed**: its entries are copied in on the first start and
the section is never read again, so editing it on a running install does
nothing.

Each subnet is either directly attached or routed:

```yaml
network:
  subnets:
    - name: LAN
      cidr: 10.1.10.0/24
      attached: true      # SPARK has an interface on this segment

    - name: Servers
      cidr: 10.1.30.0/24
      vlan: 30            # documentation only; nothing reads it
      attached: false     # reachable only through a router
```

This matters more than it looks. ARP only works on directly-attached layer 2
segments, and device identity keys on MAC address. Across a router SPARK can
ping a host but cannot learn its MAC, so devices on routed VLANs fall back to
IP-based identity — which breaks the moment DHCP hands out a different address.

Two ways to fix it: give the SPARK VM an interface in each VLAN, or wait for
SNMP collection, which reads MAC-to-IP off the switch for every VLAN at once.

`vlan:` is a display label; nothing reads it functionally. It is editable at
`/settings` and shows in its own column on the Devices page, which also filters
by subnet — including a "not on a configured subnet" option, the quickest way
to notice a segment you never configured.

A device's subnet is worked out from its address every time the page renders,
not stored when it was discovered. Renaming a subnet keeps its devices, and
adding one classifies devices found before it existed. The most specific match
wins, so documenting a `/8` does not swallow the `/24`s inside it.

---

## Monitoring

Targets come from two places: added by hand at `/targets`, or promoted from a
discovered device with the **Watch** button at `/devices`.

### Check types

| Check | Address | Useful params |
|---|---|---|
| `ping` | `10.1.10.1` | `{"count": 3, "loss_warn_percent": 1}` |
| `tcp` | `10.1.10.1:443`, or address plus `{"port": 443}` | — |
| `http` | `https://host/path` | `{"expect_status": 200, "expect_body": "ok", "cert_warn_days": 14}` |
| `dns` | `example.com` | `{"rdtype": "A", "server": "10.1.10.1", "expect": "10.1.10."}` |

Checks never raise. A poller that throws when the thing it polls is broken has
failed at its only job, so every failure path returns a result with a reason
attached.

### Three states, not two

`up` · `degraded` · `down`

`degraded` means reachable but impaired — partial packet loss, a TLS
certificate about to expire. It moves in and out immediately and never opens an
incident, because an early warning that you delay is not an early warning.

### Hysteresis

`failure_threshold` consecutive failures before a target is called **down**;
`recovery_threshold` consecutive successes before it is called **up** again.

Setting failures to 1 means a single dropped packet is an outage, which is how
you end up muting your own monitoring. Recovery is hysteretic too, so a flapping
target that answers once does not close its own incident.

### Defaults

A new target needs only a name, a check type and an address. Timing and
thresholds are hidden behind a **Tune timing and thresholds** checkbox and are
greyed out until you tick it:

| Setting | Default | Meaning |
|---|---|---|
| Interval | 15s | How often the check runs |
| Timeout | 3s | How long one probe waits |
| Failures before DOWN | 4 | ~60s to call an outage |
| Successes before UP | 4 | ~60s to call a recovery |

These live in one place, `DEFAULT_*` in `models.py`, because a disabled input is
not submitted at all — so whatever the form falls back to *is* the default a
user gets, and the column default, the form default and the text on the page
have to agree.

Editing a target whose values differ from the defaults opens the section
already ticked. It has to: saving with the box shut would submit nothing and
silently reset that target to the defaults.

### Dependencies

Point each host at the switch it sits behind, and the switch at the gateway.
When the switch fails, the hosts' incidents are still recorded — you want the
history — but flagged as symptoms, so alerting can send one message instead of
thirty.

---

## SNMP

### Find out what your gear actually supports

Vendor SNMP documentation is unreliable, and prosumer switches frequently omit
standard MIBs — temperature especially. So don't guess:

```bash
spark-probe 10.1.10.2 -c your-community

# inside Docker
docker compose run --rm spark spark-probe 10.1.10.2 -c your-community
```

It reports the device's identity, live CPU/memory/temperature, a capability
matrix of what it does and does not answer, and the interface table. Add
`--json` for machine-readable output, or `-v v3` with
`--username/--auth-key/--priv-key` for SNMPv3. It is read-only and touches no
database.

### What it reports, and what it won't

| Metric | Source, in order | Notes |
|---|---|---|
| Identity, uptime, model, serial | SNMPv2-MIB system group, ENTITY-MIB | Vendor derived from `sysObjectID` |
| Interfaces | IF-MIB, then ifXTable | 64-bit counters preferred; `spark-probe` warns when a device only offers 32-bit ones |
| Memory | HOST-RESOURCES-MIB, then UCD-SNMP-MIB | |
| Temperature | ENTITY-SENSOR-MIB | Absent on most prosumer switches |
| CPU % | HOST-RESOURCES-MIB `hrProcessorLoad` | Often unavailable — see below |
| Load average | UCD-SNMP-MIB `laLoad` | Fallback when CPU % is unavailable |

**CPU percentage is frequently unavailable, and SPARK will not invent one.**
`hrProcessorLoad` is missing on plenty of devices, and the UCD scalars that used
to serve as the fallback (`ssCpuIdle`, `ssCpuUser`) are deprecated and no longer
answered by modern net-snmp — which covers pfSense, OPNsense and Linux
appliances. The raw counters that replaced them are cumulative ticks and need
two samples to become a percentage, so that arrives with SNMP polling. Until
then `cpu_percent` is `None` rather than a fabricated `0`, and the load average
is reported in its own right. A load of 1.4 on a four-core box is not 140% CPU
and is not displayed as though it were.

Every metric carries a `sources` entry naming where it came from, because a
device reporting CPU via UCD-SNMP and one reporting it via HOST-RESOURCES are
not measuring quite the same thing.

### UniFi specifics

- SNMP is a **global** setting in UniFi Network (Settings → System), not per-device.
- **UniFi consoles (UDM/UDM-Pro/UDM-SE) do not expose SNMP through the UI.** Your
  switches will answer; the console itself will not.
- **USW Flex and USW Ultra switches do not support SNMP at all.**
- Access points have no native SNMP agent.
- Ubiquiti's own docs note their MIBs "are not comprehensive" — expect CPU and
  temperature to be sparse or missing. Run `spark-probe` and see.

---

## Authentication

A single admin account with a password: Argon2id hash, a random 256-bit session
token stored only as a SHA-256 hash, an HTTP-only `SameSite=Lax` cookie, and a
rate-limited login endpoint. A failed login costs the same Argon2 work whether
or not the username exists, so the login form cannot be used to enumerate
accounts.

The cookie carries an opaque token and is not itself signed — a database leak
hands over no usable sessions, and revocation is a row update.

SPARK ends up holding a map of your entire network, an inventory of every
service on it, and references to credentials that reach your Docker hosts. That
makes it the highest-value target on the LAN, which is why there is no
"it's internal, skip the login" mode.

To put it behind Authelia, Tailscale or Cloudflare Access instead:

```yaml
auth:
  mode: proxy
  proxy:
    header: Remote-User
    trusted_proxies: [10.1.10.5]
```

SPARK refuses to start in proxy mode with an empty `trusted_proxies`. Trusting
an identity header from any source is forgeable by anything on the network —
worse than no auth, because it looks like security.

> **Known gaps.** There is no TLS; the session cookie is deliberately not
> `Secure`, because SPARK is normally reached over plain HTTP on a LAN and a
> `Secure` cookie there would silently never be sent. Put it behind a reverse
> proxy before exposing it. The container also runs as root, and `/api/docs` is
> unauthenticated.

---

## Development

### Running tests

```bash
pip install -e ".[dev]"

pytest -q                      # unit and integration suite
python smoke_test.py           # end-to-end walk through the running app

./tests/local_agent.sh start   # optional: a local net-snmp agent
pytest -q                      #   the live collector tests then run instead of skipping
./tests/local_agent.sh stop
```

`pytest` is the developer suite. `smoke_test.py` is a standalone "did my install
work" script that needs no test framework and exercises setup, login, rate
limiting, target CRUD and the check engine over real HTTP.

### Project layout

Everything below is relative to the repository root. The application is in
`spark/`; only `README.md`, `CLAUDE.md` and `DESIGN.md` live at the top.

```
DESIGN.md         the design document
CLAUDE.md         working agreements for this repo
spark/
  docker-compose.yml, Dockerfile
  requirements.lock     every package pinned to a version and SHA-256 hashes
  requirements-build.lock  the build toolchain, pinned the same way
  config/spark.yaml     the pre-database config (subnets here are a one-time seed)
  CHANGELOG.md
  smoke_test.py         end-to-end walk through the running application
  src/spark/
    config.py           YAML + env config loading
    models.py           full Phase 1 schema, UTC datetime and enum column types
    db.py               engine, sessions, migration runner
    auth.py             Argon2 passwords, sessions, proxy mode
    cli.py              spark-probe
    main.py             app factory and entry point
    scheduler.py        APScheduler jobs, reconciled against the database
    events.py           in-process pub/sub for live page updates
    retention.py        nightly downsample and prune; never VACUUMs
    subnets.py          subnet CRUD, validation, and the one-shot YAML seed
    discovery/
      sweep.py          ICMP sweep, ARP table, reverse DNS
      oui.py            MAC prefix to vendor
      store.py          the device identity rules
    checks/
      base.py           CheckSpec and CheckOutcome; no database access
      net.py            ping, tcp, http, dns
    engine/
      state.py          hysteresis and the incident lifecycle
      runner.py         joins a check to the database, owns its session
    collectors/
      base.py           collector Protocol and the normalised result shapes
      oids.py           numeric OID catalogue and capability probes
      snmp.py           the SNMP collector
    web/                routes and dependencies
    templates/          Jinja templates
    static/             hand-written CSS, no build step
  tests/
    test_engine.py      hysteresis, incidents, dependency suppression, the checks
    test_events.py      what live updates publish, and what they stay quiet about
    test_discovery.py   device identity across DHCP churn, ARP parsing, OUI
    test_retention.py   downsampling keeps outages; weighted averages; idempotence
    test_schedule.py    when the first sweep is due; the scan schedule form
    test_subnets.py     CIDR/VLAN validation, membership, seeding, migration 3
    test_targets_page.py  what the targets list says about failures, and when
    test_supply_chain.py  the lock matches pyproject; everything pinned and hashed
    test_incidents.py   one open incident per target, across pause/resume
    test_snmp.py        pure-function tests, live tests that skip without an agent
    local_agent.sh      starts a throwaway net-snmp agent on 127.0.0.1:11161
```

No npm, no bundler, no Alembic. Clone it and read it top to bottom.

### Conventions

Four things that will bite you if you don't know them:

- **Timestamps** use the `UTCDateTime` column type, not `DateTime(timezone=True)`.
  SQLite has no offset, so the latter silently returns naive datetimes and the
  first `utcnow() - stored` raises `TypeError`. Storage format is unchanged.
- **Enums** use `enum_column()` and are `enum.StrEnum`, so values store
  lowercase, read back as members, and format as `down` rather than
  `HealthStatus.DOWN` in an alert message.
- **Checks never raise.** They return a `CheckOutcome` with a reason. The state
  machine decides what a sequence of outcomes means; the check does not.
- **Migrations are a numbered list in `db.py`**, not Alembic. Append; never edit
  or reorder an entry that has shipped.

---

## Roadmap

| # | Increment | Status |
|---|---|---|
| 1 | Foundation — config, schema, auth, dashboard shell | ✅ done |
| 2 | SNMP collection engine + capability probe | ✅ done |
| 3 | Check engine — ping, TCP, HTTP, DNS, hysteresis, incidents, targets UI | ✅ done |
| 4 | Device discovery — sweep, MAC identity, devices page | ✅ done |
| 4c | History retention, scan schedule, subnet management + filter | ✅ done |
| 4b | Service discovery — port scan, Docker inventory | next |
| 5 | Service map — tree and filterable list views | planned |
| 6 | SNMP metric storage + device pages | planned |
| 7 | UniFi Network API collector (console CPU/temp, uplink topology) | planned |
| 8 | Alerting — Discord, dependency suppression, quiet hours | planned |

Reordered twice. After increment 2 the check engine moved ahead of SNMP
storage, because a monitor that cannot tell you anything is down is not yet a
monitor. After increment 3, alerting moved to last: the inventory and map work
is the substance, and the live-updating pages cover "is anything wrong" well
enough to watch while the rest is built.

The trade-off that buys is real and worth stating plainly — until increment 8,
SPARK only tells you about an outage while someone is looking at it. The
dependency suppression and incident records the notifier will need are already
built and tested, so the last increment is the notifier itself, not the
thinking behind it.

---

## Design decisions

Four choices that are expensive to change later, written down so they don't get
"simplified" away.

**Devices are keyed on MAC, not IP.** DHCP reassigns addresses. A tool keyed on
IP silently loses a device's entire history the first time a lease churns. This
is why `attached` per subnet is not cosmetic: ARP does not cross a router, so a
routed VLAN yields no MAC and falls back to the weaker identity.

**Incidents are rows, not a query.** Deriving "was it down, and for how long"
from raw check results at read time gets painful fast, and it is the question
you ask most.

**`service` and `target` are separate tables.** A service is a fact about the
network, discovered whether or not you care. A target is a decision to watch
something. Merge them and you either monitor everything you find (noise) or lose
the inventory of what you chose to ignore.

**Live updates push, they do not poll.** `/events` holds one connection per
open tab and the check runner publishes on state transitions only. Polling
costs a request per tab per interval forever and is still late; publishing on
every check would refetch the page once per check per tab, which turns a
monitor into load on the thing being monitored.

**History is downsampled, never deleted outright.** Raw results for 7 days,
5-minute buckets for 90, hourly for 2 years, with per-status counts kept at
every width. Retention never VACUUMs: freed SQLite pages are reused by new
inserts, so the file plateaus instead of being rewritten nightly.

**Hysteresis is not optional.** A target needs N consecutive failures before it
changes state. This single detail is the difference between a tool you trust and
one you mute within a week.

See `DESIGN.md` for the full design document and `spark/CHANGELOG.md` for what
changed when.

---

## License

Not yet chosen. Pick one before sharing this outside your own network.
