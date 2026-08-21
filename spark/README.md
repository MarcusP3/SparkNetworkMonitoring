# ⚡ SPARK

Network monitoring for homelabs. Health checks, alerting, and a live map of
every device and service on your network — in one container, with one SQLite
file to back up.

Homelab tooling makes you choose between uptime checkers that know nothing
about your network and enterprise NMS platforms built for a full-time operator.
SPARK aims at the middle: it discovers the network itself and shows you what's
actually running, rather than making you type it all in.

> **Status: increment 1 of 5.** Configuration, database, authentication and the
> web shell are working. The check engine, alerting, discovery, and the service
> map are not built yet. See the roadmap below.

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

Default is a single admin account with a password (Argon2id, signed session
cookie, rate-limited login).

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
| 2 | Check engine — ping, TCP, HTTP, DNS, state machine with hysteresis | planned |
| 3 | Alerting — Discord, dependency suppression, quiet hours | planned |
| 4 | Discovery — subnet sweep, remote Docker inventory, curated port scan | planned |
| 5 | Service map — tree and filterable list views | planned |

After v1: change tracking and new-device alerts, SNMP with automatic topology,
then traffic analysis.

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
  models.py     full Phase 1 schema
  db.py         engine, sessions, migration runner
  auth.py       Argon2 passwords, sessions, proxy mode
  main.py       app factory and entry point
  web/          routes and dependencies
  templates/    Jinja templates
  static/       hand-written CSS, no build step
```

No npm, no bundler, no Alembic. Clone it and read it top to bottom.
