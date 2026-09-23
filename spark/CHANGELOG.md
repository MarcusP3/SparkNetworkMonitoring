# Changelog

## Unreleased — colour means status (2026-09-23)

Design only; no behaviour change.

### Changed

- **Icon tiles are cyan unless they count a status.** Settings wore the amber
  of *degraded*; Targets, Monitored, Watched and the target form wore the green
  of *up*. None of them is a status, and a colour that sometimes means state and
  sometimes means nothing ends up meaning nothing. Amber and red tiles now appear
  only on the Degraded and Down counts, and on Open incidents, which moves from
  pink to red because unresolved incidents are exactly a down state.
- **Violet and pink are gone.** They were literals, unthemed, and 2.7:1 on
  white — under the 3:1 that icons need. `--tile-2`, `--tile-3` and `--tile-6`
  and their rules are removed.
- The avatar gradient runs cyan to darker cyan instead of cyan to violet.
- A danger button's hover used a hardcoded light-theme red (2.8:1 on the dark
  panels); it uses `--bad`.

## Unreleased — the SPARK mark (2026-09-23)

The ⚡ emoji is gone. It rendered differently on every OS and ignored CSS, so
the header stayed yellow — the degraded colour — after the accent moved to
cyan. Design only; no behaviour change.

### Changed

- **A drawn mark: a flat-top bolt**, inline SVG from a `mark()` macro in
  `templates/_brand.html`, coloured by `--accent`. Used in the header, login and
  setup, and at the top of the README.
- **Favicon set** in `static/brand/`: `favicon.svg` (modern browsers),
  `favicon.ico` at 16/32/48 (everything else) and a 180px
  `apple-touch-icon.png` for a home-screen shortcut. A cyan bolt on a
  near-black tile, which holds up on both light and dark browser tabs.
  Cache-busted with the stylesheet's `asset_version`. The raster files were
  rendered from the SVG in Chromium; regenerate them if the path changes.

## Unreleased — brand colour and type (2026-09-23)

First pass at a visual identity. Design only: no behaviour, config or
dependency change, and no rebuild needed beyond picking up the new files.

### Changed

- **The accent is SPARK's electric cyan, not a stock blue.** `#22d3ee` in dark
  mode; `#0e7490` in light, because the bright cyan is 1.8:1 on white and
  cyan-700 is 5.4:1. Filled accent controls take near-black text in dark mode —
  white on the bright cyan fails at 1.8:1.
- **The brand mark was painted in `--warn`**, the degraded colour. It uses the
  accent now. (The mark was still the ⚡ emoji at this point, which ignores
  CSS colour; the SVG mark replaced it in the next change.)
- **`--info` is neutral grey, not blue.** It was defined and never used, and a
  blue beside the cyan would have read as a fourth status.
- The dark theme's cool corner glow and the first stat tile follow the accent.

### Added

- **Inter for text, JetBrains Mono for data**, self-hosted in
  `static/fonts/` with their OFL-1.1 licences. Variable builds, because the
  stylesheet uses weights 550 and 650. Latin subsets, 88 KB together, from
  Fontsource 5.3.0 (npm `@fontsource-variable/inter`,
  `@fontsource-variable/jetbrains-mono`). SHA-256:
  - `inter-latin-wght.woff2` `3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62`
  - `jetbrains-mono-latin-wght.woff2` `18be452724bfdc236c074ca94a249a7f41a86752c7d04ab258ce9ed5651f6a7e`

## Unreleased — the rest of the pages catch up (2026-09-22)

The shell pass left Targets, Devices and Settings wearing the new palette on
their old layouts. This finishes them. Still design only: no new features, no
new queries, and every string the tests assert on left where it was.

### Added

- **A page masthead**, shared by every page but the dashboard: an icon tile,
  the page name, one line saying what the page is for, and the page's actions.
  The same anatomy as a card header, one size up — a page and a card are the
  same kind of thing at different scales, and giving them two different
  headers is how a UI stops feeling like one piece of software.
- **Control bars read as controls.** The scan-schedule and subnet-filter bars
  on Devices hold settings rather than findings, so they are flatter and
  unshadowed. Four equal slabs down the page was the previous reading.
- **The sweep summary is four figures, not a sentence.** Probed, answered, with
  a MAC, new. They are read against each other — probed against answered is the
  comparison that matters — and a sentence makes you do that in your head. The
  age moved up into the card's subtitle.
- Settings, the target form, Targets and Devices all pick up the icon and
  subtitle treatment; the target form gains a way back to Targets.

### Fixed

- **The small uppercase labels failed contrast, and the last pass missed it.**
  Table headers and the like were checked against a 3.0 threshold, which is the
  bar for large text — these are 11px, so the bar is 4.5. On that measure the
  light theme's faint text was 3.25:1 and the dark theme's 3.81:1. Both have
  been moved until they clear 4.5 on *both* the card and the control-bar
  surface, which is the pair that actually matters now that bars have their own
  background. All fourteen pairs pass at the correct threshold.

### Checked

- Rendered at both themes across Dashboard, Targets, Devices, Settings, the
  target form and Setup.
- Suite: 322 passed, 6 skipped. `smoke_test.py`: 76 passed. No test changed —
  the point of restyling in place.

## Unreleased — a new look for the shell and the dashboard (2026-09-22)

A visual pass, from a mockup. No new features and no new data: everything on
the page was already being queried, and anything in the mockup that would have
needed data the app does not collect — the health chart, the per-card
sparklines, the trend percentages, the uptime column, search and the
notification bell — was left out rather than drawn in.

### The shell

- **A deep navy dark theme** with two fixed radial glows, warm from the
  bottom-left and cool from the top-right. They are tokens, so the light theme
  switches them off rather than inventing a pastel version of the same effect.
- **A sticky, translucent top bar** — useful now that Devices can show 250 rows
  at once — with the current page as a filled pill, and a signed-in block with
  an avatar, name and role.
- Larger radii, softer shadows, a wider page, and a footer that carries the
  real version, read from package metadata rather than typed into a template.
- **Tables** get a header band, uppercase tracked labels and a row hover.

### The dashboard

- A greeting, the date, and six stat cards with tinted icon tiles — each with a
  line saying what its number counts, because "1" under "Monitored" is
  ambiguous in a way that "Actively checked" is not.
- Network configuration and Watched keep their tables; recent incidents becomes
  a timeline, which is the right shape for events at points in time.
- The heading stayed **"Recent incidents"** rather than becoming the mockup's
  "Recent activity". Every entry in it is an outage; "activity" would promise a
  feed of everything the app does.

