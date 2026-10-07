# Using SPARK

How each part of SPARK works, page by page. Deploying, upgrading and backing up are in the [README](../README.md).

## Contents

- [Monitoring](#monitoring)
- [Internet](#internet)
- [Network map and Services](#network-map-and-services)
- [Preferences](#preferences)
- [Alerts](#alerts)
- [SNMP](#snmp)
- [Credentials](#credentials)
- [Backup and restore](#backup-and-restore)

---

## Monitoring

Targets come from two places: added by hand at `/targets`, or promoted from a
discovered device with the **Watch** button at `/devices`. To watch many at
once, tick them in the Devices list (the box in the header ticks the whole
page) and press **Watch N** in the bar that appears: each gets a ping check,
exactly as its own Watch button would give it, and their first checks run
within seconds. Devices already watched, ignored, or without an address have
no box.

On the Targets list a target's name opens its device's page — the device it
was watched from, or the device at its address (a URL's host counts). One
with no device, such as an outside website, is plain text.

The Devices list has a search box (name, host name, any address, MAC or
vendor; press Enter) and filters by subnet, SNMP, API credential and whether
a device is watched. All of them go in the URL, so a filtered view can be
bookmarked. The automatic-scan schedule is at the foot of the Last sweep
panel and applies as soon as you change it.

**Excluded addresses** (Settings → Subnets, under the subnet list) are never
touched by the automatic scans: not pinged by the sweep (so never looked up
or recorded), not port-scanned or used as a port-scan control address, and
not tried by **Find SNMP**. Enter one address (`10.0.0.25`) or a start–end
range (`10.0.0.100-10.0.0.150`), with an optional note. What you set up by
hand still runs — a target, SNMP polling of a listed device, an API
credential. A scan already running finishes as it started; the next one
leaves them out, and the Last sweep panel says how many it left out.

The chips over the list show only the targets in one state, the search box
matches a name or address, and the check filter one kind of check; click a
column heading to sort by it. Each row's trend is its latency over its last
thirty results, in the colour of its state now. The Checks / min tile is the
load the intervals put on the engine, not a health figure.

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

An `http` check reads at most the first 1 MB of a response, and only when
`expect_body` is set; the phrase has to appear within that. A monitored host
that starts serving something enormous cannot push SPARK out of memory.

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

## Internet

The Dashboard's **Internet** card checks your connection every minute with a
few small checks rather than one ping, so it can say *what* is wrong:

- **Reachability** — pings to three public resolvers from different
  providers: Cloudflare `1.1.1.1`, Google `8.8.8.8` and Quad9 `9.9.9.9`.
- **Quality** — latency, jitter and packet loss, from those that answered.
- **DNS** — `example.com` looked up through your own resolver and through
  `1.1.1.1`, so a dead DNS server reads differently from a dead line.
- **Web** — an HTTPS fetch of `https://www.gstatic.com/generate_204`, a page
  made for connectivity checks, which proves traffic really gets out.
- **Your gateway** — pinged too, when a device has the Gateway / router role.

It reads **Online**, **Degraded** (reachable, but with packet loss over 2%,
DNS or the web check failing — the card says which) or **Down**: no
resolver answers *and* the web check fails. One provider having a bad
minute, or a network that drops ping, is not an outage.

**The internet is down** (Settings → Alerts) alerts after two down checks
in a row, a minute apart, and again when it is back, and each outage is an
incident on the Dashboard. If a target already watches the gateway and it is
down, that is the alert you get — not a second one about the internet.

The card also shows uptime over 24 hours and 7 days, latency over the last
24 hours, and recent outages — something to point at when talking to your
ISP. Checks are kept 30 days. **Turn off** on the card stops them: SPARK
then contacts none of those hosts. Speed tests are not part of it.

---

## Network map and Services

**Network map** (`/map`) shows every device in its place — gateway, then
switches, then what hangs off each — with its status and the services the
port scan found on it.

**Services** (`/services`) is its own tab: a searchable list of every
service on the network ("plex", "8080", "nas", "192.168.1." all work), with
its status and a **Watch** button for the ones not watched yet. An old
`/map?q=…` link goes there. The dashboard's Services tile opens it too.
The chips over the list show all, watched, not watched, or only those
**worth a look** (open ports with a known way to go wrong: telnet, FTP, SMB,
RDP, an open Docker socket, databases and the like), and a dropdown narrows
it to one subnet; all of it goes in the URL. Click Service, Port or Device to
sort. Beside the list, **Worth a second look** says why each flagged port is
flagged, and **Most common ports** lists the busiest ports, each a link to
every device running it. **Scan ports** here returns to this page.

**Placing a device** is done on its own page, in the **On the network map**
card: a **Role** (gateway, switch, access point, hypervisor, server, NAS,
UPS, client, camera) and
**Connected to** (the device it is plugged into). Set it for your handful of
infrastructure; everything else can hang off its switch or stay in **Not
placed yet**, which the map lists rather than hides. A device cannot be
connected to itself or to anything below it.

**A device that moved** (a new DHCP lease, a changed static IP) shows up as
a new device at its new address, often with a merge suggestion. On the
merge page, **Moved** keeps the original device -- name, history,
targets, SNMP -- at the new address and lets the old address go, and
points its targets at the new one; **Merge** would instead keep the old
address as an extra one.

**Removing a device** is the last card on its page: **Remove** and then the
confirm. It takes everything SPARK keeps only for that device (its targets
and their history, SNMP polling and charts, ports, extra addresses).
Anything connected to it is left not placed. A device still on the network
comes back at the next sweep as a new one; to keep it off the list, use
**Ignore** on the Devices page instead.

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
targets (with their history), services, anything connected below it, and
any API credential or Docker host tied to it move across, and later sweeps count that address as the same device. Removing
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

**Finding things.** The map is built to be read without scrolling past
everything:

- **Find** (the box above the map) matches a device's name, address, MAC,
  vendor, role, or any of its ports ("plex", "192.168.1.4", "445"). It
  keeps the matches and the path down to them, and highlights the match.
- **Problems only** keeps devices that are down, degraded or not answering,
  or with a watched port that is, and the path to them.
- **Infrastructure / Everything.** Gateways, switches, access points and
  servers are rows. The end devices under each (phones, laptops, cameras)
  are small tiles in a grid: in **Infrastructure**, the default, folded to
  a count ("9 devices · 1 with a problem ▸") that opens on a click; in
  **Everything**, all shown.
- **Folding.** A device with things below it has a ▾ at the start of its
  row: fold that branch away (the arrow turns ▸), or **Collapse all** /
  **Expand all**. Finding ignores folds while it is in use.
- **Found by SNMP** and **Not placed yet** are one line each at the top
  until opened.
- Ports: the ones you watch with a target show on each device; the rest
  are a count ("+6 ports") linking to them on the **Services** tab.

Folds, the view and opened groups are remembered in that browser. Without
JavaScript every branch and device simply shows.

**List / Diagram.** The switch in the Network panel's header draws the same
map top-down instead: gateway at the top, then switches and access points as
boxes, servers with nothing below them in one *Servers* group, and end
devices in a group under what they plug into. Lines turn amber or red along
the path to anything degraded or down. Drag to pan, Ctrl or ⌘ and scroll to
zoom (or the + / − / Fit buttons), click a device to open it. Find and
Problems only dim what does not match. The choice is remembered in that
browser; on a phone the list is always shown.

The tiles over the map count what is placed, how much of it is network gear
(gateways, switches, access points) and servers (servers and NAS), what has
a problem, what is not placed yet, and how many places SNMP is suggesting.

**Status** comes from the device's targets (the worst of them), or from SNMP
polling if it has none, or reads *not watched*. A service watched by a
target shows that target's status.

**Alerts follow the map.** While a device is down, nothing below it alerts:
the core switch goes, you get one message for the switch, not one for each
server behind it. The failures are still recorded, flagged as explained by
their dependency. This works alongside a target's own **depends on**, which
still applies.

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

**Account** changes the password — the current one, then the new one twice —
and lists everywhere the account is signed in (when, from which address, which
browser, last used), with **Sign out everywhere else** to end all of them but
the one you are using. A session that has timed out is not listed — it can
never be used again — and is deleted at startup and in the nightly
clean-up. Changing the password ends every session, this one included, and
gives your browser a fresh one on the spot.

If the password is lost, on the machine SPARK runs on:

```bash
docker compose exec spark spark-reset-password
```

It prompts twice, sets the new password and signs out every session. There is
no reset by email and no back door from the network: being at the machine is
the credential. In proxy mode the card only says that the proxy owns sign-in.

The time zone and the timeout are settings for the instance, since SPARK has
one account.

---

## Alerts

**The Alerts page** (Alerts in the menu) has everything about alerts in one
place. **Activity** lists what is firing now — every open outage and alert,
newest first — and the history of everything that has ended, suppressed ones
included, 25 to a page. Narrow the history by source (Target, SNMP, Port,
Storage, API, TrueNAS, Proxmox, Internet) or by device. **Messages** is every
message SPARK decided to send and what became of it: sent, waiting, held for
quiet hours, failed (with Discord's answer), or not sent because alerting was
off or there was no webhook. The tiles count what is firing, what began in
the last 24 hours and 7 days, and messages sent and failed in the last 7
days. The rules themselves are still under Settings → Alerts and Settings →
Suppressions, linked from the page's menu. The dashboard keeps the newest
ten, with **All alerts →** to this page.

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
time closes as *suppressed*. The dashboard leaves suppressed alerts out:
the one closed by the suppression, and any earlier ones of a rule that is
now off for that device (they come back if the suppression is removed). A
rule given its own line keeps its history. Each alert on the dashboard's Recent incidents
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
| **Sign-in events** | The lockout tripping (once per window, with the address); a sign-in from an address no session has come from before; the password changed or reset; first-run setup completing. Off with "Sign-in events" on the Alerts card. |

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

---

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
the list, and only those. To find candidates, press **Find SNMP** in the Devices
page's Last sweep panel, or **Find SNMP devices** on the same card. It sends one read-only question (the device's name) to every
discovered device not already listed, trying each profile in turn, and lists
the ones that answer with the profile that worked. **Add** or **Add all** puts
them on the list; nothing is added until you do. About 250 devices take around
ten seconds, and the results appear without reloading. On the Devices page,
each device that answered gets an **Add** button in its SNMP column (with the
profile it answered), one that refused the credentials says `refused`, and the
SNMP filter's **Ready to add** shows just those. Without a profile the
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
as unsupported; run it again a minute later. An earlier version of these docs
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

Settings → Credentials holds keys for devices' own APIs — TrueNAS, Proxmox
and UniFi Network. SNMP communities and v3 users stay under Settings → SNMP.

On the Devices list, a device with a credential carries a pill with its kind
(TrueNAS, Proxmox, UniFi) by its name, which opens its card, and the **API**
filter narrows the list to devices with any credential or one kind — the
quick way to find the UniFi console or a Proxmox host. A credential only
shows there once it is given a device. One without a device still connects
and is checked, but its card shows on no device page: its row under
Settings → Credentials carries a **No device** note, and the API filter
says how many it is leaving out. Choose the device under **Edit** on the
credential.

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

- **A drive is failing** — not ONLINE in its pool, or any read, write or checksum
  error in its pool; again once it is ONLINE with none (after the pool is
  cleared in TrueNAS). Disks outside any pool, such as a boot stick, have no
  ZFS state and are not alerted on.
- **TrueNAS raises an alert** — WARNING and above, once each (SMART
  failures arrive this way), and again when TrueNAS clears it or it is
  dismissed there. INFO and NOTICE are shown on the page only.

A method TrueNAS will not answer leaves that part as it was last read; an
unanswered read is never taken as the all-clear. Serial numbers are not
stored.

### Proxmox

SPARK reads Proxmox VE over its REST API at `https://<host>:8006/api2/json`
with an API token, and only ever with GET.

1. On the Proxmox host (the node → Shell), make a user and a token that can
   only read. PVEAuditor is Proxmox's read-only role; with privilege
   separation on, the token needs the role itself as well as its user:

   ```bash
   pveum user add spark@pve --comment "SPARK monitoring"
   pveum acl modify / --users spark@pve --roles PVEAuditor
   pveum user token add spark@pve monitor --privsep 1
   pveum acl modify / --tokens 'spark@pve!monitor' --roles PVEAuditor
   ```

   The third command shows the token's secret once.
2. In SPARK, Settings → Credentials → **Add a credential**: Proxmox, a name
   (SPARK's own label), the device, and the **Token ID** and **Secret** as
   Proxmox showed them — for the commands above, `spark@pve!monitor` and the
   secret. A whole `USER@REALM!TOKENID=SECRET` pasted as the secret works
   too. On an edit, either half left empty keeps the saved one. Port 8006 is
   assumed; give `address:port` for another.
3. Compare the fingerprint SPARK shows with the node → System → Certificates
   (`pveproxy-ssl.pem` if there is one, otherwise `pve-ssl.pem`), or run
   `pvenode cert info` on the host, and press **Trust this certificate**.
   **The token has not been sent before this.**

The certificate is pinned exactly as for TrueNAS. SPARK writes each request
by hand on the TLS connection after checking the certificate, so the token
cannot leave before the check — an HTTP library would send the header first
and show the certificate after.

**What it reads, every 5 minutes:** `/version`; `/nodes` (each node's CPU,
memory, uptime); `/cluster/resources?type=vm` (every VM and container); and
for each online node `/status` (Proxmox and kernel version, CPU model),
`/storage?enabled=1` (each enabled storage, whether it is active, its
space), `/disks/zfs` (each pool's health) and `/disks/list` (each drive's
SMART health and wear). The device gets a **Proxmox** card with all of it.
Templates are left out; serial numbers are not stored.

`/disks/list` has Proxmox run `smartctl` on each drive, as its own Disks
page does, which wakes a drive that has spun down.

**Alerts** (Settings → Alerts; the mute list and suppressions apply):

- **A watched VM or container stops** — only guests you press **Watch** on,
  on the device page; not running on two checks in a row (so a restart is
  not news), or no longer listed; again once it runs. A stopped test VM
  nobody is watching is not a problem.
  Watched guests are also listed on the **Targets** page, under
  *Proxmox guests*, marked as coming from the Proxmox integration, with
  their state and an Unwatch button.
- **A pool is not ONLINE** — a ZFS pool not ONLINE, or an enabled storage
  that is not active (an NFS share gone); again when it is back.
- **A pool is … full** — a storage at or over the line on two reads, until
  it is 5 under. A shared storage counts once.
- **A drive is failing** — SMART health anything but PASSED (OK on SAS);
  again when it passes. UNKNOWN (a USB stick, a controller that hides SMART)
  is not decided on.

A call Proxmox will not answer leaves that part as it was last read, and a
node that did not answer is not taken to have lost its drives.

### UniFi

SPARK reads UniFi Network over Ubiquiti's official Integration API, on the
console itself: `https://<console>/proxy/network/integration`, with an API
key in the `X-API-KEY` header, and only ever with GET. It is written to
Ubiquiti's published API reference ([developer.ui.com](https://developer.ui.com)),
checked against UniFi Network 9.1 and 10.6: every field is treated as
optional, a number is read whether it is written as a number or as text,
and a device state SPARK has not seen before is shown as UniFi writes it —
so a newer UniFi Network that adds to the API does not break it.

1. In UniFi Network, open the **Integrations** page and make an API key.
   It is shown once. SPARK never changes anything with it.
2. In SPARK, Settings → Credentials → **Add a credential**: UniFi, a name,
   the device (the console), and the key. Port 443 is assumed; give
   `address:port` for another.
3. Compare the fingerprint SPARK shows with the one your browser shows for
   the console's own page (the padlock → the certificate → SHA-256), and
   press **Trust this certificate**. **The key has not been sent before
   this.** The certificate is pinned exactly as for TrueNAS and Proxmox.

**What it reads, every 5 minutes:** `/v1/info` (the UniFi Network version);
`/v1/sites`; for each site `/devices` (every adopted device and its state)
and `/clients`; and for each device its own page (firmware, whether an
update is waiting, its uplink, ports and radios) and, while it is online,
`/statistics/latest` (CPU, memory, uptime, uplink rate). Every page of a
list is read. The console's device gets a **UniFi** card: each device with
its state, address, firmware, CPU, memory, clients and uptime, the device
it uplinks through, and a link to its own SPARK page — by MAC, or by
address for a device SPARK knows by IP alone.

**A routed device gains its MAC.** Across a router SPARK sees no MACs, so a
UniFi device on another VLAN is known by IP alone. UniFi knows its MAC, and
each check gives it to that device — on the same terms as a router's ARP
table over SNMP: only when exactly one MAC-less device is at that address,
no device has that MAC yet, and the address is not one a polled device says
is its own. Where another device already has the MAC, that is a merge for
you to make, not SPARK. Clients are only counted — wired, wireless, VPN, guests, and per
device; their names and addresses are not stored.

A call UniFi will not answer leaves that part as it was last read. If the
key stops working, *SPARK cannot use the API* alerts as for the others;
UniFi's own device alerts are not added yet.

---

## Backup and restore

Settings → Backup. A backup is one file holding everything SPARK cannot
rebuild: the database, `secret.key` (without it no stored credential — SNMP,
TrueNAS, Proxmox, the Discord webhook — can be read) and the HTTPS
certificate in `data/tls/`. The database is copied with SQLite's own online
backup, so it is consistent while SPARK keeps running.

**Every night at 04:00** (after the 03:30 clean-up) SPARK makes one in
`data/backups/` and keeps the last seven. They sit beside the files they
copy, readable by SPARK alone, like the originals. A backup on the same disk
does not survive the disk: copy one somewhere else now and then.

**Download** makes one now, or takes a nightly one, and encrypts it with a
passphrase you type twice (at least 12 characters; never stored): scrypt
for the key, AES-256-GCM in 1 MiB chunks, every chunk numbered and the last
one marked, so a file that is altered or cut short does not decrypt rather
than restoring something else. Without the passphrase the file is useless,
and so is a restore. A download is announced in Discord like a password
change, when sign-in alerts are on.

**Restore** is a command, run with SPARK stopped, from the `spark/` folder:

```bash
docker compose stop spark
docker compose run --rm spark spark-restore /data/backups/spark-backup-20261002-040000.tar.gz
docker compose up -d
```

A downloaded `.sparkbackup` goes in `data/` first (`/data/<file>` inside
the container); it asks for the passphrase. It checks the whole file before
touching anything — the passphrase, every chunk, that the archive holds only
SPARK's own files, the database's integrity and version — and moves the data
that was there to `data/pre-restore-<when>/` rather than deleting it. It
refuses while SPARK is running (SPARK holds a lock on its data directory)
and refuses a backup from a newer SPARK: update first. An older backup is
brought up to date by the usual migrations when SPARK starts.
