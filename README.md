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
| Device inventory — naming, review state, watch one or many in one click | ✅ working |
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
| Network map and Services — declared tree, services list, alerts follow it | ✅ working (topology from SNMP not yet) |

SPARK tells you when something goes down, in Discord, and when it comes back —
see [Alerts](#alerts).

---

## Contents

- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Monitoring](#monitoring)
- [Network map and Services](#network-map-and-services)
- [Preferences](#preferences)
- [Alerts](#alerts)
- [SNMP](#snmp)
- [Credentials](#credentials)
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
      cidr: 172.16.10.0/24
      attached: true      # SPARK has an interface on this segment

    - name: Servers
      cidr: 172.16.30.0/24
      vlan: 30            # documentation only; nothing reads it
      attached: false     # reachable only through a router
```

This matters more than it looks. ARP only works on directly-attached layer 2
segments, and device identity keys on MAC address. Across a router SPARK can
ping a host but cannot learn its MAC, so devices on routed VLANs fall back to
IP-based identity — which breaks the moment DHCP hands out a different address.

Two ways to fix it: give the SPARK VM an interface in each VLAN, or put the
router on the SNMP list. Every 15 minutes SPARK reads the router's ARP table,
which has the MAC for every VLAN it routes, and fills in the MAC of each
device there that lacks one. A device that later turns up at a new address is
listed under **Possible duplicates** (see Duplicates under [Network map and Services](#network-map-and-services)) rather
than followed automatically.

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
discovered device with the **Watch** button at `/devices`. To watch many at
once, tick them in the Devices list (the box in the header ticks the whole
page) and press **Watch N** in the bar that appears: each gets a ping check,
exactly as its own Watch button would give it, and their first checks run
within seconds. Devices already watched, ignored, or without an address have
no box.

### Check types

| Check | Address | Useful params |
|---|---|---|
| `ping` | `172.16.10.1` | `{"count": 3, "loss_warn_percent": 1}` |
| `tcp` | `172.16.10.1:443`, or address plus `{"port": 443}` | — |
| `http` | `https://host/path` | `{"expect_status": 200, "expect_body": "ok", "cert_warn_days": 14}` |
| `dns` | `example.com` | `{"rdtype": "A", "server": "172.16.10.1", "expect": "172.16.10."}` |

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

## Network map and Services

**Network map** (`/map`) shows every device in its place — gateway, then
switches, then what hangs off each — with its status and the services the
port scan found on it.

**Services** (`/services`) is its own tab: a searchable list of every
service on the network ("plex", "8080", "nas", "192.168.1." all work), with
its status and a **Watch** button for the ones not watched yet. An old
`/map?q=…` link goes there. The dashboard's Services tile opens it too.

**Placing a device** is done on its own page, in the **On the network map**
card: a **Role** (gateway, switch, access point, server, client) and
**Connected to** (the device it is plugged into). Set it for your handful of
infrastructure; everything else can hang off its switch or stay in **Not
placed yet**, which the map lists rather than hides. A device cannot be
connected to itself or to anything below it.

**Found by SNMP.** With your switches on the SNMP list, SPARK reads their MAC
tables (BRIDGE-MIB and Q-BRIDGE-MIB) and any LLDP neighbours every 15
minutes, and works out which switch port each device is on. The map page
lists what that finds under **Found by SNMP**, to **Accept** one at a time,
**Accept all**, or mark **Not right**; each device's page says where SNMP
sees it ("office-switch, Port 5"). Nothing is placed until you accept it, and a
parent set by hand is never replaced. How it works:

- The gateway (the device whose Role is Gateway / router, or else the polled
  device holding the most addresses of its own) is the top. Each switch's
  uplink is the port it learned the gateway's MAC on.
- A device is on the nearest switch that sees it on a port leading away from
  the gateway. A switch no other switch sees hangs off the gateway.
- When one switch port has several devices and exactly one of them is
  infrastructure (polled over SNMP, or a gateway, switch, access point or
  server by role), the rest are behind it: wireless clients behind their
  access point, VMs behind their host.
- LLDP, where a switch has it, is exact and wins over the MAC tables.

**Manual or automatic.** Chosen at setup, and changed any time under
**Preferences → Network map**:

- **Manual** (the default): what SNMP finds is suggested, as above.
- **Automatic**: after every SNMP read (every 15 minutes) devices are placed
  where the switches see them, and a device automatic placed is moved when
  it moves. A place you set yourself, on a device's page or by pressing
  Accept, is never touched, and neither is one automatic set that you then
  changed or cleared. **Not right** on a device's page takes an automatic
  place back off for good.

**Wipe map** (Preferences → Network map) starts the map over: every role,
every place (including yours) and every "Not right". It shows what it will
clear and asks first. Devices, targets, services, alerts and history are
untouched. In automatic mode the map is rebuilt at once. If SPARK knows your
gateway only because you set its role, the page says so: set it again after
the wipe.

What it cannot see: unmanaged switches (what is behind one lands on the
port above it), and which of two access points on one port is the wired
one, as with a mesh AP. Set the mesh AP's parent by hand once and its
clients follow. Devices on the gateway's own ports show as on the gateway.
The SNMP **Test** lists "MAC address table" and "LLDP neighbours" when a
device has them.

**Duplicates.** A firewall with a gateway address on several VLANs shows up
once per address: across a router SPARK sees no MAC, so each address looks
like a device of its own. On the real device's page, **Addresses → Same
device as this one** merges a duplicate in. A preview says exactly what will
happen first. Its address becomes an extra address of the device, its
targets (with their history), services and anything connected below it move
across, and later sweeps count that address as the same device. Removing
the address undoes it: the next sweep finds it as a device of its own. Two
devices with different MACs, or both polled over SNMP, are not merged.

**Suggested by SNMP.** With the firewall (or any router or multi-homed
server) on the SNMP list, SPARK reads its own addresses and its ARP table
every 15 minutes, and a device found at one of its addresses, or at a MAC
another device already has, is listed under **Possible duplicates**: on the
Devices page and on both devices' pages. Nothing is merged until you press
**Merge** on the usual preview. **Not the same** stops that suggestion for
good. The SNMP **Test** lists "Its own IP addresses" and "ARP table" when the
device supports them.

**Folding.** A device with things connected below it has a ▾ beside it:
click to fold that branch away ("12 below ▸"), or use **Collapse all** /
**Expand all**. What you fold is remembered in that browser. Ports: the ones
you watch with a target show on each device; the rest are folded under their
count ("+6 more") and open on a click. The **Services** tab still lists
every one.

**Status** comes from the device's targets (the worst of them), or from SNMP
polling if it has none, or reads *not watched*. A service watched by a
target shows that target's status.

**Alerts follow the map.** While a device is down, nothing below it alerts:
the core switch goes, you get one message for the switch, not one for each
server behind it. The failures are still recorded, flagged as explained by
their dependency. This works alongside a target's own **depends on**, which
still applies.

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

**The dashboard's incidents** are every kind of problem, not only outages: a
target down, and every alert rule that fired — SNMP thresholds, starred
ports, storage, a device that stopped answering SNMP, an API credential that
stopped working, a drive with errors, TrueNAS's own alerts. Each shows where
it came from (Target, SNMP, Port, Storage, API, TrueNAS), links to its
device, and stays *ongoing* until the rule clears. **Open incidents** counts
both kinds. They are recorded even for a muted device (the problem was real;
only the message is held back), but not while a rule is switched off.

**Suppressions** (Settings → Suppressions) are for a device that breaks a
rule by design — ZFS keeps TrueNAS's memory nearly full on purpose. One
rule, for one device: **off**, or **its own line** ("memory over 100%",
"CPU over 98%"). Lines apply to CPU, memory, temperature, port traffic, pool
and disk space, and drive temperature; the other rules can only be off.
TrueNAS's own alerts can be suppressed one type at a time (PoolUSBDisks),
or all at once. A suppressed rule is fully quiet — no message and no
incident — and every other rule and device is untouched. Saving or removing
one starts that rule afresh for that device, so an alert standing at the
time closes as *suppressed*. Each alert on the dashboard's Recent incidents
has a **Suppress** link that fills the form in. (Muting is the other tool:
the whole device, still recorded, only not sent.)

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
| A **starred** port is down / back up | Down on two polls in a row |
| A **starred** port is busy / back to normal | Over 80% of its speed for 10 minutes |
| CPU / memory high, back to normal | Over 90% for 10 minutes |
| Temperature high, back to normal | Over 80 °C for 5 minutes |

The last four are **SNMP alerts**, from what polling already collects. The
numbers are defaults, each rule can be switched off, and all of them are set
on the SNMP alerts card. A value has to stay over the line on every poll for
the whole time; it clears only when it is back under by 5 (% or °C), so a
value hovering on the line does not send a message every few minutes; a gap
in polling longer than three intervals starts the count again. Ports alert
only when **starred** — press the star beside a port on its device's page.
Most ports on a switch are desks and access points whose links come and go,
so the default is quiet; star the uplinks, the NAS, the server links. Every
starred port is listed on the card with an Unstar button.

**The mute list** is one global list of things that never alert: a whole
device (its targets, its SNMP polling and its ports) or a single target.
Muted things are still checked, polled and shown; only the messages stop.
Add to it from the Muted card, or with **Mute alerts** on a device's page.

What is deliberately not sent:

- a target whose failure is explained by one it **depends on** being down
  (set on the target), or by a device above it on the **network map** being
  down — the switch goes, you get one message, not thirty;
- a recovery for an outage that was never alerted (it went down while alerts
  were off, or its dependency explained it);
- **degraded** — a warning on the page, not a page for you;
- SNMP silence on a device a target already reports down, or on a device that
  has never answered (that is a configuration problem, shown on the SNMP card);
- the devices from the very first sweep, which are all new and none of them news;
- anything on the mute list.

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
- **Storage**, where the device reports any, read every 5 minutes:
  - **TrueNAS**: each pool's health (ONLINE, DEGRADED…) and space, and each
    drive's temperature. TrueNAS's pool table has no sizes, so a pool's
    space is its root dataset's used and available. TrueNAS answers these
    slowly (it works them out when asked), which is why they are read on
    their own schedule with a 30-second timeout rather than every poll.
  - **Anything running net-snmp** (Linux servers, Proxmox hosts): each real
    filesystem's size, use and free space. Memory, /run, snaps and
    container layers are left out, and a bind mount is shown once.
  - Alerts, under **Settings → Alerts → Storage**: a pool not ONLINE (at
    once), a pool 85% full or more, a disk 90% full or more (both on two
    reads in a row), a drive at 50 °C or more for 10 minutes. Each ends 5
    under its line, and says so.
- **Interfaces** — one port's traffic chart, the busiest to begin with.
  Pick another from the **Interface** list (each shows its status and rate
  now); under the chart are its current rate, peak in the range, errors, and
  its star for alerts. **All N interfaces**, folded away beneath, is the full
  table: every port's status, rates, errors and a small trace of the range.
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
spark-probe 172.16.10.2 -c your-community

# inside Docker
docker compose run --rm spark spark-probe 172.16.10.2 -c your-community
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

## Credentials

Settings → Credentials holds keys for devices' own APIs — TrueNAS now, the
UniFi controller later. SNMP communities and v3 users stay under Settings →
SNMP.

### TrueNAS

SPARK talks to TrueNAS the way TrueNAS now asks to be talked to: JSON-RPC 2.0
over a WebSocket at `wss://<host>/api/current`. The REST API was deprecated in
25.04 and is gone in 26, so it is not used.

1. In TrueNAS, make a key for a user that can only read — a service account
   with the **Read-only Administrator** role is ideal. Keys are made under the
   user menu (top right) → **My API Keys**.
2. In SPARK, Settings → Credentials → **Add a credential**: TrueNAS, a name,
   the device, and the key. The address defaults to the device's; set one only
   if TrueNAS answers on another address or port.
3. SPARK connects and shows the certificate's SHA-256 fingerprint. **The key
   has not been sent yet.** Compare the fingerprint with TrueNAS (System →
   Certificates) and press **Trust this certificate**.
4. SPARK logs in and shows *connected*, with the TrueNAS version and host name.

**HTTPS only, always.** TrueNAS revokes a key that is ever sent over plain
HTTP, and there is no fallback to it here.

**The certificate is pinned, the way SSH pins a host key.** TrueNAS ships a
self-signed certificate, so ordinary verification would always fail. Instead
SPARK remembers the one you trusted and sends the key only down a connection
presenting it. A renewed or replaced certificate stops everything, with the
new fingerprint shown, until you trust it again. Changing the address or the
key forgets the trusted certificate.

The key is encrypted as soon as it arrives (the same vault as SNMP secrets),
is never shown again, and never appears in a page — an edit with the key field
left empty keeps the saved one.

**Checked every 5 minutes, and shown on the device page.** Once its
certificate is trusted, SPARK logs in with each credential every 5 minutes.
The device it belongs to gets a card saying *connected* (with the TrueNAS
version and when it was last checked), *waiting for you* (a certificate to
check first), or *not connected* with the reason, and a Test button. If a
credential fails two checks in a row — a revoked key, a replaced
certificate, TrueNAS down — SPARK alerts, and again when it works. The rule
is under Settings → Alerts → APIs; the mute list applies.

**Drive health and TrueNAS's own alerts.** The same 5-minute check reads,
with query methods a Read-only Administrator may call (checked against a
real 25.10 box): `pool.query` (each pool's status, last scrub, and its
topology, whose disks carry ZFS's read/write/checksum error counts),
`disk.query` (model, size), `disk.temperatures`, and `alert.list`. The
device's Storage card then shows a drive table — pool and vdev, state,
errors, temperature — the last scrub beside each pool, and TrueNAS's
current alerts. Settings → Alerts → APIs has two more rules, both on by
default:

- **A drive is not ONLINE, or has errors** — any read, write or checksum
  error in its pool; again once it is ONLINE with none (after the pool is
  cleared in TrueNAS). Disks outside any pool, such as a boot stick, have no
  ZFS state and are not alerted on.
- **TrueNAS raises an alert** — WARNING and above, once each (SMART
  failures arrive this way), and again when TrueNAS clears it or it is
  dismissed there. INFO and NOTICE are shown on the page only.

A method TrueNAS will not answer leaves that part as it was last read; an
unanswered read is never taken as the all-clear. Serial numbers are not
stored.

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

**Input.** Every query is parameterised, every page is escaped by the template
engine, and nothing runs a shell (ping is ICMP from Python, ARP is read from
`/proc`), so a name or address containing SQL, markup or shell syntax is only
ever text. On top of that, every form field has a length cap (`limits.py`)
that the page enforces with `maxlength` and the server enforces again; a
request body over 64 KB is refused with 413 before any route reads it; an id
too large to exist is a 404 rather than a database error; and a malformed
form gets a page saying which field was wrong, never a stack trace or a JSON
dump.

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
    trusted_proxies: [172.16.10.5]
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
    limits.py           how long any input may be, the largest request, the largest id
    alerts.py           deciding what to alert on; the mute list; the Discord outbox and dispatcher
    snmp_alerts.py      SNMP threshold rules: starred ports down or busy, CPU, memory, temperature
    storage.py          TrueNAS pools and drives, filesystems anywhere; read every 5 minutes, and their alerts
    truenas.py          the TrueNAS API client: JSON-RPC over wss only, certificate pinning, login
    credentials.py      API credentials (Settings → Credentials): add, edit, Test, Trust
    suppressions.py     one alert rule, for one device, off or with its own line
    truenas_health.py   drive health and TrueNAS's alerts over the API: the alerts, the Storage card
    servicemap.py       the network map: the tree, each device's status, the services list
    hierarchy.py        a device's place: parents, loops refused, "is anything above it down"
    merge.py            merging a duplicate device into the real one; what moves, what is refused
    identity.py         SNMP own addresses and ARP tables: merge suggestions, MACs across routers
    topology.py         the map from SNMP MAC tables and LLDP: where each device is plugged in
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
      routes_credentials.py  Settings → Credentials
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
    test_alert_incidents.py  incidents for alert rules: opened on firing, closed on clearing, on the dashboard
    test_suppressions.py  per-device rule overrides: quiet, their own lines, afresh when saved; the page
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
    test_snmp_alerts.py  thresholds held for their time, no flapping, starred ports, the mute list
    test_storage.py     TrueNAS and hrStorage parsing (from a real 25.10 box), storage alerts, the Storage card
    test_credentials.py  the TrueNAS client against a TLS fake: nothing sent before Trust, pins, renewals; the page
    test_truenas_health.py  pool topology, disks and alerts in the shapes a real 25.10 box returns; drive and TrueNAS alerts
    test_service_map.py  the tree, statuses, search, placing devices, alerts quiet below a down device
    test_merge.py       merging duplicates; sweeps afterwards count the address as the kept device
    test_identity.py    SNMP address and ARP parsing, what is suggested and what never is, MACs filled in
    test_topology.py    the map from MAC tables and LLDP across network shapes; accept, dismiss, never overwrite
    test_preferences.py the time zone: set at setup and in Preferences, used on pages, charts, quiet hours
    test_idle_timeout.py  sign-in timeout: enforced, not extended by background requests, tab sent to sign-in
    test_vault.py       credential encryption, key derivation, the key file
    test_hardening.py   headers, CSP nonces, cross-site POSTs, proxy-mode fixes, form bounds
    test_no_homelab_details.py  no real host names or address scheme in anything shipped
    test_input_limits.py  impossible ids, oversized fields and bodies, nan, blank names; injection stays inert
    test_watch_selected.py  tick devices and watch them all; skips, duplicates, junk, first checks queued
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
| 5 | Network map and Services — tree and filterable list views | ✅ done (Docker containers join it with 4d) |
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