### Two bugs found on the way

- **Status pills were never themed.** `up`, `unknown`, `ongoing` and the rest
  carried hardcoded light values — `#e6f4ea` on `#1e6b33` — so in dark mode
  every one of them rendered as a pale chip stamped on a near-black page. They
  are on tokens now.
- **Buttons that are links were underlined.** `.btn-primary` and `.btn-quiet`
  are worn by `<a>` as well as `<button>`, and an anchor brings its own
  underline; "Add target" rendered as underlined text in a blue box.

### Checked

- **Contrast, computed rather than eyeballed.** Fourteen text/background pairs
  against WCAG AA. Two failed on the first pass — the light theme's faint text
  on a table header at 2.71:1 and its warn colour at 4.44:1 — and both were
  darkened until they passed. All fourteen now clear their target.
- Rendered at both themes across Dashboard, Targets, Devices, Settings and
  Setup. Three layout faults were caught that way and fixed: an `ongoing` pill
  mangled by a class collision between the timeline bullet and the pill dot, a
  cause and duration running together as one sentence, and the incident marker
  on Targets wrapping onto a second line once the status pills grew.
- Suite: 322 passed, 6 skipped. `smoke_test.py`: 76 passed. No test needed
  changing, which was the point of restyling in place rather than restructuring.

Targets, Devices and Settings inherit the new palette and card styling; their
layouts are unchanged and come next.

## Unreleased — the port list is yours to edit (2026-09-22)

The built-in 45 are a good default and a bad answer to "what is on *my*
network". A homelab runs Deluge on 8112 and Node-RED on 1880, and neither is on
anybody's well-known list; meanwhile four Windows ports are pure cost on a
network with no Windows on it.

### Added

- **Custom ports**, in Settings → Port scanning. Port plus an optional name,
  added a row at a time, each with its own Remove. The name is documentation —
  it is what the Devices page shows instead of the number.
- **Built-ins can be switched off.** This is the half that makes the scan
  *faster*: unticking four Windows ports on a Windows-free network buys back
  four timeouts per device, every scan, forever.
- **The budget is stated, not discovered later.** *"Scanning 43 port(s) — 41 of
  45 built-in, plus 3 of your own. Across 23 devices that is up to about 4s per
  scan, and only if nothing answers."* The device count is in there because the
  cost of a port is not a property of the port.
- A custom entry on a built-in's port **renames** it rather than duplicating
  it. 3000 is Grafana to most people and your own app to you.

### A bug this would have shipped, and one it exposed

- **Switching a port off would have closed every service on it.** `record_scan`
  closes any scan-sourced service it did not find, and it did that without
  asking whether it had looked — so unticking SMB would have marked every SMB
  service on the network closed at the next scan, on the strength of never
  having probed them. A scan now carries the set of ports it covered, and only
  closes within it. An empty set closes nothing.
- The same bug was **already reachable** before this change: the control probe
  removes intercepted ports from the list, so a port found intercepted this
  week silently closed whatever was recorded on it last week.
- **The control probe only checked the built-ins.** 8080 redirected to a
  captive portal is the same trap as 80, but a port you added was never
  eligible to be caught. It now probes the same list the scan uses.

### Corrected

- `ports.py` claimed a 1024-port scan was "seventeen minutes for that host
  alone" and that forty ports cost forty seconds. Both assumed serial probing
  and ignored the two semaphores in the same file — out by roughly tenfold. The
  real model is `max(ceil(ports/12), ceil(ports×hosts/256)) × timeout`, measured
  against black-holed addresses: 67 hosts × 45 ports is 12 seconds, and the same
  hosts at 85 ports is 23. The Settings estimate uses that model, and the tests
  assert it against those measurements.

### Tests

- 64 new in `tests/test_port_catalogue.py`.
- Mutation-checked, six deliberate breakages, three went unnoticed:
  - Nothing tested that the **scan runner reads the catalogue at all**. It
    could have been editable, storable, mergeable and entirely ignored by the
    thing it configures, with the page looking right throughout. Three tests
    now cover it, one end to end against a real listening socket.
  - Nothing caught the control probe ignoring custom ports. Now covered by an
    interception simulated with a listener on `0.0.0.0` and loopback controls.
  - An `isinstance(value, bool)` guard in `parse_port` turned out to be dead —
    parsing via `str()` had already handled it. Removed; the test that proves
    `True` is not port 1 stays.
- Suite: 322 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — the Devices page fits on a screen (2026-09-22)

A real network puts a few hundred rows on that page. It was one long table, so
the sweep summary, the subnet filter and "Mark all reviewed" scrolled off the
top and stayed there.

### Added

- **A page-size dropdown and a prev/next pager** on the Devices page. 50 a page
  by default; 25, 100, 250 and All are offered. Both live in the URL, so a
  choice survives a reload, a bookmark and the live refresh — which already
  refetches `location.search` for the subnet filter's sake.
- The count line now says which slice you are looking at: *Showing 26–50 of 63
  on this subnet (67 device(s) in all).*

### The parts that were easy to get wrong

Paging is a scrolling aid, and every risk in it is the same mistake in a
different place: the page number quietly changing what something *means*.

- **A device falling between two pages.** The ordering is "last seen first",
  and a sweep records everything it found in one batch — so a great many
  devices share a `last_seen` exactly, and the order of rows tied on the only
  sort column is undefined. The query now ends in `Device.id`. A device nobody
  sees is a device nobody reviews, which is what this page is for.
- **Counts collapsing into "how many are on screen".** "Mark all 12 reviewed"
  acts on every unreviewed device, so it is counted before the slice is taken.
- **A stale page number.** Clamped to the last page, not honoured blindly —
  rows come and go between refreshes, and an empty table reads as a network
  that emptied out. There is deliberately no hidden `page` field in the filter
  form, which is how changing the subnet or the page size returns to page 1
  without any JavaScript.
- **`?per_page=100000`.** Validated against the offered list, like the sweep
  interval. The value travels in a URL people edit and paste at each other.
- The size `<select>` is never `disabled` — that mistake has been made twice on
  this page already, and a disabled select submits nothing.

### Also

- Services are now fetched for the devices being shown rather than for every
  device that exists. Still one query.

### Tests

- 29 new in `tests/test_paging.py`, including a round trip asserting every
  device appears exactly once across the pages.
