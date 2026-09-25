# <img src="spark/src/spark/static/brand/favicon.svg" width="36" height="36" alt="" align="top"> SPARK

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
| Service discovery — TCP port scan, watch a service in one click | ✅ working |
| History retention — nightly downsample and prune | ✅ working |
| Hysteresis, incident tracking, dependency suppression | ✅ working |
| Target management UI (`/targets`) | ✅ working |
| SNMP collection — profiles, Test, scheduled polling, history, device pages with charts | ✅ working |
| Alerting (Discord) — down/recovered, SNMP silence, new devices, quiet hours | ✅ working |
| Docker inventory — container lists via a read-only socket proxy | ❌ not yet |
| Service map, topology | ❌ not yet |

SPARK tells you when something goes down, in Discord, and when it comes back —
see [Alerts](#alerts).

---

## Contents

- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Monitoring](#monitoring)
- [Preferences](#preferences)
- [Alerts](#alerts)
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
mkdir -p data && sudo chown 9700:9700 data    # the container runs as uid 9700, not root
docker compose up -d --build
```

Create `data/` yourself, before the first `up`: if Docker creates it for the
bind mount it belongs to root, and SPARK refuses to start (with this same
command in the message) rather than run as root.

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
| Web UI | Everything you would change routinely: targets, subnets and VLAN tags, the scan schedule, SNMP profiles and the polling interval, the Discord webhook and alert settings, and later retention |

Any YAML value can be overridden by environment variable, nesting with double
underscores: `SPARK__APP__PORT=9800`, `SPARK__AUTH__MODE=proxy`.

### Dependencies and the supply chain

15 direct dependencies, 39 packages in the full closure, no npm and no build
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
cap_drop: [ALL]         # nothing Docker grants by default is needed...
cap_add: [NET_RAW]      # ...except real ICMP; without it ping degrades to TCP probes
volumes: [./data:/data] # spark.db and the session key must survive a rebuild
```

On a bridge network SPARK sits behind NAT and discovery finds **nothing**. This
is the single most common way to end up with an empty dashboard.

The container runs as **uid 9700, not root**. The `./data` bind mount has to be
owned by that uid — `sudo chown -R 9700:9700 data` from `spark/` — and SPARK
refuses to start, printing that command, if it is not. ICMP still works for
the unprivileged user because `CAP_NET_RAW` is attached to the Python binary
as a file capability; that is also why the compose file must **not** set
`no-new-privileges`, which makes the kernel ignore file capabilities and
would silently degrade ping.

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

## Preferences

Click your name in the top bar. **Time zone** sets the zone every time in
SPARK is shown in — page timestamps, chart axes and hover readouts — and the
zone quiet hours are kept in. It is chosen first at setup (the browser's own
zone is preselected), and Preferences offers the browser's zone whenever it
differs from the saved one. Times are stored in UTC regardless; only how they
read changes.

**Sign-in timeout** is how long SPARK stays signed in without being used:
30 minutes unless changed, with choices from 15 minutes to 24 hours and no
"never". Clicking, typing and opening pages count as use; a page updating
itself does not, so a dashboard left open still times out, and the tab goes
back to the sign-in page on its own. Sign in again and you land where you
were. In proxy mode the proxy decides instead, and the card says so.

Both are settings for the instance, since SPARK has one account.

## Alerts

**Settings → Alerts.** Paste a Discord webhook (in Discord: Server Settings →
Integrations → Webhooks → New Webhook → Copy Webhook URL), save, and press
**Send a test**. The URL is stored encrypted, like SNMP credentials, and never
shown again; only Discord's own hosts over https are accepted.

What is sent — changes only, never a reminder that something is still down:

| Event | When |
|---|---|
| A target is down | After its failure threshold (consecutive failed checks) |
| A target is back up | After its recovery threshold, with how long it was down |
| An SNMP device stopped answering | Three missed polls, and at least three minutes |
| An SNMP device is answering again | The next poll that answers, with how long it was silent |
| New devices on the network | One message per sweep that found any |

What is deliberately not sent:

- a target whose failure is explained by one it **depends on** being down
  (set on the target) — the switch goes, you get one message, not thirty;
- a recovery for an outage that was never alerted (it went down while alerts
  were off, or its dependency explained it);
- **degraded** — a warning on the page, not a page for you;
- SNMP silence on a device a target already reports down, or on a device that
  has never answered (that is a configuration problem, shown on the SNMP card);
- the devices from the very first sweep, which are all new and none of them news.

**Quiet hours** hold alerts and send one summary when the window ends. The
window is kept in the time zone set under **Preferences**.

**If Discord is unreachable** alerts wait and retry after 30 s, 2, 10 and 30
minutes, then are marked failed; a webhook Discord says does not exist fails at
once. Discord's rate limit is honoured, and a burst of four or more alerts is
sent as one message. The **Recent** list on the card shows every alert and
what happened to it.

How it works: the decision to alert is written in the same database
transaction as the change that caused it (an outbox, the `notification`
table), and a job every 15 seconds sends what is due without holding the
database. A crash cannot lose an alert or send one for a change that never
committed, and a slow Discord never delays a check.

## SNMP

### Add a device and test it

**Settings → SNMP.** Create a profile — a v2c community, or a v3 user with its
authentication and privacy passwords — then add discovered devices to it and
press **Test**. SPARK asks each device a set of read-only questions and records
which ones it can answer, so you can see what it will be able to collect before
it collects anything. Most homelabs need exactly one profile.

**Add common defaults** creates a v2c profile with the factory read-only
community, `public`, in one click (the button disappears once any v2c profile
uses `public`). `private` is deliberately not offered: by convention it is the
read-write community, SPARK never writes, and v2c would send it in clear text
— to every device, when **Find SNMP devices** is pressed.

**SPARK does not look for SNMP devices on its own** — it polls the devices on
the list, and only those. To find candidates, press **Find SNMP** at the top
of the Devices page, or **Find SNMP devices** on the same card. It sends one read-only question (the device's name) to every
discovered device not already listed, trying each profile in turn, and lists
the ones that answer with the profile that worked. **Add** or **Add all** puts
them on the list; nothing is added until you do. About 250 devices take around
ten seconds, and the results appear without reloading. On the Devices page,
each device that answered gets an **Add** button in its SNMP column (with the
profile it answered), one that refused the credentials says `refused`, and the
filter's **Answered Find, not polled** shows just those. Without a profile the
button reads **Set up SNMP** and goes to the SNMP settings.

It runs only when pressed, on purpose: with a v2c profile it sends the
community string, in clear text, to every device it tries. A device with SNMP
off or a wrong community gives no answer at all; only SNMPv3 agents say the
credentials were wrong, and those are listed separately.

**Which devices are polled** shows on the Devices list, in the **SNMP** column —
`polling`, `no answer`, `paused` or `waiting` (added, first poll not yet run),
each linking to the device's charts — and the **SNMP** filter narrows the list
to polled or unpolled devices.

Credentials are **encrypted in the database** under a key derived from
`secret.key` in the data directory, and never shown again once saved — an edit
form leaves a secret blank to keep it. A copied or backed-up database is useless
on its own. The flip side: **back up `secret.key` with the database**, or every
credential has to be entered again after a restore.

A failed Test says which kind of failure it was, because they need different
fixes:

| Result | Means |
|---|---|
| `AuthFailed` | The device answered and rejected the credentials — wrong v3 user or auth password |
| `Unreachable` | No answer at all. Down, SNMP off, *or* a wrong community or privacy password — an agent drops a request it cannot authenticate rather than refusing it, so from outside these look identical |
| `CipherUnavailable` | SPARK cannot encrypt v3 traffic. SPARK's fault, not the device's |

### Polling

Every device on the SNMP list is polled on a schedule — **every 60 seconds by
default**, set with **Poll every** on the card (30 seconds to an hour). A device
added to the list gets its first poll within a few seconds. **Pause** stops
polling one device and keeps its history; **Remove** takes the device off the
list *and deletes its history*.

Each poll asks for:

- uptime, CPU % (or the load average, where the device has no percentage),
  memory %, and the hottest temperature sensor, if there is one;
- every interface's name, status, speed and counters.

Model and serial are left to Test; they do not change minute to minute, and on
a large chassis that walk is the heaviest one.

The card shows the latest poll for each device — `polling` with the numbers,
or `no answer` with the reason — and when the next one is due.

### The device page

**Devices → Details** (or a device's name on the SNMP card) opens
`/devices/<id>`. For a device on the SNMP list:

- **Health** — CPU (or load average), memory and the hottest temperature
  sensor, each charted over **1h, 24h, 7d or 30d**. Charts that would be empty
  are left out.
- **Interfaces** — every port with its status, current rate in and out, the
  peak in the range, errors, and a small trace of the range. Choose a port to
  chart its traffic; the busiest one is charted to begin with.
- The solid line is the average for each point on the chart; where a point
  covers several polls, a faint line shows the busiest of them, so a
  five-minute spike still shows on a 30-day chart. Hover for the reading.
- Gaps are gaps. A stretch the device did not answer is a break in the line,
  never a drop to zero, and the page says what share of polls were answered.

Only `up` gets a status colour. An empty switch port reads `down` to SNMP, and
a page of red for unplugged ports would be a page of alarms about nothing.

Charts are drawn by SPARK itself as SVG — no chart library, no CDN — and times
are shown in your browser's time zone. The page reads whichever history
tables cover the range (raw, five-minute, hourly) and combines them weighted
by sample count, so a 30-day chart is continuous across the 7-day boundary
where raw samples turn into rollups.

A device that is not polled gets the same page with its sweep details and open
services, and a pointer to the SNMP card.

**Traffic is stored as a rate per interval, and only for interfaces that are
up.** Every interface's status is always recorded; an empty port just doesn't
add a row of zeros every minute. Turning counters into rates has four traps,
and each one produces a wrong number rather than an error:

| Case | What SPARK does |
|---|---|
| A 32-bit counter wraps (every 4 GiB — about 34 s at a full gigabit) | Adds the wrap back once. If the result is faster than the link, it was more than one wrap and is discarded |
| A 64-bit counter goes backwards | A reset, not a wrap: new baseline, no rate |
| The device rebooted (`sysUpTime` went backwards) | New baseline, no rate |
| More than three intervals since the last answer | New baseline, no rate — an hour's average is not a one-minute sample |

A device that offers only 32-bit counters can't be measured above about
570 Mbps at 60-second polling (4 GiB per minute). `spark-probe` warns when a
device has only 32-bit counters.

History follows the **same retention as checks**: raw samples for a week, then
five-minute averages *and peaks* for 90 days, then hourly for two years. Same
settings, same nightly job.

### Find out what your gear actually supports, from the command line

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
| CPU % | HOST-RESOURCES-MIB `hrProcessorLoad` | Unavailable on some devices, and for the first minute of any net-snmp agent — see below |
| Load average | UCD-SNMP-MIB `laLoad` | Fallback when CPU % is unavailable |

**When a device offers no CPU percentage, SPARK will not invent one.**
`cpu_percent` is `None` rather than a fabricated `0`, and the load average is
reported in its own right. A load of 1.4 on a four-core box is not 140% CPU and
is not displayed as though it were.

**net-snmp reports no CPU percentage for its first minute.** `hrProcessorLoad`
(and the older `ssCpuIdle`) are one-minute averages, empty until the agent has
sampled for a minute. A Test run straight after restarting `snmpd` shows CPU
as unsupported; run it again a minute later. An earlier version of this README
said modern net-snmp never serves them — that was measured against a test agent
that had just started, and is wrong. pfSense, OPNsense and Linux hosts that
have been up for more than a minute report CPU % normally.

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
hands over no usable sessions, and revocation is a row update. It is marked
`Secure` when the login itself arrived over HTTPS.

A session ends after 30 minutes without use (changeable under
[Preferences](#preferences)), and in any case after `auth.session_days` (30
days). The timeout is enforced by the server against the session's last-seen
time, which is written at most once a minute, so a session can end up to a
minute early but never late. Requests a page makes by itself — the live
refresh, the event stream, the timeout check — send `X-Requested-With: fetch`
and do not count as use. Raising the timeout does not revive sessions that
had already timed out: they are deleted when it is saved.

Every response carries a Content-Security-Policy that allows only SPARK's own
origin (inline scripts run on a per-request nonce), `frame-ancestors 'none'`,
`nosniff`, and `no-store` on pages. Every POST is checked against its `Origin`
header and refused if it came from another site, as a second layer over
`SameSite=Lax`. Behind a reverse proxy, keep the `Host` header intact (the
default everywhere) or every form will answer 403. There is no OpenAPI
document or Swagger page; there is no API.

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

> **Known gaps.** There is no TLS of SPARK's own; on plain HTTP the session
> cookie cannot be `Secure` without silently never being sent, so put it
> behind a reverse proxy before exposing it beyond the LAN. Rate limiting is
> per source IP, which is the right key for a single-account instance but
> means a lockout is also a way to lock the real admin out from that address
> for fifteen minutes.

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
    snmp_config.py      SNMP credential profiles, devices, and Test
    snmp_poll.py        scheduled SNMP polling; counters to rates, wraps and resets
    snmp_history.py     SNMP history as chart-sized series, across raw and rollups
    snmp_discover.py    Find SNMP devices: try the profiles, suggest what answers
    prefs.py            preferences: time zone (the `local` filter's formatting), sign-in timeout
    alerts.py           deciding what to alert on; the Discord outbox and dispatcher
    charts.py           server-rendered SVG charts; no chart library
    vault.py            encryption for stored credentials (key from secret.key)
    port_catalogue.py   which ports the scan looks at, and what they cost
    discovery/
      sweep.py          ICMP sweep, ARP table, reverse DNS
      oui.py            MAC prefix to vendor
      ports.py          the default port list and the TCP connect scan
      services.py       recording what a scan found, without losing history
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
      routes_device_page.py  the per-device page
      hardening.py      security headers, CSP nonces, same-origin check on writes
    templates/          Jinja templates
    static/             hand-written CSS, no build step
      charts.js         local times and hover readouts on charts
      fonts/            Inter + JetBrains Mono, self-hosted (OFL-1.1)
      brand/            favicon (SVG + ICO) and Apple touch icon
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
    test_ports.py       the scanner against real sockets; the service store
    test_paging.py      one device per page, exactly once, whatever the filter
    test_port_catalogue.py  the editable port list, and what it costs to scan
    test_snmp.py        pure-function tests, live tests that skip without an agent
    test_snmp_crypto.py SNMPv3 privacy actually works; failures are told apart
    test_snmp_settings.py  profiles, devices, Test; secrets absent from DB and pages
    test_snmp_poll.py   counter wraps and resets, recording, scheduling, SNMP history
    test_device_page.py history read back weighted and gap-true; charts; the page
    test_layout.py      every table scrolls inside its card
    test_snmp_discover.py  Find suggests and never adds, from Settings or Devices; the SNMP column and filter
    test_alerts.py      what is sent and what is not; retries, rate limits, quiet hours
    test_preferences.py the time zone: set at setup and in Preferences, used on pages, charts, quiet hours
    test_idle_timeout.py  sign-in timeout: enforced, not extended by background requests, tab sent to sign-in
    test_vault.py       credential encryption, key derivation, the key file
    test_hardening.py   headers, CSP nonces, cross-site POSTs, proxy-mode fixes, form bounds
    local_agent.sh      throwaway net-snmp agent on 127.0.0.1:11161, v2c and v3
```

No npm, no bundler, no Alembic. Clone it and read it top to bottom.

### Conventions

Ten things that will bite you if you don't know them:

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
- **SNMP counters** use the `Counter64` column type. They run to 2^64 − 1 and a
  plain `Integer` raises `OverflowError` above 2^63 − 1.
- **Every table sits in `<div class="table-scroll">`.** Without it, a table
  wider than the window widens the whole page instead of scrolling inside its
  card. `tests/test_layout.py` checks every template.
- **Show times with `| local`**, never `.strftime` in a template. Stored
  times are UTC; the filter shows them in the zone chosen under Preferences.
- **No inline styles.** The CSP allows styles from `'self'` only, so a
  `style="…"` attribute is silently ignored. Position things with classes (the
  chart labels sit at fixed quarters for exactly this reason); a script may set
  `element.style`, which the policy allows.
- **Retention cutoffs are aligned to the bucket width** (`retention._floor`).
  A new downsampled series must use the aligned cutoffs, or a bucket straddling
  the cutoff loses its later half the next night, silently.
- **Cyan is the brand, never a status.** `--accent` is for SPARK itself and for
  things you can click. Up, degraded and down are green, amber and red; unknown
  and paused are grey. The reverse holds too: status colours appear only on
  statuses, and never on charts: the first series on a chart is cyan, the
  second neutral grey. Icon tiles are cyan unless what they count is actually happening:
  the Degraded, Down and Open incidents tiles turn amber or red only when their
  number is above zero, on the same condition as the number itself. A red that
  is always there is a red you learn to stop seeing. No blue "info" state — it
  read as a fourth status beside
  the cyan. Nothing loads from a CDN, fonts included: SPARK runs on LANs with no
  internet. The mark is the flat-top bolt in `templates/_brand.html`; its path
  is duplicated in `static/brand/favicon.svg`, so change both together.

---

## Roadmap

| # | Increment | Status |
|---|---|---|
| 1 | Foundation — config, schema, auth, dashboard shell | ✅ done |
| 2 | SNMP collection engine + capability probe | ✅ done |
| 3 | Check engine — ping, TCP, HTTP, DNS, hysteresis, incidents, targets UI | ✅ done |
| 4 | Device discovery — sweep, MAC identity, devices page | ✅ done |
| 4c | History retention, scan schedule, subnet management + filter | ✅ done |
| 4b | Service discovery — TCP port scan, services on devices | ✅ done |
| 4d | Docker inventory — read-only socket proxy | next |
| 5 | Service map — tree and filterable list views | planned |
| 6 | SNMP — credentials, Test, polling, history, device pages | ✅ done |
| 7 | UniFi Network API collector (console CPU/temp, uplink topology) | planned |
| 8 | Alerting — Discord, dependency suppression, quiet hours | ✅ done (ahead of 4d, 5, 7) |

Reordered twice. After increment 2 the check engine moved ahead of SNMP
storage, because a monitor that cannot tell you anything is down is not yet a
monitor. After increment 3, alerting moved to last: the inventory and map work
is the substance, and the live-updating pages cover "is anything wrong" well
enough to watch while the rest is built.

Then reordered back: once SNMP polling and device history existed there was
enough watching going on that "only while someone is looking at the page" had
become the biggest gap, so alerting (8) was built before 4d, 5 and 7.

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

Copyright © 2026 Marcus Pierce. SPARK is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE.md).

Free for personal, homelab, hobby and educational use. Any commercial use —
including use inside a business, resale, rebranding, or hosting it as a
service — needs a separate license. Contact me through GitHub.