- Mutation-checked rather than assumed. Five deliberate breakages were
  introduced to see which tests noticed; two did not, and both were real:
  - The `subnet_filter.shown` count turned out to be **dead** — nothing read
    it. Removed, rather than left as a second copy of a number that would
    eventually drift into meaning "how many are on screen".
  - The ordering tiebreaker could be deleted with every test still passing,
    because SQLite happens to return tied rows in rowid order. A test that
    cannot fail is not a guard, so that one now asserts on the `ORDER BY`
    itself and says in its docstring why it has to.
- Verified in a browser at both themes: page boundaries, clamping, the live
  refresh keeping page 2, and both dropdowns returning to page 1.
- Suite: 258 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — the scan checks whether the network is lying to it (2026-09-22)

Prompted by every device appearing to run DNS. That turned out to be a
misreading, but the question it raised is real: a TCP connect scan cannot tell
a service apart from a firewall answering on its behalf, because the handshake
genuinely completes either way. From the scanner's seat, a rule redirecting
port 53 to the local resolver makes every address on the network run DNS.

### Added

- **A control probe.** Before each scan, SPARK probes up to three addresses in
  your subnets that discovery has never found anything at. A port that answers
  on a host that is not there is the network talking, not a service, so it is
  excluded from the scan rather than recorded — listing it would put a row in
  the inventory for something that does not exist.

- **The Devices page says so** when it happens, naming the ports and how many
  dead addresses they answered on, with the usual cause: a firewall redirecting
  a port to itself. A silent omission would be its own kind of lie.

### The two rules that make it safe

- **Unanimity.** A port has to answer on *every* control before it is called
  intercepted. Anything less and one live machine that slipped into the control
  set would suppress the ports it runs across the whole network — hiding real
  services, which is a worse failure than showing false ones.

- **At least two controls, or it declines to judge.** A single "free" address
  might be a host that appeared since the last sweep. With one sample there is
  no way to tell, so it does not guess.

Controls are also spread across the subnet rather than taken from one end: the
bottom is where infrastructure lives and the top is often where a DHCP pool
ends, so a cluster at either end is likelier to hit something real.

### Tests

- Eight more in `tests/test_ports.py`, simulating interception with loopback
  aliases — which is a faithful model rather than a mock. A listener bound to
  `0.0.0.0` answers on `127.0.0.2` and `127.0.0.3` alike, exactly as a firewall
  redirect looks from the scanner; one bound to `127.0.0.1` answers only there,
  exactly as a real service does. Both cases are asserted, along with one
  control declining to judge and no controls not being an error.
- Verified end to end: a real interceptor bound to `0.0.0.0:53` alongside real
  services on `127.0.0.1:22` and `:443`. Port 53 was detected against three
  controls, excluded, and the banner shown; only ssh and https were recorded.
- One test asserted nonsense and was rewritten — it compared a three-element
  list to its own sorted prefix, which is the same list and could never fail.
  It now asserts the property it meant to: that the controls span the range.
- Suite: 229 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — increment 4b: service discovery (2026-09-22)

The inventory could say what was on the network. It can now say what those
things are running, and turn any of it into a monitored target in one click.

### Added

- **A TCP connect port scan** of devices discovery has already found. Connect,
  not SYN: SPARK is a container, `connect()` needs no privilege it does not
  already have, and the difference only matters to someone trying not to be
  logged. SPARK is not trying not to be logged.

- **A curated catalogue of 45 service ports** — SSH, HTTP/S, SMB, RDP, the
  common databases, and the homelab set: Proxmox, Plex, Jellyfin, Home
  Assistant, Portainer, Grafana. Named, so the Devices page shows "postgres"
  rather than 5432.

- **A Services column** on the Devices page. Each service is a button: pressing
  it creates a **TCP** target on that exact port. A ping target tells you the
  host is up; a host that pings while its database is dead is the outage you
  wanted to catch.

- **Ports worth a second look are flagged** — telnet and FTP for sending
  credentials in clear text, an open Docker socket because it is root on that
  host, Redis and Elasticsearch for defaulting to no authentication. Not a
  judgement on running them, but an inventory that notices is more use than one
  that lists everything identically.

- **A Port scanning section in Settings** — on/off and an interval in hours,
  plus a plain statement of what the scan does to your network, since it is
  more intrusive than an ICMP sweep and you should be able to read that before
  pointing it at a segment.

- `discovery/ports.py`, `discovery/services.py`, and `tests/test_ports.py`.

### Two limits that shape the whole thing

Both are the same point: a scan that is slow is a scan that gets switched off.

- **Only discovered devices are scanned, never whole subnets.** The sweep
  decides who exists; this decides what they are running.

- **A curated list, not a port range.** An open port refuses instantly and a
  closed one refuses instantly, but a *filtered* port — a firewall dropping
  rather than rejecting — costs the full timeout every time. At 1024 ports one
  firewalled host is seventeen minutes on its own. Forty-five bounds it to
  forty-five seconds.

Also: hours, not minutes. What a machine listens on changes when you deploy
something. And devices unseen for a fortnight are skipped rather than paying a
timeout per port.

### Notes

- A service that stops answering is marked closed, not deleted. "This host used
  to run Postgres" is exactly what an inventory should be able to tell you, and
  a DELETE cannot.
- A scan never overwrites a name from a better source, and never closes a
  service it did not discover. That matters for the Docker inventory arriving
  next: a container is not absent just because its port was shut to us.
- No schema change. `Service` has been in the schema since increment 1.

### Deferred

Docker inventory is its own increment. When it lands it will read container
lists through a **read-only socket proxy** rather than SSH or the TLS API —
SPARK never touches the socket, so it cannot start, stop or create containers
even if compromised.

### Tests

- `tests/test_ports.py`, 26 tests. The scanner is tested against **real
  sockets** — a listener on a free port, a port deliberately closed, an
  unroutable address, garbage input. Mocking `open_connection` would only prove
  the mock was called; the question is whether a TCP connect distinguishes a
  service from the absence of one, and only a socket answers that.
- The store is tested for what does not raise: a rescan duplicating rows, a
  closed service vanishing, a scan overwriting a better name.
- Verified end to end in a browser against six real listeners on 22, 23, 80,
  443, 6379 and 8006 — all six found and named, telnet and redis flagged, and
  clicking "https" produced a TCP target on `:443` that came up immediately.
- Suite: 221 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — one open incident per target (2026-09-18)

Reported as three ongoing incidents for one target, with the right diagnosis:
pausing and resuming while it was down. A target can only be down once at a
time, so three simultaneous open incidents is corruption, not a display quirk.

### Fixed

Three faults in one path, none of which raised — an incident with no
`closed_at` is a perfectly valid row, so the only symptom was a dashboard
reporting outages that had ended hours earlier.

- **Pausing a target left its incident open.** Nobody is checking a paused
  target, so its incident has no knowable end; left open it reported an outage
  growing for the length of the pause. Pausing now closes it at the moment
  monitoring stopped, which is the only honest answer available.

- **Resuming made the next failure look like a new outage.** Resume sets the
  status to UNKNOWN, so the transition into DOWN read as fresh and opened
  another incident on top of the one still open. Going down now continues an
  incident that is already open instead of opening a second.

- **Recovery could never close them.** Recovery requires the previous status to
  be DOWN, and after a resume it was UNKNOWN — so a target that came back left
  its incident open forever. Recovery now also closes *every* open incident for
  the target rather than only the newest, which is what made older duplicates
  immortal.

### Added

- **`incident.resolution`** — why it closed: recovered, paused, or superseded.
  A duration cannot distinguish "it came back" from "we stopped watching", and
  those two read identically on the dashboard. A paused incident now says
  "paused, not recovered" under its duration.

### Schema

- **Migration 4** adds the column; **migration 5** repairs databases that
  already have overlapping incidents, closing each superseded one at the moment
  the next opened — the last instant it can honestly be said to have still been
  running. The newest is left open: if the target is still down, it is.

- Migration 4 checks whether the column exists before adding it. `ALTER TABLE`
  is not idempotent, and a database created by `create_all` at a version this
  migration then runs against already has it — which took startup down with
  "duplicate column name" until the check was added. Any migration adding a
  column needs this.

### Notes

- After the repair you will still see several incident rows. That is correct:
  each pause genuinely ended an observation and each resume began a new one, so
  separate outages is what was actually seen. Merging them would claim
  knowledge of the gaps. What was broken is that all of them were open at once.

### Tests

- `tests/test_incidents.py`, 9 tests: the reported bug reproduced as three
  pause/resume cycles, pausing closing the incident, going down again
  continuing rather than duplicating, recovery closing all of them, the
  ordinary outage path still working, the migration against a database
  carrying the real corruption, and a comparison of the migrated schema against
  `create_all` to catch hand-written DDL drifting from the model.
- Two migration tests no longer pin the project's version number to a literal,
  which made every new migration break an unrelated test.
- Suite: 195 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — a Targets table that holds still (2026-09-18)

### Fixed

- **Pausing a target no longer greys out its own buttons.** `tr.is-paused td`
  dimmed the whole row, including the actions — so Resume, the control you came
  to a paused row to press, was the hardest thing in it to see. The dimming now
  applies only to the middle cells, which are the ones actually stale. The
  status pill is exempt too: it is not stale, it is the thing explaining why
  everything else is.

- **The table stops shifting sideways when a target changes state.** Two causes,
  and the reported diagnosis — the status text changing length — was the first
  of them:

  - Status pills sized themselves to their word, so "up" and "unknown" and
    "degraded" each made the column a different width. All status pills are now
    one width and centred, and the column is reserved outright so the incident
    marker appearing cannot move it either.
  - The second was **"Pause" becoming "Resume"**. The table is `width: 100%`,
    so a wider actions column is paid for by shaving pixels off every column to
    its left. Measured at 8px of drift accumulating across the row, after the
    pill fix had already removed the rest. The button now has a floor.

  Verified by measuring every column header's x-position before and after a
  pause: 8px of drift, then 0.

### Tests

- Two in `tests/test_targets_page.py` for the markup hooks the CSS hangs off —
  a class silently disappearing would stop the rules applying with nothing else
  noticing.
- Suite: 186 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — hash-pinned dependencies (2026-09-18)

Prompted by the right question: what in here could get compromised. The answer
was not the package list — 15 direct dependencies, 38 in the closure, all
mainstream. It was that nothing was pinned.

Every constraint was `>=`, there was no lockfile, and the Dockerfile ran a bare
`pip install .`. So every `docker compose build` resolved fresh from PyPI: you
never built the same image twice, had no record of what shipped, and a single
hijacked maintainer account was enough to put code on a host running with
`NET_RAW` on a home network. That is the `event-stream` shape.

### Added

- **`requirements.lock` and `requirements-build.lock`** — every package, direct
  and transitive, pinned to a version and its SHA-256 hashes. The image installs
  from them with `--require-hashes` and never re-resolves.

- **The build toolchain is locked separately and installed with
  `--no-build-isolation`.** Without that, `pip install .` fetches hatchling
  unverified at build time, which is a hole straight through the runtime lock.
  Easy to miss; it defeats the whole exercise.

- **`tests/test_supply_chain.py`**, 9 tests: every declared dependency is
  locked, nothing is unpinned, every entry carries a well-formed hash, the
  build toolchain is covered, and the Dockerfile still uses `--require-hashes`
  and `--no-deps`.

### Changed

- **Dependencies are installed before the source is copied**, so the dependency
  layer is cached independently of application changes. A source-only edit no
  longer reinstalls 38 packages.

- **The base image is pinned by digest rather than the `python:3.12-slim` tag**,
  which is rebuilt regularly and points at different bytes over time.

### Removed

- **`itsdangerous`.** Declared since increment 1, never imported, shipped in
  every image. Sessions are database-backed tokens rather than signed cookies,
  so nothing ever used it. A dependency that does nothing is pure attack
  surface. `config.secret_key()` stays: it is still touched at startup as a
  fail-fast check that the data directory is writable.

### Verified

- The full install path — build lock, runtime lock, then the app with
  `--no-deps --no-build-isolation` — run end to end in a clean 3.12 environment.
- A hash was then corrupted and `pip` refused: exit 1, nothing installed.
  Worth recording how that test failed first: changing *one* hash of a package
  changed nothing, because pip accepts an artifact matching **any** listed hash
  and each package lists several (an sdist and per-platform wheels). The real
  check needs every hash for a package corrupted. A tamper test that passes for
  the wrong reason is worse than none.

### Still open

- No CI, so nothing runs `pip-audit` on a schedule. `pip-audit -r
  requirements.lock` checks for known advisories by hand.
- The Discord webhook URL is still stored in the database in plaintext.

## Unreleased — the dashboard was still reading spark.yaml (2026-09-18)

### Fixed

- **The dashboard's subnet table ignored anything added in Settings.** Devices,
  the sweep and the scheduler were all moved onto the database in the last
  change and the dashboard was left reading `config.network.subnets`, so it
  went on showing the file's idea of the network while Settings edited the real
  one. Nothing raised — the two pages simply disagreed, which is the failure
  mode that takes longest to notice. It now reads the same source as everything
  else, and its "no subnets" warning points at Settings rather than at a file
  that is no longer consulted.

- Stale empty states on the Dashboard and Targets pages still said "there is no
  discovery yet, so targets are added by hand". Discovery shipped in increment
  4; both now point at the Watch button on the Devices page.

### Added

- The dashboard flags a subnet that is listed but has **Sweep** unticked. That
  state is invisible otherwise and looks exactly like a working subnet that
  never finds anything.

### Tests

- Five more in `tests/test_subnets.py`: a subnet added in Settings reaches the
  dashboard, a removed one leaves it, an edited VLAN shows there, the empty
  warning no longer mentions `spark.yaml`, and an unswept subnet is called out.
  The first two would have caught this.
- Suite: 175 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — two bits of visual noise (2026-09-18)

### Changed

- **The failure counter is gone from targets that are already down.** "2613
  consecutive failure(s)" sat where the last-checked time goes and said nothing
  the status pill had not already said; the number only climbs. It is still
  shown *below* the threshold, as "failing, 2 of 4" — that is the one place a
  target wobbling toward an incident is visible before it flips, and it now
  reads as progress toward a state change rather than as a running tally. A
  paused target shows nothing either: its counter is frozen at whatever it was
  when you paused it, so reporting it would describe a moment in the past as
  though it were now.

### Fixed

- **The "new ✕" badge wrapped onto two lines** on the Devices page, which made
  one control look like two. Caused by the VLAN column squeezing the Name
  column; pills now refuse to wrap, the name field gives up the space instead
  of the badge, and its minimum width came down to suit the narrower column.

### Tests

- `tests/test_targets_page.py`, 5 tests: the count is absent when down and when
  paused, present below the threshold, absent when healthy, and removing it did
  not take the status pill or the last-checked time with it.
- Suite: 170 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — say what the subnet checkboxes actually do (2026-09-18)

Both checkboxes on the Settings page were labelled in shorthand — "L2" and
"on" — under headers that already said the same thing, with the real
explanation in a tooltip nobody hovers. The first question the page got was
what the attached checkbox meant.

### Changed

- The cryptic inline labels are gone; the column headers do that job, and the
  checkboxes carry `aria-label`s instead of visible abbreviations.

- The paragraph under the table is now a three-term legend covering
  **Attached**, **Sweep** and **VLAN**, each with the consequence rather than
  the definition.

- **Attached** now says how to check — `ip -br addr` on the host — and which
  way to err. The two mistakes are not symmetrical and the page never said so:
  ticking it wrongly is harmless, because the ARP table holds no entries for
  addresses beyond the router, so the lookups come back empty, identity falls
  back to IP anyway, and the Devices page shows a "no ARP" pill saying the flag
  disagrees with reality. Unticking it wrongly skips an ARP read that would
  have worked and throws away MAC identity. So: when in doubt, tick it.

- **Sweep** is documented for the first time. It was an unlabelled checkbox
  that silently controlled whether a subnet is scanned at all.

- **VLAN** says plainly that nothing reads it — not the sweep, not identity,
  not the scheduler — so a network that does not want VLAN IDs to matter can
  still record them.

## Unreleased — subnets move into the database, with a Settings page (2026-09-18)

Adding a subnet used to be an SSH session, a file edit and a container restart.
DESIGN.md's premise is that SPARK can be handed to someone else and configured
through the browser; subnets were the largest thing still contradicting that.

### Added

- **A Settings page**, replacing the greyed-out nav link. Subnets are added,
  edited and removed there: CIDR, name, VLAN tag, whether the segment is
  directly attached, and whether to sweep it. Changes apply to the running
  scheduler, so adding the first subnet starts the sweep and removing the last
  one stops it without a restart.

- **Editable VLAN tags.** Per subnet, not per device — a device is on a VLAN
  because of the segment it sits in, so there is one place to correct a
  mistake. Nothing reads the tag; it is documentation, and the Devices page
  shows it in its own column.

- **A subnet filter on the Devices page**, as a GET form, so the choice lands
  in the URL and survives a reload, a bookmark and the live refresh. It offers
  "not on a configured subnet", which is how you notice a segment you forgot to
  configure. The count reads "showing 1 of 3" rather than just the filtered
  number.

- `Subnet` model, `subnets.py`, `web/routes_settings.py`, `templates/settings.html`.

### Changed

- **`network.subnets` in `spark.yaml` is now seed-only.** Its entries are
  copied into the database on the first start after upgrading and the section
  is never read again. Editing it on an existing install does nothing — said
  plainly in the file itself, because a setting you can change in two places
  disagrees with itself eventually.

- **Subnet membership is computed from the address, not from the label stored
  at discovery time.** Renaming a subnet no longer orphans the devices found on
  it, and adding a subnet retroactively classifies devices discovered before it
  existed. The most specific match wins, so documenting a `/8` does not swallow
  the `/24`s inside it.

- The sweep, the scheduler and the Devices page all read subnets from the
  database. `schedule_discovery()` takes a `subnet_count` for the same reason
  it already took `settings`: so a request that may hold the write lock is not
  waiting on a second session to read.

### Fixed

- **The live refresh dropped the query string.** It refetched
  `location.pathname` alone, which would have reset the subnet filter every
  time a sweep finished. Introduced by this change; caught before it shipped.

### Schema

- **Migration 3** creates the `subnet` table; `CURRENT_VERSION` moves to 3.
  Built from the model's metadata rather than hand-written DDL, so it cannot
  drift from what `create_all` gives a fresh install. The migration creates the
  table only — copying `spark.yaml` in needs the config object, which
  migrations deliberately do not get, so the seed runs at startup.

- The seed is guarded by a flag, not by "is the table empty". Those differ in
  the case that matters: upgrade, delete the subnets you no longer use,
  restart, and find them back. There is a test for exactly that.

### Notes

- Deleting a subnet keeps the devices found on it. A device is evidence that
  something was on the network; deleting the segment you were looking through
  is not a statement about what you saw. They stop matching the filter, which
  is the honest outcome.

- A subnet too large to sweep (bigger than a `/22`) is flagged on the Settings
  page rather than refused. A `/16` is a reasonable thing to document and an
  unreasonable thing to scan, and silently accepting it would leave a subnet
  that looks configured and never runs.

- CIDRs are canonicalised on the way in, so typing the address of the box you
  are standing on — `10.1.10.7/24` — is accepted and stored as `10.1.10.0/24`.

### Tests

- `tests/test_subnets.py`, 46 tests: CIDR and VLAN validation, membership
  including the unparseable and most-specific cases, CRUD, seeding (including
  the deleted-subnet-comes-back regression), the migration run against a
  database wound back to version 2, a full upgrade end to end, and the filter
  against a stale id, junk input and no subnets at all.
- Suite: 165 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — the Save button appears only when there is something to save (2026-09-18)

The Save button beside each device name was always visible, on every row, and
did nothing on almost all of them. The first question it ever got asked was
"what does save do on the page?", which is the answer.

### Changed

- **Save is hidden until a name differs from what is stored.** Typing something
  and undoing it hides it again — an edit is a difference, not a keystroke.

- It is hidden by CSS hanging off a class JavaScript adds to `<html>`, not
  rendered conditionally on the server. With JavaScript off the button is
  simply always there and the form still works, which is the right way for this
  to degrade. `visibility`, not `display`, so the Name column keeps its width
  and the table does not shuffle sideways as you type.

- The handler is delegated from `document` and compares against a
  `data-original` attribute from the server, so rows replaced by a live refresh
  are already covered rather than needing rebinding.

### Fixed

- **A live refresh no longer discards what you are typing.** Refreshing `#live`
  replaces every element inside it, including the name field under the cursor,
  so a sweep finishing mid-word threw the word away. The refresh now waits
  while a field in that region is focused or holds unsaved changes, and runs
  when you are done. Pre-existing, but the hidden Save button makes it visible:
  your text and the button would vanish together.

### Tests

- Four more in `tests/test_schedule.py`, against a seeded device: the button is
  in the HTML rather than conditionally rendered, the comparison value is the
  stored name, an unnamed device compares against empty rather than against its
  hostname placeholder, and saving still works.
- Suite: 119 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — automatic scanning, on the page and on the clock (2026-09-18)

Reported as "the devices tab does not run automatically". It was scheduled, and
had been since increment 4 — but an APScheduler interval trigger's first fire is
one whole interval away, so a fresh container sat for 15 minutes doing nothing
and every `--force-recreate` restarted that clock. Measured on the real trigger:
the first automatic sweep was due 926 seconds after startup. Nothing on the page
said so, and the only evidence either way was a log line at INFO.

### Added

- **Automatic scanning controls on the Devices page.** A checkbox to turn the
  periodic sweep on or off and a dropdown of intervals — 5, 10, 15, 30 minutes,
  1, 2, 6, 12, 24 hours. Applied to the running scheduler, not just written to
  the database: no restart, no editing `spark.yaml`.

- **A next-scan line that counts down.** "Is this actually scheduled?" is now
  answerable by looking at the page. It is read from the scheduler rather than
  from the settings, so the one case where those disagree — the box ticked but
  no subnets configured — reads as "nothing to scan" instead of a countdown to
  a sweep that will never happen.

- `scheduler.discovery_next_run()`, and a `first_run_delay` on
  `schedule_discovery()`.

### Fixed

- **The first sweep after startup now runs in 15 seconds instead of 15
  minutes.** This is the whole of the reported bug.

- `schedule_discovery()` accepts settings from the caller. It used to open a
  second session to re-read them, and calling it from inside a request that
  still held the write lock is the exact shape of the "database is locked" hang
  that the Devices page had in increment 4.

- The empty-state text no longer claims the first sweep is 15 minutes away.

### Notes

- The interval is validated against the offered list rather than clamped to a
  range. A value that is not one of the choices did not come from the page, and
  the safe reading of that is to keep the existing setting.

- The dropdown is greyed with CSS, not the `disabled` attribute, when automatic
  scanning is off. A disabled `<select>` submits nothing, so the obvious
  implementation silently resets the stored interval every time the box is
  unticked. There is a test for this. Note that `fieldset.tuning` on the target
  form makes the opposite choice deliberately — there, dropping the values *is*
  the intent, so the server applies its own defaults.

- The controls sit outside `#live`, which a live refresh replaces wholesale; a
  dropdown you had changed but not applied would otherwise be discarded
  mid-edit.

### Tests

- `tests/test_schedule.py`, 18 tests: when the first sweep is due, enabling and
  disabling, ticked-but-no-subnets, the form round trip, junk input, and that
  rescheduling replaces the job rather than stacking five sweeps of the same
  network onto the same timer.
- Suite: 115 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — retention: the database stops growing forever (2026-09-18)

The retention settings have existed since increment 1 and nothing read them.
Measured: a check result costs 147 bytes on disk, so ten targets at a 15-second
interval is 8.4 MB/day — about 250 MB a month and 3 GB a year, unbounded.

### Added

- **`check_rollup` table and a nightly downsample.** Raw results are kept for 7
  days, then folded into 5-minute buckets for 90 days, then hourly for 2 years
  — the schedule `DEFAULT_SETTINGS` has specified all along. Measured on 30
  days of synthetic history: 132,484 raw rows became 6,625 buckets, a 20x
  reduction, with a deliberate 20-minute outage still visible in the counts.

- Buckets keep per-status counts rather than one availability figure. "95% up"
  and "up all month except a 90-minute outage" are different months and an
  average cannot tell you which you had.

- Expired sessions and old login attempts are now cleared nightly. They were
  only ever purged at startup, so a long-running instance never cleared them —
  a gap noted in the increment 2 review and left open until now.

### Notes on disk wear

- **It never VACUUMs.** Deleting rows in SQLite frees pages for reuse rather
  than shrinking the file, so a pruned database plateaus and new inserts refill
  the same pages. Verified: 47.8 MB before and after 50,000 further inserts
  plus a prune. A nightly VACUUM would rewrite the whole file — the write
  amplification worth avoiding on an SSD.
- The file will not shrink below its high-water mark on its own. If you want
  space back after the first prune of an already-large database, a one-off
  manual `VACUUM` does it. Once, by hand, not on a schedule.
- The job is a handful of set-based statements in one transaction, once a
  night, not a row-at-a-time loop.

### Schema

- **This is the project's first real migration.** `CURRENT_VERSION` moves to 2
  and migration 2 creates `check_rollup` from the model's own metadata rather
  than hand-written DDL, so it cannot drift from what `create_all` gives a
  fresh install. Verified against a database built to look like a v1 install:
  migrates cleanly, and a second start is a no-op.

## Unreleased — increment 4: device discovery (2026-09-18)

The `device` table has existed since increment 1 and been empty ever since.
It now fills itself.

### Added

- **Subnet sweep.** ICMP across every configured subnet, then a read of the
  kernel's ARP table, then reverse DNS on whatever answered. Runs every 15
  minutes by default, plus a **Scan now** button. The order matters: the pings
  are what populate ARP, which is where MAC addresses come from.

- **MAC-based device identity.** A device is its MAC if we know it and its IP
  only if we don't, so a DHCP lease change keeps one device's history intact
  instead of orphaning it. On routed subnets ARP cannot reach across the
  router, so those fall back to IP identity — which is what the `attached`
  flag in `spark.yaml` has always been for. A row discovered without a MAC is
  adopted rather than duplicated once a MAC becomes visible.

- **Vendor from the MAC prefix**, via a curated OUI table rather than the full
  IEEE registry — the same reasoning as shipping numeric OIDs instead of a MIB
  compiler. Unknown prefixes report nothing rather than guessing. Locally
  administered addresses are flagged, since a phone randomising its MAC per
  network will never return under the same one.

- **Devices page** with inline renaming, ignore, and a **Watch** button that
  turns a discovered device into a ping target in one click. That button is
  the point: an inventory you cannot act on is trivia.

### Fixed

- **"Scan now" deadlocked against its own request.** Resolving the session
  cookie updated the session row's last-seen time, so every authenticated
  request held SQLite's single writer slot for its whole duration. Sweeping
  inside that request opened a second session, tried to write, blocked on its
  caller, and failed after the busy timeout with "database is locked". The
  button now queues the sweep on the scheduler and returns immediately; the
  sweep publishes an event when it finishes and the page updates itself.

- **The session row is no longer written on every page view.** `last_seen_at`
  is rewritten only when it is more than a minute stale. A minute of resolution
  is ample for an idle-session timestamp, and it removes a write — and a held
  lock — from every authenticated request.

- **Table rows aligned.** `.table td` had no `vertical-align`, so cells lined
  up on their first text baseline — a row whose name cell was two lines tall
  left every other cell stranded at the top. Worse, `.actions` set
  `display: flex` on the `<td>` itself, which stops it generating a table-cell
  box at all, so it never stretched to the row height and centring within it
  did nothing. Cells now centre, the action buttons lay out inline, and the
  "new" badge sits beside the name field instead of under it so rows are a
  uniform height.

- **Save no longer doubles as "dismiss the new badge".** One button was doing
  two unrelated jobs and neither was labelled: pressing Save on an untouched
  row cleared the badge while storing nothing. The badge is this page's
  security signal — an unfamiliar MAC appearing overnight is the thing worth
  noticing — so a no-op button must not quietly clear it. Save now only names
  a device; the badge is its own dismiss control. Naming a device still
  acknowledges it, because labelling something is review.
- **Mark all N reviewed**, shown only while something is unreviewed. The first
  sweep of a real network produces a screenful of badges at once, and
  dismissing them one at a time teaches you to ignore the badge — the opposite
  of what it is for.

### Added (diagnostics)

- **The Devices page says why it is empty.** The sweep now records what it did
  — probed, answered, how many yielded a MAC, per subnet — and the page shows
  it. Three causes that produce an identical empty list are now told apart:
  ICMP could not open a socket (a container problem, and it says so), 254
  addresses probed with no replies (a network or config problem), and no sweep
  has run yet. Before, all three read "Nothing discovered yet" and the answer
  was only in `docker logs`.
- A subnet marked `attached` whose sweep returns replies but no MACs is
  flagged, since that silently downgrades those devices to IP identity.
- The old empty state guessed at `NET_RAW` whenever the list was empty. It now
  says that only when ICMP actually failed.

### Notes

- Discovery deliberately does not set `Device.status`. "Answered an ICMP sweep
  forty seconds ago" is not the same claim as "is up", and the check engine
  owns that column for anything actually being watched. Discovery's freshness
  signal is `last_seen`, rendered as an age.
- Subnets larger than /22 are skipped with a warning rather than silently
  probing 65k addresses.
- Still no services: no port scan and no Docker inventory. Those were
  deliberately left out of this increment so the identity rules could land on
  their own.

## Unreleased — increment 3: the check engine (2026-09-17)

SPARK can now tell you something is down. No alerting yet — that is increment 4
— so this is still a page you have to look at, but the state underneath it is
real.

### Added

- **Four check types** in `checks/`: ICMP ping (latency and packet loss over
  several packets), TCP connect, HTTP(S) (status code, optional body match, and
  a TLS expiry countdown), and DNS resolution. They are pure functions over a
  `CheckSpec` with no database access, which is what makes the state machine
  testable. None of them raise: a poller that throws when the thing it polls is
  broken has failed at its only job.

- **State machine with hysteresis** in `engine/state.py`. A target goes DOWN
  only after `failure_threshold` consecutive failures and recovers only after
  `recovery_threshold` consecutive successes. DEGRADED is deliberately
  asymmetric — soft, immediate in both directions, and never opens an incident,
  because an early warning that is delayed is not an early warning.

- **Incidents as rows**, opened on the transition into DOWN and closed on the
  way out, with `suppressed_by_dependency` set when the target's parent was
  already down. The incident is still recorded — you want the history — it is
  just flagged so the notifier can stay quiet about the thirty hosts behind a
  dead switch.

- **Scheduler** (`scheduler.py`): one in-process APScheduler, one job per
  enabled target, reconciled against the database rather than built once at
  startup, so adding a target in the UI starts polling it immediately. Jobs
  carry jitter, `coalesce`, and `max_instances=1`.

- **Target management UI** at `/targets` — add, edit, pause, delete, and
  "Check now". Load-bearing rather than a convenience: with no discovery yet,
  this is the only way targets exist at all.

- Dashboard now shows live status per target, latest latency and detail, and
  the ten most recent incidents.

### Changed

- **New-target defaults are now 15s interval, 3s timeout, 4 failures before
  DOWN, 4 successes before UP** (previously 60s / 5s / 3 / 2), and the four
  tuning fields are hidden behind a checkbox and greyed out until ticked. A new
  target needs only a name, a check type and an address.

  The defaults are defined once as `DEFAULT_*` in `models.py` and read by the
  column defaults, the form defaults and the page text. That is not tidiness: a
  disabled input is not submitted, so an unticked box means the *form's*
  fallback is what the user gets, and a drift between the three would be
  invisible until someone wondered why a target polls on a schedule nobody
  chose.

  Editing a target with non-default values opens the section already ticked,
  because saving it shut would submit no tuning fields and reset the target.
  There is a smoke test for exactly that.

### Added

- **Pages update themselves.** The Targets page and the dashboard now refresh
  the moment a target changes state, instead of showing whatever was true when
  you last hit reload. A row whose status actually moved is briefly
  highlighted, so a change that happens while you are looking elsewhere is not
  silently absorbed.

  Server-sent events rather than polling: one connection per open tab carrying
  nothing while the network is quiet, versus a request every few seconds per
  tab forever that still shows a change up to one interval late. `EventSource`
  reconnects on its own, so a container restart recovers with no retry logic.

  Events fire on real transitions only — not on every check. An event per check
  would mean a page refetch per check per open tab, which is how a monitoring
  tool starts loading the server it monitors. There are tests for the silence
  as well as the signal.

  The page re-fetches itself and swaps in the `#live` element rather than
  rendering fragments from a second set of templates, so the two cannot drift
  apart, and swapping rather than reloading keeps scroll position.

### Fixed

- **Dropdowns were unreadable in dark mode.** The stylesheet already themed
  `select` from CSS variables, but a later block re-declared it with a
  hardcoded white background and `color: inherit`, so in dark mode the control
  and its popup rendered light-on-light. That block now styles only `textarea`,
  which was the element genuinely missing, and `select option` is set
  explicitly for browsers that colour the popup from the control.
- **The tuning checkbox rendered centred.** `.form label` is
  `flex-direction: column`, so the `align-items: center` meant to centre the
  box against its label centred the whole row horizontally instead. It is now
  an explicit row, left-aligned.
- Removed the sentence restating the defaults next to the checkbox. The greyed
  fields already show those values, so it said the same thing twice.

- **The stylesheet is now cache-busted.** `/static/app.css` was a stable URL,
  so browsers kept serving the copy they already had. Templates re-render on
  every request and so update the instant a new image starts, but the CSS did
  not — which presents as a deploy that looks half-applied and costs a hard
  refresh to diagnose, every time. `base.html` now requests
  `app.css?v=<hash>`, where the hash is of the file's own contents, so the URL
  changes exactly when the file does and never when it doesn't.

### Dependencies

- `icmplib` and `dnspython`, both previously named in DESIGN.md's stack table
  but never declared.

### Notes

- No schema migration was needed — `models.py` defined the full Phase 1 schema
  up front, so `target`, `check_result` and `incident` were already there. The
  migration runner therefore still has not executed a real step.
- `muted_until` is stored but not yet honoured; it belongs with the notifier.

## Unreleased — review fix pass (2026-09-13)

A code review of increment 2, and the fixes for what it found. Verified with
`pytest` (33 passed against a live net-snmp agent, 27 passed / 6 skipped
without one) and `smoke_test.py` (29 passed).

### Security

- **Login no longer leaks which usernames exist.** `authenticate()` only reached
  Argon2 when the user row existed, so a wrong password on a real account took
  ~122 ms and an unknown username ~4 ms — a 28x difference that enumerates
  accounts regardless of the error message being identical. Failed lookups now
  verify against a dummy hash. Measured after: 1.00x.

- **Open redirect on `?next=` closed.** The guard was `startswith("/") and not
  startswith("//")`, which `/\evil.com` passes — browsers normalise the
  backslash and navigate to `//evil.com`. Replaced with `_safe_next()`, which
  parses the target and rejects any scheme or host, and also rejects a path
  starting with `//` (an empty authority such as `////evil.com` parses to an
  empty `netloc` while leaving a protocol-relative path behind). Seven hostile
  inputs are covered in `smoke_test.py`.

### Correctness

- **Timestamps are tz-aware again.** All 22 timestamp columns use a new
  `UTCDateTime` type. SQLite has no offset, so `DateTime(timezone=True)` stored
  naive text and returned naive datetimes; `utcnow() - target.last_status_change`
  — the "back up after 4m 12s" line in a recovery alert — raised `TypeError`.
  On-disk format is unchanged, so existing databases still read.

- **Enum columns round-trip as enums.** They were `String(16)` with enum
  defaults, which accepted members on write and returned bare strings on read.
  Now `enum_column()` with `values_callable`, and the enums are `enum.StrEnum`
  so they still format as `down` rather than `HealthStatus.DOWN`.

- **CPU collection no longer silently returns nothing on net-snmp devices.**
  `hrProcessorLoad` is absent on many devices, and the UCD fallbacks
  (`ssCpuIdle`, `ssCpuUser`) are deprecated and unanswered by modern net-snmp —
  so the entire chain was dead on pfSense, OPNsense, and Linux appliances. The
  dead OIDs are renamed `*_DEPRECATED` and documented, the raw counters and
  `laLoad` are added, `DeviceHealth` gains `load_1min` / `load_5min` /
  `load_15min`, and `spark-probe` prints a Load line. `cpu_percent` stays `None`
  rather than a fabricated `0`. Deriving a real percentage needs two samples and
  somewhere to keep the previous one, so it lands with the polling scheduler.

- `change_password` revoked every session including the caller's while its
  comment claimed "every other". Comment corrected to match the behaviour.

### Tests

- **`pytest` failed on a clean checkout (4 failed, 29 passed).**
  `_agent_running()` probed with a UDP `sendto`, which succeeds even when
  nothing is listening, so `needs_agent` never skipped and the live tests ran
  against no agent. It now performs a real SNMP GET. Verified in both
  directions: skips without an agent, runs with one.

- The live health test asserted `cpu_percent is not None`, which SNMP does not
  promise. It now asserts the contract: a percentage if offered, in range, with
  a named source; otherwise a load average.

### Known, unfixed

- `itsdangerous` is a declared dependency that is never imported, and
  `config.secret_key()` writes a key file nothing uses. The README now describes
  the session cookie accurately; DESIGN.md §4 still says "signed session
  cookie", and the dependency and key file should both be removed.
- The Discord webhook URL is stored in the `setting` table in plaintext, while
  DESIGN.md §4 states the database holds only references, never secret material.
- Phase 3 SNMP was built before the Phase 1 check engine, against DESIGN.md's
  own "do not build phase N+1 before N" rule.
- `oids.ENTERPRISE_PREFIXES` maps `1.3.6.1.4.1.25623` to OPNsense; that PEN
  belongs to Greenbone. The table needs an audit against the IANA registry.
- `purge_expired` runs only at startup. Rate limiting is per-IP only and
  failures are not cleared on success. The container runs as root with
  `NET_RAW`. `/api/docs` is unauthenticated.
