# Changelog

## Unreleased — device page controls off the picture (2026-10-05)

### Changed

- **Mute alerts and the 1h / 24h / 7d / 30d range left the header
  picture**, where they covered it and 1h / 7d / 30d were hard to read on
  a bright sky. Mute alerts is a small solid button under the device's
  title line; the time range sits in the Health card's header, beside the
  charts it changes (under the title on a phone).

## Unreleased — the map Diagram on touch screens (2026-10-04)

### Fixed

- **Dragging the Diagram on an iPad moved the whole page with it.** The
  drawing asks the browser not to scroll under it (`touch-action: none`),
  which iPad and iPhone Safari ignore. A touch moving the drawing now holds
  back its own default, so only the map moves. A cancelled touch is also
  let go of, so the map no longer stays "grabbed".

### Added

- **Pinch to zoom the Diagram** with two fingers, about the point between
  them; lifting one finger carries on panning from there without a jump.
  Safari's own page pinch is held back while on the drawing. A tap on a
  device still opens it.

## Unreleased — stat tiles' lit edge reaches the corner (2026-10-04)

### Fixed

- **The coloured edge of the stat tiles was cut off at the bottom.** It was
  a bar skewed at a fixed angle, right only for a tile 96px tall; the tiles
  are taller, so the bar ran out past the slanted side near the bottom and
  was clipped. It is now drawn with a gradient whose line lies on the
  slanted side itself, so it runs corner to corner at any height, on
  phones too.

## Unreleased — Diagram icons the right size everywhere (2026-10-04)

### Fixed

- **Giant icons over the network Diagram** in some browsers. Each device
  box's icon is an `<svg>` inside the drawing's `<svg>`, sized only by the
  stylesheet. Chrome applies that; a browser that does not falls back to
  the SVG default, the full width of the drawing, so each icon was drawn
  hundreds of pixels wide from its box. The icons now carry
  `width="18" height="18"` themselves, which every browser honours.

## Unreleased — Network overview line meets the ring (2026-10-04)

### Fixed

- **The line from SPARK to the subnets met the hub below the ring's
  centre** (by about 13px): it is drawn at the middle of the hub, and the
  hub was the ring and its label. An empty row above the ring, as tall as
  the label below it, now puts the ring's centre on the line, whatever
  the number of subnets. Phones, which draw no line, are unchanged.

## Unreleased — SPARK 2, the smaller pages (2026-10-04)

### Changed

- **A device's page, the target form, Preferences, and the merge and wipe
  previews** carry the same header as the main pages: a small label over a
  big title (the trail back, such as *Devices /*, is the label where a page
  has one), and no page icon.

## Unreleased — the network map as a diagram (2026-10-04)

### Added

- **A Diagram view of the network map** (List / Diagram in the Network
  panel's header). The same map drawn top-down: gateway, switches and access
  points as boxes; a parent's servers with nothing below them in one
  *Servers* group; end devices in a group under what they plug into.
- **Lines take the colour of trouble below them**: amber or red all the way
  down the path to a degraded or down device.
- **Pan and zoom**: drag, Ctrl or ⌘ and scroll, or + / − / Fit. Click a
  device to open it. Find and Problems only dim what does not match.
- The view is remembered in the browser. On a phone the list always shows.
- Laid out on the server and drawn as SVG, with no chart library: SPARK
  still loads nothing from the internet.

## Unreleased — SPARK 2, the Settings pages (2026-10-03)

### Added

- **Six tiles over every Settings page**: Subnets, SNMP profiles, Polled
  devices, Alert rules on (as "9 of 13"), Credentials, and Last backup (the
  time of the last nightly backup, or *failed* in red). Each opens its page.

### Changed

- **The Settings menu is a framed panel**: each entry has an icon, its name
  and state as before, and a line saying what is on that page. On a narrow
  screen it wraps as a row of tabs without the extra line.

## Unreleased — SPARK 2, the Services page (2026-10-03)

### Added

- **Six tiles over the page**: Services, On devices, Watched, Worth a look,
  Distinct ports and Last port scan. They count every service, whatever the
  list below is narrowed to.
- **Chips to narrow the list**: All, Watched, Not watched, Worth a look,
  each with its count, and a subnet dropdown. Both go in the URL with the
  search (`?show=`, `?subnet=`), so a view can be bookmarked.
- **Worth a second look**: every open port with a known way to go wrong,
  why, and a Watch button (or its status once watched). Flagged ports carry
  a warning sign in the list too.
- **Most common ports**: the eight busiest, each a link to every device
  running it.
- **Sort the list** by Service, Port or Device (click the heading).
- **Scan ports on this page**, which comes back here rather than to Devices.

## Unreleased — SPARK 2, the Network map (2026-10-03)

### Added

- **Six tiles over the map**: Placed, Network gear, Servers, Problems, Not
  placed, and SNMP hints (places SNMP suggests, waiting for Accept). They
  keep up with the map as it refreshes.

### Changed

- **One Network panel** holds the Find box, Problems only, Infrastructure /
  Everything, Collapse and Expand all, the Found by SNMP and Not placed yet
  strips, and the tree.
- **Each device is a framed row**: the fold arrow first, a status dot, its
  icon, name, address and role, with its ports as badges on the right. Up
  is the green dot alone; down and degraded keep their label, and the row
  turns red.
- **End devices are framed tiles**, with a ring for one nothing watches.

### Fixed

- **The map's device tiles had turned into round buttons** in one long row
  after the Targets page change: that page's status chips shared their
  class. Scoped to their own toolbar now, with a test.

## Unreleased — SPARK 2, the Devices page (2026-10-03)

### Added

- **Six tiles over the page**: Devices, New, Watched, SNMP polled, Services
  and Last sweep. They count the whole inventory, whatever the filters below
  are showing.
- **A search box** on the device list: name, host name, any address, MAC or
  vendor. Press Enter; it pages and combines with the other filters, and
  lands in the URL like them (`?q=`).
- **A Watched filter**: watched devices only, or only those not watched yet
  (`?watch=yes` or `no`).

### Changed

- **The page is two panels.** *Last sweep* holds Scan now, Scan ports and
  Find SNMP, the sweep's figures, and the automatic-scan schedule, which
  now applies the moment you change it (the Apply button remains for
  browsers without scripts). *Possible duplicates* sits beside it when
  there are any. *All devices* holds the filters, Mark all reviewed and the
  list.
- **Each row** shows the vendor under the name and the MAC under the address
  as small lines (not columns), API credentials beside the SNMP state,
  services as port badges ("ssh 22"), and Watched as a green status. The
  name reads as text with a pencil and becomes a field when you point at it.

## Unreleased — SPARK 2, the Targets page (2026-10-03)

### Added

- **Six tiles over the list**: Targets, Up, Degraded, Down, Paused, and
  Checks / min (what the engine runs a minute at the intervals set).
- **Narrow the list without leaving the page**: status chips with counts, a
  search on name or address, and a check-type filter. Click a column heading
  (Status, Name, Every, Latency) to sort by it. All of it happens in the
  browser and survives the live refresh; with scripts off every row shows.
- **Latency and a trend on each row**: the latency of the latest result, and
  a small line of the last thirty, in the colour of the target's state now.
  Speed-test samples are left out of the trend, as on the charts.
- **How long ago** each target was checked ("12 s ago"), kept current while
  the page is open; the exact time is in its tooltip.

### Changed

- **The page header** has a small "Monitoring" label over the title, and
  **Add target** moved into the list's own header, clear of your name.
- **Row actions are icons** (Check now, Edit, Pause or Resume, Delete), and
  Check now, Pause and Delete no longer reload the page, so a filter stays
  as you set it.
- **Intervals read as people say them** ("15 s", "1 min"), check types as
  badges, and a dependency as a line under the name.

## Unreleased — SPARK 2, the dashboard (2026-10-03)

### Changed

- **The greeting is larger, and the date moved up beside your name**, where
  it no longer sits under the user chip.
- **Each stat tile keeps one colour**: Devices and Services cyan, Monitored
  green, Degraded amber, Down red, Open incidents pink. The number is what
  says something is wrong: it takes the tile's colour only above zero. What
  each tile counts is in its tooltip rather than a line under the number.
- **Network overview replaces the subnet table**: SPARK in the middle with a
  line to each subnet, how it reaches it, its VLAN, and how many of your
  devices sit there (with a bar for their share of the total).
- **Recent incidents sits beside it**, with an amber edge, and shows a calm
  "All systems operational" when nothing has gone wrong and something is
  being watched.
- **Watched devices runs the full width below**, with a View all link to
  Targets and bolder status pills.

### Fixed

- The stray divider left of the avatar in the top bar.

## Unreleased — SPARK 2, the console shell (2026-10-03)

### Changed

- **A new look, applied to every page at once.** A sidebar with icons for
  each page, a banner picture across the top that follows the time of day
  (morning, sunset, night, by your browser's clock), panels with cut corners
  and a cyan frame, and slanted stat tiles. Pages keep their content; each one
  is brought fully into the new design in its own change after this.
- **Dark only.** The light theme is gone: SPARK no longer follows the OS
  light/dark setting. The new design is drawn for a dark page.
- **On a phone or narrow window** the sidebar becomes a bar across the top.

### Upgrade

- Rebuild the container (`docker compose up -d --build`). No database change.

## Unreleased — Devices filters fixed (2026-10-02)

### Fixed

- **The API filter did nothing in a browser.** Choosing an option did not
  reload the page: the script that submits a filter on change named the
  others one by one and missed it. Every filter-bar dropdown now carries a
  class the script looks for, so a new one cannot be missed again (a test
  checks it).

### Changed

- **The SNMP filter's choices read plainly**: Polled, Not polled, Ready to
  add (was "Polled over SNMP", "Answered Find, not polled").

## Unreleased — find API devices on the Devices list (2026-10-02)

### Added

- **An API filter on the Devices list**: any device with a credential, or
  only TrueNAS, Proxmox or UniFi. It shows once there is a credential, and
  survives paging like the others.
- **A pill by the name of each device with a credential** (TrueNAS,
  Proxmox, UniFi), which opens its card on the device page.

### Changed

- **The Devices filter bar sits under the Last sweep card**, right above
  the list it filters.

## Unreleased — UniFi gives routed devices their MAC (2026-10-02)

### Added

- **A device SPARK knows by IP alone gains the MAC UniFi reports for it**,
  on each 5-minute check: SPARK sees no MACs across a router, UniFi does.
  The same rule as the MAC from a router's ARP table (identity.py): exactly
  one MAC-less device at the address, nobody else with that MAC, and not an
  address a polled device says is its own. Never a merge.

### Fixed

- **UniFi devices on routed VLANs were not linked to their SPARK pages** on
  the UniFi card: they were matched by MAC only, and SPARK had none for
  them. They now link by address too, straight away.

## Unreleased — UniFi over its official API (2026-10-02)

### Added

- **UniFi credentials.** Settings → Credentials takes a UniFi Network API
  key (made on UniFi Network's Integrations page) and the console's
  address. Same rules as TrueNAS and Proxmox: sealed on arrival, never shown
  again, sent only over HTTPS and only after you trust the console's
  certificate fingerprint; GET only.
- **A UniFi card on the console's device page**: every adopted device with
  its state, address, firmware (and whether an update is waiting), CPU,
  memory, client count, uptime and the device it uplinks through, linked to
  its SPARK device page when the MAC matches; client totals (wired,
  wireless, VPN, guests). Clients are counted, not stored.
- Written to Ubiquiti's published API reference (developer.ui.com) and read
  tolerantly, so it works the same on UniFi Network 9.1 through 10.6 and
  should keep working as fields are added: every field is optional, numbers
  are read whether written as numbers or text, every page of a list is
  read, and an unknown device state is shown as written.

### Changed

- Settings → Credentials shows each kind's own instructions only for that
  kind.

## Unreleased — base image refreshed (2026-10-02)

### Security

- **`python:3.12-slim` re-pinned to a current digest**
  (`sha256:dddfd7e0…4e0016`). Trivy, now running again in CI, found 7
  HIGH/CRITICAL fixed vulnerabilities in the Debian 13.7 packages of the
  September image; the Python packages were all clean. Still Python 3.12.
  Rebuild: `docker compose up -d --build`.
- **Debian security updates are applied at build** (`apt-get upgrade` in
  the Dockerfile). The fresh digest still carried packages with fixed
  HIGH/CRITICAL CVEs: the official image is rebuilt on its own schedule and
  Debian's fixes land in between.

## Unreleased — CI actions pinned by commit, Trivy working again (2026-10-02)

### Fixed

- **The image scan job ran nothing since it was added**: it asked for
  `aquasecurity/trivy-action@0.28.0`, a tag that no longer exists. After the
  March 2026 trivy-action compromise (attackers force-pushed nearly every
  version tag to secret-stealing code; GHSA-69fq-xp46-6x23) the old tags
  were deleted. Now `v0.36.0`, an immutable release. SPARK's CI never ran
  the compromised code: the tag was already gone when CI was added, and the
  job failed before running anything.

### Changed

- **Every GitHub Action is pinned to a full commit SHA** with its version in
  a comment: checkout v7.0.1, setup-python v7.0.0 (off the deprecated
  Node 20), trivy-action v0.36.0.
- **Dependabot proposes only new `python:3.12-slim` digests**, not a jump to
  a newer Python (3.12 → 3.14 needs the locks, tests and CI moved
  together). Supersedes Dependabot PRs #3, #5 and #6; #4 (Python 3.14) is
  declined.

## Unreleased — a short README; the detail in docs/ (2026-10-02)

### Changed

- **The README is one page**: what SPARK is and does, and how to deploy,
  upgrade, back up, restore and reset the password. Everything else moved,
  unchanged, into `docs/`: `guide.md` (using SPARK, page by page),
  `configuration.md` (spark.yaml, TLS, Docker settings, cost, sign-in,
  first start and upgrading in detail) and `development.md` (status, tests,
  CI, layout, conventions, roadmap, design decisions). Links between them
  were rewritten and checked. CLAUDE.md says where each kind of change is
  documented now. No code change.

## Unreleased — backup and restore (2026-10-02)

### Added

- **Settings → Backup.** A backup is the database (copied with SQLite's
  online backup, consistent while SPARK runs), `secret.key` and the HTTPS
  certificate, with a manifest saying what made it.
- **Nightly backups** at 04:00 into `data/backups/`, the last seven kept
  (0600, like the files they copy). The page shows how last night went.
- **Encrypted download**, of one made now or a nightly one: a passphrase
  typed twice (12+ characters, never stored), scrypt and AES-256-GCM in
  1 MiB chunks, numbered and the last one marked, so an altered or
  truncated file fails instead of restoring. A download is a sign-in
  security event in Discord.
- **`spark-restore`**, with SPARK stopped: `docker compose run --rm spark
  spark-restore /data/<file>`. Checks everything first (passphrase, every
  chunk, only SPARK's own files in the archive, database integrity and
  version), refuses while SPARK runs or for a backup from a newer SPARK,
  and moves the old data to `data/pre-restore-<when>/` instead of deleting
  it.
- SPARK holds a lock file on its data directory (`data/.spark.lock`) while
  it runs, which is how `spark-restore` knows.

### Upgrade note

New command: rebuild (`docker compose up -d --build`) as usual. No
migration.

### Tests

48 new (`test_backup.py`).

## Unreleased — the sessions list shows only live sessions (2026-10-01)

### Fixed

- **Preferences → Account listed every session that had timed out** as if
  it were still signed in. A session that goes unused past the timeout (30
  minutes unless changed) is refused on its next request, but its row
  stayed until its 30-day expiry — so each time-out and fresh sign-in added
  a row, and "Sign out everywhere else" counted them all. The list and the
  count now leave timed-out sessions out, using the timeout set under
  Preferences, and the startup and nightly clean-ups delete them.

### Tests

3 new (`test_account.py`), which fail without the fix.

## Unreleased — performance review: memory, CPU, shutdown, logs (2026-10-01)

Findings P1–P4 of the 2026-10-01 performance review. No change to what
SPARK does or how it is reached; `docker compose up -d --build` and you are
done. Measured on the same workload before and after (30 SNMP devices
polling every 30 s, a burst of 30 concurrent Tests at 2½ minutes):
CPU 113 s → 69 s over six minutes, resident memory 153 MB → 137 MB, and the
burst added 4 MB instead of 17.

### Changed

- **One SNMP engine per credential, reused across polls** (P1). Every poll
  used to build its own pysnmp engine, and building one reads thirteen MIB
  modules from disk and compiles them — 70–80 ms of CPU and ~5 MB of
  objects per poll, thrown away a second later. Overlapping polls (the first
  minute after a start, Find, Test on several rows) left the heap 20–50 MB
  larger for good. Engines now live for the life of the process, one per
  distinct credential — never one for the whole process, because pysnmp
  keys SNMPv3 users by name and two profiles sharing a user name with
  different keys would fight over one engine. A socket or dispatcher error
  evicts the engine and the next poll builds a new one; a timeout or a
  refused credential is the device talking and keeps it. For SNMPv3 this is
  also fewer packets: the agent's engine ID is remembered rather than
  discovered on every poll.
- **Shutdown is bounded to 3 seconds** (P2). Since HTTPS, closing an idle
  browser connection meant waiting up to 30 s for a TLS close_notify from a
  peer that was not reading — and Docker SIGKILLs after 10 s, so every
  `docker compose restart` with a tab open ended in a kill with no scheduler
  shutdown. uvicorn now stops waiting after 3 s and runs the shutdown; the
  compose file's `stop_grace_period: 8s` sits above that. Measured: 30.1 s
  before, 3.2 s after. uvicorn's note about it is logged at INFO, not ERROR.
- **APScheduler's per-job log lines are off** (P3). "Running job" and
  "executed successfully" were 84 % of the log — 10–15 MB a day into
  Docker's log, which the compose file now caps at 5 × 10 MB. A missed run,
  a skipped overlap and a job's own traceback still log.
- `MALLOC_ARENA_MAX=2` in the image (P4): 4 % less resident memory, free.
- `mem_limit: 512m` is on, with the measurement behind it in the compose
  file: ~100 MB is Python and its libraries, 141 MB on a real network,
  160 MB with thirty SNMP devices, plus 128 MB of transient peak when two
  people sign in at once (Argon2 at 64 MB a hash, two allowed). Exit 137 in
  `docker compose ps` means it was hit.

### Not changed, measured

- No leak: a 12-minute soak at 60 polls a minute sat flat at 161.7 MB.
- Disk: one SQLite commit per check or poll writes about 40 KB (WAL frames,
  then the checkpoint). On a real network that was 2.6 GB in 14 hours —
  harmless for an SSD's lifetime, worth knowing on ZFS. Batching commits
  would cut it several-fold; not done, since it changes when a result is
  on disk. In the backlog.

## Unreleased — security review, round three: the visible batch (2026-09-30)

Findings #25, #26, #29 and #32 of the 2026-09-30 review, and its
defence-in-depth list. **This one changes how SPARK is reached** — read
*Upgrading* below before `git pull`.

### Security

- **HTTPS by default** (#25). `app.tls: auto` makes a self-signed
  certificate on first start (`data/tls/`, EC P-256, ten years, 0600) and
  prints its SHA-256 fingerprint to the log for the person to compare in the
  browser, the way TrueNAS's and Proxmox's are checked. `off` is plain HTTP
  for a reverse proxy in front; `{cert, key}` is an operator's own
  certificate, and the only mode that sends `Strict-Transport-Security` —
  HSTS with a certificate the browser does not trust is a lockout. Over
  HTTPS the session cookie is `__Host-spark_session`; both names are read,
  so a session survives the switch. The healthcheck tries HTTPS, then HTTP.
  `SPARK__APP__TLS=off` from the environment.
- **`app.allowed_hosts`** (#29): when set, any other `Host` gets a 421 —
  the DNS-rebinding defence. Empty (the default) means any, as before;
  loopback is always allowed for the healthcheck.
- **`config/spark.yaml` is no longer tracked** (#32). The shipped file is
  `config/spark.example.yaml`; the copy you edit is git-ignored, so `git
  pull` never fights it and a fork never pushes a real network's subnets or
  proxy addresses. A container whose `SPARK_CONFIG` names a missing file
  now stops with the `cp` command instead of running on defaults.
- **Sign-in events go to Discord**: the lockout tripping (once per window,
  with the address), a sign-in from an address no session has come from
  before, the password changed or reset, first-run setup completing. A
  toggle on the Alerts card (on by default).
- `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy:
  same-origin` on every response.
- **Running without `NET_RAW`** is now a documented option: the Dockerfile
  takes `SETCAP_NET_RAW=0`, and the compose file carries the
  `ping_group_range` sysctl, the `no-new-privileges` line and a
  `read_only: true` block, commented, with what to check. Not switched on
  by default: neither could be exercised end to end here, and a sweep that
  silently finds nothing is worse than a capability.
- **CI, Dependabot and a security policy** (#26). `.github/workflows/ci.yml`
  runs the test suites from the hash-pinned locks, `pip-audit` and `bandit`,
  and builds the image, scans it with Trivy and polls its healthcheck over
  TLS — on every push, and every Monday so a vulnerability published later
  still fails a run. `.github/dependabot.yml` proposes base-image digests
  and action versions (not pip; the lock is uv's). `SECURITY.md` says how
  to report privately and what is in scope. Two things only the owner can
  do, and should: hardware-key 2FA on the GitHub account and branch
  protection on `main`.

### Upgrading

1. `git pull` will refuse if your `config/spark.yaml` has local edits,
   because this commit renames the tracked file. Keep a copy and put it
   back:
   ```bash
   cd ~/NetworkMonitoringApp/spark
   cp config/spark.yaml /tmp/spark.yaml
   docker compose stop && sudo cp -a data data.bak-$(date +%F)
   git pull
   cp /tmp/spark.yaml config/spark.yaml
   docker compose up -d --build
   ```
2. The URL is now `https://<host>:9700`. `http://` bookmarks stop working
   (one listener cannot serve both); the browser warns once about the
   certificate — compare the fingerprint in `docker compose logs spark`
   and accept. Anything else polling `/healthz` over http needs `https`
   and `-k`. Behind a reverse proxy that terminates TLS, add `tls: off`
   under `app:` in `spark.yaml` first.
3. Nothing else changes: same port, same sign-in, same data.

### Docs

README: Quick start, Configuration (TLS, allowed hosts), Docker settings
(without NET_RAW), Alerts table, Authentication (transport, sign-in events),
Continuous integration, Upgrading, project layout. CLAUDE.md: TLS, config
file and sign-in event conventions; deployment reality. SECURITY.md new.

### Tests

29 new: `test_tls.py` (the certificate, every `app.tls` spelling, and HTTPS
under a real uvicorn — `__Host-` cookie, no HSTS, the healthcheck script over
both schemes), `test_security_events.py`, Host allowlist and header tests
in `test_hardening.py`, config-file tests in `test_setup_code.py`.

### Known, unfixed

`read_only: true` and the `NET_RAW`-free layout are documented, not
default. Action versions in `ci.yml` are tags, not SHAs; Dependabot keeps
them current, and pinning by SHA is the next step once it has run once.

## Unreleased — security review, round three (2026-09-30)

Findings #22–#24, #28, #30, #31, #33 and #34 of the 2026-09-30 review. Nothing
here changes how SPARK is reached: same port, same URL, same sign-in. Rebuild
required (`docker compose up -d --build`): the lock and a console script
changed. Back up `data/` first — there is a migration.

### Security

- **SPARK decides which peers are proxies; uvicorn no longer does** (#22,
  High). uvicorn's proxy-header handling trusted 127.0.0.1 and ::1 by default
  and rewrote the client address from `X-Forwarded-For` before SPARK saw it.
  With host networking, loopback is every container and process on the VM,
  so from there anyone could pick their own address: a fresh login-lockout
  bucket per request, and in proxy mode a forged `X-Forwarded-For` naming the
  real proxy satisfied `trusted_proxies` — admin with no password. A proxy
  on the same host, meanwhile, was refused. Now `proxy_headers=False`, and
  `web/hardening.py` applies `X-Forwarded-For`/`-Proto` only from a peer in
  `auth.proxy.trusted_proxies` (`proxies.py`), in both auth modes; the
  identity header is checked against the TCP peer (`request.state.peer`),
  never against a header. Setting `trusted_proxies` in password mode now
  keys the lockout on real clients behind a TLS proxy and makes the cookie
  `Secure` when the proxy says https (#27). Proxy mode logs a warning when
  listening on every interface.
- **First-run setup needs a code from the log** (#23). `/setup` was owned by
  whoever reached the port first. SPARK now prints a 12-character setup
  code when it starts with no account (and makes one on demand if the
  account is ever deleted); the form requires it, wrong guesses count
  toward the login lockout, and it is spent once the account exists. A
  partial unique index on `user.is_admin` (migration 20) makes the database
  itself allow exactly one administrator, so concurrent claims yield one
  account, not several; a database that somehow already has two stops at
  start with the command to fix it.
- **The password can be changed, and lost ones reset** (#24). Preferences →
  Account: change the password (ends every session and gives this browser a
  fresh one), see every session with its address and browser, **Sign out
  everywhere else**. `spark-reset-password`, run with `docker compose exec
  spark spark-reset-password`, sets a new password from the machine SPARK
  runs on and signs out every session.
- **Argon2 runs in a thread, at most two at once** (#28). It ran on the
  event loop, so each login attempt (120 ms, 64 MiB) stalled every check and
  poll, and a flood of attempts was a flood of stalls.
- **The HTTP check keeps at most 1 MB of a body** (#30), streamed, and only
  when `expect_body` is set; `expect_body` matches within that. A monitored
  host serving something enormous could push SPARK out of memory.
  `pids_limit: 256` in compose; `mem_limit` is there commented, with how to
  size it.
- **Everything under `data/` is 0600** (#31): umask 077 at start, and the
  database, its `-wal`/`-shm` and `secret.key` are chmod-ed if an older
  version left them 0644. Reading `data/` from the host now takes `sudo`.
- **`websockets` is declared** (#33): the TrueNAS client imports it and it
  arrived only through `uvicorn[standard]`. `uvloop` and `httptools` are
  declared too, the `[standard]` extra is dropped, and `watchfiles` and
  `python-dotenv` — development conveniences that never ran in the
  container — leave the closure: 39 packages → 37. Lock regenerated with
  every other pin and hash unchanged.
- The version number is no longer on the sign-in page, and the proxy-mode
  sign-in page no longer names the identity header (#34).

### Docs

README: Quick start (setup code), Monitoring (body cap), Preferences
(Account card, `spark-reset-password`), Authentication (proxies, both
layouts, first run, files), dependency count, project layout. CLAUDE.md:
client-address and Argon2 conventions, closure count.

### Tests

50 new: `test_live_server.py` (under a real uvicorn — the test client never
saw uvicorn's middleware, which is why #22 went unnoticed), `test_setup_code.py`,
`test_account.py`, `test_http_check.py`, `test_data_privacy.py`; proxy-header
tests in `test_hardening.py` rewritten against the middleware. Every setup
POST in the suite and the smoke test now sends the code.

### Known, unfixed

Findings #25 (TLS of SPARK's own), #26 (CI, Dependabot, SECURITY.md, tags),
#29 (`Host` allowlist) and #32 (`spark.yaml` tracked) are the visible batch
and wait for their own commits. Argon2 has no global attempt budget on
purpose: with one it would be a login-DoS handle for anyone on the LAN.

## Unreleased — targets open their device (2026-09-30)

### Added

- **A target's name on the Targets page opens its device's page**: the
  device it was watched from, or else the device at its address (primary,
  merged, or by host name; a URL's host counts). A target with no device
  to open, such as an outside website, stays plain text.

### Tests

1 new (`test_targets_page.py`).

## Unreleased — Proxmox token in two fields (2026-09-30)

### Changed

- **Adding a Proxmox credential asks for the Token ID and the Secret
  separately**, as Proxmox shows them when a token is made, instead of one
  `USER@REALM!TOKENID=SECRET` string. A whole token pasted as the secret
  still works. The Token ID field and Proxmox's instructions show only when
  Proxmox is chosen (TrueNAS's only for TrueNAS). On an edit, either half
  left empty keeps the saved one. Errors say which half is wrong, and the
  Token ID typed is kept on the form (the secret never is).
- The Name placeholder says it is SPARK's own label.

### Tests

10 new (`test_proxmox.py`).

## Unreleased — Proxmox over its API (2026-09-30)

### Added

- **Proxmox credentials.** Settings → Credentials takes a Proxmox API
  token (`USER@REALM!TOKENID=SECRET`, the PVEAuditor role on `/`). The
  certificate is pinned as for TrueNAS, and the token is written onto the
  connection only after the certificate is checked. GET only; port 8006
  unless the address gives another.
- **A Proxmox card on the device page**: each node (version, CPU, memory,
  uptime), every VM and container (state, CPU, memory, uptime), each
  enabled storage (active, space), each ZFS pool's health, and each drive's
  SMART health and life left. Read every 5 minutes with the credential check.
- **Watch** a VM or container from that card. Only watched guests alert.
- **Alerts**: a new rule, *A watched VM or container stops* (two checks in
  a row, or no longer listed). Proxmox also feeds *A pool is not ONLINE*
  (a ZFS pool, or a storage that is not active), *A pool is … full* (its
  storages) and the drive rule, now called *A drive is failing* (SMART not
  PASSED). All appear as dashboard incidents and can be suppressed per device.
- Migration 19: `api_credential.options`, for the watched guests.

### Fixed

- On a phone, a table's visually hidden column heading (the bar column on
  the Storage card) could widen the whole page sideways. Table scrollers now
  contain it.

### Tests

47 new (`test_proxmox.py`), against a real TLS server.

## Unreleased — NAS, UPS and camera roles (2026-09-30)

### Added

- **Three more device roles**: NAS, UPS and Camera, each with its own icon
  on the map, chosen on a device's page like the others. A NAS and a UPS
  are rows on the map, like a server; a camera is a tile, like a phone. A
  NAS also counts as something devices can sit behind on a switch port
  (its apps and VMs) when the map is read from SNMP.
- Stored as text, so no migration.

### Tests

2 new (`test_service_map.py`, `test_topology.py`).

## Unreleased — a compact, searchable network map (2026-09-30)

### Changed

- **The network map is built to find things.** A **Find** box (name,
  address, MAC, vendor, role, ports) keeps the matches and the path to
  them; **Problems only** keeps what is down, degraded or not answering.
  Both ignore folds while in use, and the count says how many matched.
- **End devices are tiles**, in a grid under the switch or access point
  they hang off; infrastructure stays as rows. **Infrastructure** (the
  default) folds each group to its count ("9 devices · 1 with a problem
  ▸"); **Everything** shows them all.
- **Found by SNMP** and **Not placed yet** are one line each at the top
  until opened, and stay open across Accept / Not right.
- Unwatched ports are a count ("+6 ports") linking to that device on the
  Services tab, instead of a list to open on each row.
- The view, folds and opened groups are remembered in the browser; the
  Find text and Problems only survive live refreshes. On a phone the tree
  is indented less and tiles sit two across.

### Tests

`test_service_map.py`: the toolbar, tiles and their problem counts, ports
as a count, Not placed yet as tiles; `test_topology.py` for the new strip.
Find, Problems only, the views, remembering, and a live refresh keeping
the Find text were checked in a browser. `pytest` and `smoke_test.py` pass.

## Unreleased — suppressed alerts leave the dashboard (2026-09-30)

### Changed

- **Recent incidents and Open incidents leave suppressed alerts out**: the
  incident a suppression closed, and every earlier one of a rule that is
  now off for its device. Removing the suppression brings the earlier ones
  back (the one it closed stays out). A rule given its own line keeps its
  history. A TrueNAS alert type no longer listed is still recognised, from
  the incident's own words.

### Tests

5 new in `test_suppressions.py`; seven deliberate breaks each caught.

## Unreleased — Suppressions (2026-09-30)

### Added

- **Settings → Suppressions.** One alert rule, for one device: off, or its
  own line (memory over 100% on the NAS, CPU over 98% on a switch), while
  the global rules under Alerts still apply everywhere else. Lines for CPU,
  memory, temperature, port traffic, pool and disk space, and drive
  temperature; the others (ports down, pools not ONLINE, SNMP going quiet,
  drive errors, API credentials, TrueNAS alerts) can only be off. TrueNAS's
  own alerts can be suppressed by type (PoolUSBDisks) or all at once.
- Fully quiet: no Discord message and no incident. Saving or removing one
  starts that rule afresh for that device, so an alert standing at the time
  closes as "suppressed" instead of hanging on below a raised line.
- The TrueNAS alert type field shows only when the alert chosen is "A
  TrueNAS alert".
- A **Suppress** link beside each alert on the dashboard's Recent
  incidents, which opens the form filled in with that device and rule (and
  the TrueNAS alert type).

### Changed

- Migration 18: the `alert_suppression` table.

### Tests

28 new (`test_suppressions.py`), across SNMP thresholds, storage lines,
SNMP going quiet, and the TrueNAS API (by type, all types, drive errors,
the API itself); sixteen deliberate breaks each caught. `pytest` and
`smoke_test.py` pass; checked in a browser in both themes and at phone
width.

## Unreleased — alerts are incidents too (2026-09-30)

### Added

- **The dashboard's incidents include alerts, not only outages.** Every
  alert rule that fires opens an incident — SNMP CPU, memory, temperature,
  starred ports, storage (pools, space, drive heat), a polled device that
  stops answering SNMP, an API credential that stops working, a drive with
  errors, TrueNAS's own alerts — and it closes when the rule clears. **Open
  incidents** counts them with the target outages; **Recent incidents**
  lists both, newest first, each with where it came from (Target, SNMP,
  Port, Storage, API, TrueNAS) and a link to its device.
- Recorded for a muted device too (only the message is held back); not
  while a rule is switched off. Unstarring a port or removing a credential
  closes its incident as "no longer watched". An alert already standing
  when this is deployed gets its incident on its next check.

### Changed

- Migration 17: the `alert_incident` table.
- A polled device that stops answering SNMP while muted, or with alerts
  switched off, now records the incident and still sends nothing.

### Tests

13 new (`test_alert_incidents.py`), and incident checks added to the
storage, credential and TrueNAS tests; sixteen deliberate breaks each
caught. `pytest` and `smoke_test.py` pass; checked in a browser in both
themes and at phone width.

## Unreleased — Network map, and Services as its own tab (2026-09-29)

### Changed

- **Service map is now Network map**: the tab, the page, the device page's
  "On the network map" card, Preferences and setup. Still at `/map`.
- **Services is its own tab** (`/services`): the searchable list of every
  service that sat under the map, with a count ("1 of 3 match") and the
  Watch button. The map links to it; so does the dashboard's Services tile,
  which was not a link before. An old `/map?q=…` link redirects there.
- Phone row: six links now, so "Dashboard" reads "Home" there as "Network
  map" reads "Map", and the smallest phones get slightly smaller text. Every
  link fits at 320 px.

### Tests

5 new in `test_service_map.py`: the tab, the map without the list, the old
search link, the dashboard tile. `pytest` and `smoke_test.py` pass; checked
in a browser in both themes and at 320 and 390 px.

## Unreleased — larger tile icons, and Watched as a button-shaped status (2026-09-29)

### Changed

- "Watched" on the Devices list is the same box as Details, Watch and
  Ignore, in status green, instead of a rounded pill that changed the row's
  shape.
- The icons in the tinted tiles were half the tile's width and read as
  specks. Now about two thirds: 26 px in a card's 40 px tile, 28 px in a
  page's 44 px, 22 px in a dashboard tile's 36 px.

## Unreleased — drive health and TrueNAS alerts over the API (2026-09-29)

### Added

- **Drive health from the TrueNAS API**, read by the 5-minute credential
  check over the same login, with query methods only. Checked first with a
  read-only probe against a real TrueNAS 25.10.6 box and a Read-only
  Administrator key: `pool.query` (status, last scrub, and the topology,
  whose DISK leaves carry each drive's state and read/write/checksum error
  counts), `disk.query`, `disk.temperatures`, `alert.list`. `disk.query`'s
  own `pool` is empty on 25.10, so a drive's pool comes from the topology.
- **The Storage card** gains a drive table (model, size, pool and vdev,
  state, errors r/w/c, temperature), a Last scrub column, and TrueNAS's
  current alerts. Shown on devices without SNMP too. Serials are not kept.
- **Two rules** under Settings → Alerts → APIs, on by default: a drive not
  ONLINE or with any errors (again once ONLINE with none); TrueNAS's own
  alerts at WARNING and above, once each, and again when cleared or
  dismissed in TrueNAS.

### Changed

- Migration 16: `api_credential.readings`.
- A TrueNAS call that times out or loses its connection is now a
  TrueNASError with a readable message, so it shows on the credential
  rather than stopping the check.

### Tests

22 new (`test_truenas_health.py`), with fixtures in the shapes the real box
returned; nineteen deliberate breaks each caught. `pytest` 1110 passed with
the agent, `smoke_test.py` 76 passed. Checked in a browser in both themes
and at phone width.

## Unreleased — the API on the device page, checked every 5 minutes (2026-09-29)

### Added

- **A card on the device page for each API credential** it has: *connected*
  with the TrueNAS version, host name and when it was last checked;
  *waiting for you* with a link to check the certificate; or *not
  connected* with when it last worked and why not. A Test button that comes
  back to the same page.
- **Checked every 5 minutes** once a certificate is trusted (never before:
  that would only fetch the certificate again).
- **An alert when a credential stops working**, on two failed checks in a
  row, and again when it works. Settings → Alerts → APIs; the mute list
  applies. Changing a credential's address or key, or removing it, forgets
  the alert quietly.

### Fixed

- Alert rules added in a later version (storage, and now APIs) showed
  unticked on Settings → Alerts for a setup that had saved the rules form
  before they existed, and saving it switched them off. The saved rules are
  now read over the defaults.

### Changed

- The rules form's button is "Save alert rules", as it holds more than SNMP.

### Tests

17 new in `test_credentials.py`. Fifteen deliberate breaks each caught (one
more, the job's own filter for untrusted credentials, is backed by the same
check in `check_one` and changed nothing). `pytest` 1086 passed with the
agent, `smoke_test.py` 76 passed. Checked in a browser in both themes and at
phone width.

## Unreleased — no homelab details in anything shipped (2026-09-29)

### Changed

- Placeholders and examples are generic now. The Credentials form suggests
  `truenas` and `192.168.1.20 or truenas.lan`; Targets, Subnets and the map
  search use `192.168.1.x`. The README, code comments, smoke test and tests
  use generic names (truenas, office-switch, ap-hall, pool `tank`) and
  generic ranges (`192.168.1.0/24`, `172.16.x.0/24`) in place of real ones.

### Tests

- `test_no_homelab_details.py` fails if a real host, device or pool name, or
  the real address scheme, appears in any shipped file (code, templates,
  docs, tests). It lists them only as SHA-256 hashes. Checked by planting a
  name, an address and a range: each was caught.

## Unreleased — Settings → Credentials, and the TrueNAS API client (2026-09-29)

### Added

- **Settings → Credentials**, for keys to devices' own APIs. TrueNAS first;
  SNMP profiles stay under Settings → SNMP.
- **A TrueNAS API client** (`truenas.py`): JSON-RPC 2.0 over a WebSocket at
  `wss://<host>/api/current`, as TrueNAS 25.x documents it (REST is
  deprecated in 25.04 and removed in 26). HTTPS only — TrueNAS revokes a key
  sent over plain HTTP — with no fallback.
- **Certificate pinning.** The first Test fetches the certificate and sends
  nothing; the page shows its SHA-256 fingerprint; **Trust** pins it (only if
  it is still the one last seen) and logs in. A different certificate later
  stops everything and shows the new fingerprint. A new address or key
  forgets the pin.
- A credential shows *connected* with the TrueNAS version and host name,
  *waiting for you* while a certificate needs checking, or *not connected*
  with the reason. Keys are sealed on arrival, never shown again, and never
  put back into a page, even when the form is re-shown with an error.

Nothing reads storage over the API yet; that is next.

### Changed

- Migration 15: the `api_credential` table.

### Tests

33 new (`test_credentials.py`), against a TLS WebSocket fake of TrueNAS that
can swap its certificate mid-test and records every login attempt. Each
deliberate break tried was caught. `pytest` 933 passed with the agent,
`smoke_test.py` 76 passed. Checked in a browser in both themes and at phone
width.

## Unreleased — storage: TrueNAS pools and drives, disk space anywhere (2026-09-29)

### Added

- **A Storage card** on a polled device's page, read every 5 minutes:
  - TrueNAS: each pool's health and space, and each drive's temperature,
    from TRUENAS-MIB. Checked against a real TrueNAS SCALE 25.10 box: the
    pool table there has health and I/O counters but no sizes, so a pool's
    space is its root dataset's used + available (the boot pool has no
    dataset row and shows health only). Drive temperatures come in
    thousandths of a degree.
  - Anything running net-snmp: its real filesystems from hrStorageTable
    (fixed disks only; /proc, /sys, /dev, /run, snaps, container layers and
    tiny mounts left out; a bind mount shown once). Not on TrueNAS, where
    that table repeats every dataset.
- **Storage alerts**, under Settings → Alerts: a pool not ONLINE (at once,
  and again when ONLINE), a pool 85% full or more, a disk 90% full or more
  (two reads in a row each), a drive at 50 °C or more for 10 minutes. Space
  and heat alerts end 5 under their line. The mute list and each rule's
  switch apply as for the other SNMP alerts.
- Read on their own 5-minute schedule, one device at a time, with a
  30-second timeout: TrueNAS works these values out when asked and its
  whole agent waits while it does. Only devices answering their polls are
  asked.

### Changed

- Migration 14: the `snmp_storage` table. New rules in `alert_rules`.

### Tests

20 new (`test_storage.py`); seventeen deliberate breaks each caught (one
more changed nothing and is harmless). The rules-form test covers the new
fields. `pytest` 900 passed with the agent, `smoke_test.py` 76 passed.
Checked in a browser in both themes and at phone width.

## Unreleased — manual or automatic map, and Wipe map (2026-09-28)

### Added

- **Map mode**, asked at setup and changed under **Preferences → Service
  map**. Manual (the default, and what an upgraded install keeps): SNMP
  suggests, you accept. Automatic: after every SNMP read, devices are placed
  where the switches see them, and a device automatic placed follows SNMP
  when it moves. Switching to automatic applies it at once.
  - Never touched: a place set on a device's page, one accepted from a
    suggestion, and one automatic set that a person then changed or
    cleared. Moves that would make a loop are skipped. Roles are filled in
    only where none is set.
  - "Not right" is respected, and on a device automatic placed it takes the
    place back off.
  - The map page says the map is automatic instead of listing suggestions;
    a device's page says "Placed there automatically".
- **Wipe map** (Preferences → Service map): a page first says what it will
  clear (roles, places including hand-set ones, "Not right" answers) and
  that devices, targets, services, alerts and history stay. Automatic
  mode rebuilds the map at once. It warns when the gateway is known only by
  its role, since the wipe clears that too.
- Merging keeps what automatic placed below the duplicate as automatic's.

### Changed

- Migration 13: the `map_auto` table (which places automatic set). A new
  `map` setting.

### Tests

20 new (`test_map_mode.py`); nineteen deliberate breaks each caught. The
Preferences save-button test now counts three cards. A browser run of setup,
Preferences, the map, a device page and the wipe page, at desktop and phone
width. `pytest` 880 passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — suggestion buttons stay put (2026-09-28)

### Fixed

- **Accept**, **Not right** and **Accept all** on the map, and **Not the
  same** on Devices, no longer jump the page down to the suggestions card.
  They are sent in the background and the page's live region is swapped for
  the answer, so the page stays where it was scrolled to. Without
  JavaScript they post and redirect as before.

### Tests

1 new in `test_topology.py`, one assertion in `test_identity.py`. A browser
run checks the scroll position is unchanged after each button. `pytest` 859
passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — a foldable map, and quieter ports on it (2026-09-28)

### Changed

- **Branches fold.** Each device on the map with anything below it has a
  toggle; folded, it reads "N below". **Collapse all** and **Expand all** sit
  above the tree. What is folded is remembered in the browser (by device)
  and survives the page's live refresh. Without JavaScript every branch is
  open, as before.
- **Ports on the map.** Watched ports (a target on them) stay on each device,
  since they carry a status. The rest fold under their count ("3 ports",
  "+6 more") and open on a click. The Services table is unchanged and still
  lists everything.
- The live refresh now announces each swap (`spark:live-updated`), so a
  page can put back what it keeps in #live.

### Tests

2 new in `test_service_map.py`. A browser run checks folding, Collapse and
Expand all, that a fold survives a reload and a live refresh, an opened
"+N more" survives a live refresh, and the page without JavaScript.
`pytest` 858 passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — interfaces as a list, not a long table (2026-09-28)

### Changed

- A device page's **Interfaces** card now shows one chart, with an
  **Interface** list above it to pick the port (status and current rate in
  each entry, starred ports marked ★). Picking one shows it straight away and
  keeps the chosen time range; the page lands back on the chart. Without
  JavaScript a **Show** button does the same.
- Under the chart: the port's status, rate now, peak in the range, errors,
  and **Star for alerts**.
- The full interface table is still there, folded under **All N
  interfaces**, for starring several ports or scanning status.

### Tests

2 new in `test_device_page.py`. `pytest` 856 passed with the agent,
`smoke_test.py` 76 passed. Checked in a browser with a 48-interface access
point, both themes and phone width.

## Unreleased — the map from SNMP, as suggestions (2026-09-28)

### Added

- **MAC tables and LLDP.** With the addresses and ARP table, every 15
  minutes, each polled device is now asked for its MAC table (Q-BRIDGE-MIB,
  or BRIDGE-MIB) and its LLDP neighbours. Learned entries only, plus the
  switch's own MACs; bridge ports are mapped to interfaces.
- **Found by SNMP** on the map page: where those tables put each device not
  placed yet, with **Accept**, **Accept all** and **Not right**. A switch
  with a MAC table is also offered the Switch role, and a gateway found by
  its addresses the Gateway role.
  - The top is the device whose role is Gateway, or else the polled device
    with the most addresses of its own.
  - A device is on the nearest switch that sees it away from the gateway,
    on the port it was learned on.
  - Several devices on one port with exactly one piece of infrastructure
    among them are behind it (wireless clients and their AP, VMs and their
    host). Two access points on one port stay on the switch until one is
    placed under the other by hand.
  - LLDP, where there is any, wins over the MAC tables.
  - Contradictory tables (stale entries, loops) place nothing rather than
    guess.
- Each device's **On the service map** card says where SNMP sees it, with
  **Use this** while that is still a suggestion.
- Nothing is placed without being accepted, and a parent set by hand is
  never replaced; accepting a role leaves the parent alone.

### Changed

- Migration 12: the `snmp_neighbour` table. A new `map_dismissed` setting.
- The 15-minute job is now "Read SNMP addresses, ARP and MAC tables".

### Tests

37 new (`test_topology.py`): the inference against a UniFi home network
(MAC tables, no LLDP, a mesh AP), an LLDP network, a gateway with its own MAC table, and stale or
contradictory tables; parsing; the pages. Twenty-six deliberate breaks each
caught. The form-cap test now covers the map routes. A browser run in both
themes and at phone width. `pytest` 854 passed with the agent,
`smoke_test.py` 76 passed.

## Unreleased — merge suggestions from SNMP (2026-09-28)

### Added

- **Every 15 minutes, each device on the SNMP list is asked for its own
  addresses** (IP-MIB `ipAddrTable`, or `ipAddressTable` on newer agents)
  **and its ARP table** (`ipNetToMediaTable`, or `ipNetToPhysicalTable`).
  45 seconds after start, and again shortly after a device joins the SNMP
  list, rather than a full 15 minutes later.
- **Possible duplicates**, from those answers. Nothing merges on its own:
  - a device at one of a polled device's own addresses (the firewall's
    gateway on each VLAN) is suggested as part of it;
  - a device with no MAC whose address the ARP table puts at a MAC another
    device has (a server with VLAN interfaces on one NIC) is suggested as
    part of that device;
  - two MAC-less devices at one MAC nobody has yet are suggested as one.
  Listed on the Devices page, on the kept device's Addresses card, and on
  the duplicate's own page. **Review merge…** opens the usual preview, which
  now says why it was suggested. **Not the same** stops that suggestion for
  good.
- **MACs across a router.** A device with no MAC gets the one the ARP table
  gives for its address, when exactly one device is there and no device has
  that MAC yet. Never for an address a polled device says is its own: that
  device is a duplicate, and a MAC would block its merge.
- The device page says how many addresses and ARP entries a polled device
  reported, and when.
- SNMP **Test** lists "Its own IP addresses (IP-MIB)".

### Not suggested

Anything the merge would refuse (two different MACs, both polled over SNMP),
ignored devices, and addresses two routers disagree about.

### Changed

- Migration 11: the `snmp_address` table. A new `merge_dismissed` setting.

### Tests

32 new (`test_identity.py`), including a read of the real net-snmp agent.
Twenty deliberate breaks each caught. A browser run in both themes and at
phone width. `pytest` 817 passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — merge duplicate devices (2026-09-28)

### Added

- **Addresses** card on each device's page: its primary address, any extra
  addresses, and **Same device as this one** to merge a duplicate in. It is
  for a firewall with a gateway address on each VLAN (identified by IP on
  routed VLANs, so one device per address) and for multi-homed servers.
- A **preview page** says exactly what the merge will do before anything
  changes. Merging then:
  - makes the duplicate's addresses extra addresses of the kept device;
  - moves its targets, history included, and its services (a port both have
    is kept once, and its watcher follows the kept copy);
  - moves devices connected below it on the service map, and its SNMP
    polling if the kept device has none;
  - fills in only the name, MAC and role the kept device lacks;
  - deletes the duplicate, and its mute entry with it.
- **Sweeps respect it.** An address listed as a device's extra address is
  counted as that device (primary address unchanged), not a new device. If
  a device with its own MAC turns up at that address, the listing is dropped
  and the new device keeps it. **Remove** beside an address undoes a merge.
- The Devices list shows "+N" beside a device's address when it has extra
  ones.
- Refused: merging a device into itself, two devices with different MACs,
  or two that are both polled over SNMP.

### Changed

- Migration 10: the `device_address` table.

### Tests

16 new (`test_merge.py`). Eleven deliberate breaks each caught. A browser
run merges three gateway duplicates into one firewall. The migration-9 test
now checks against the current schema version. `pytest` 785 passed with the
agent, `smoke_test.py` 76 passed.

## Unreleased — service map (2026-09-27)

### Added

- **Service map** (`/map`, now live in the nav; "Map" on phones). Every
  device in its place — gateway, switches, what hangs off each — with an
  icon for its role, its status, and the services the port scan found on it.
  Devices not placed yet are listed in a **Not placed yet** group rather than
  left off. Below the tree, a searchable list of every service on the
  network, with Watch for those not watched yet.
- **On the service map** card on each device's page: **Role** (gateway,
  switch, access point, server, client) and **Connected to**. A device cannot
  be connected to itself or anything below it; the list leaves those out and
  the server refuses them. Save appears only once something changes.
- Status on the map: the worst of the device's targets, else its SNMP
  polling, else *not watched*. A watched service shows its target's status.

### Changed

- **Alerts follow the map.** A target whose device has anything above it
  down does not alert, and an SNMP device gone silent below a down device
  does not either; the incident is still recorded, flagged as explained by
  its dependency. Works alongside each target's own "depends on".
- `DeviceRole` gains `access_point`. The role and parent columns already
  existed (declared in the original schema), so there is no migration.
- The Save-when-changed script moved from Preferences into the base
  template, for any form marked `.save-when-changed`.
- `input[type=search]` takes the same style as other inputs.

### Tests

23 new (`test_service_map.py`); the form-field cap test now covers the
device page's routes too. Thirteen deliberate breaks each caught. A browser
run places a device, searches services, and checks the map and a five-link
phone nav at 320 and 390px. `pytest` 768 passed with the agent,
`smoke_test.py` 76 passed.

## Unreleased — SNMP alerts and the mute list (2026-09-27)

### Added

- **SNMP alerts**, from what polling already collects, sent through the
  existing Discord alerting (quiet hours, batching, retries included):
  - a **starred** port going down (on two polls in a row) and coming back;
  - a starred port over **80%** of its speed for **10 minutes**;
  - **CPU** or **memory** over **90%** for **10 minutes**;
  - **temperature** over **80 °C** for **5 minutes**.

  Each fires once and recovers once. The value must stay over the line on
  every poll for the whole time, clears only when back under by 5, and a gap
  in polling of more than three intervals restarts the count. Numbers and
  on/off per rule on a new **SNMP alerts** card under Settings → Alerts.
- **Starred ports.** A star beside each port on a device's page; only starred
  ports alert. A starred port that is down shows red there. Every starred
  port is listed on the SNMP alerts card, with Unstar.
- **The mute list**: one global list of devices and targets that never alert,
  on a new **Muted** card (add and remove there), plus **Mute alerts** on each
  device's page. Muting a device covers its targets, its SNMP polling and
  its ports. Muted things are still checked and shown.

### Changed

- Migration 9: `snmp_interface.starred`, and the `alert_mute` and
  `alert_state` tables.

### Tests

24 new (`test_snmp_alerts.py`) and 3 in `test_alerts.py` for the mute list on
targets and SNMP silence. Sixteen deliberate breaks each caught. A browser
run stars two ports, mutes a device and checks both pages on a phone.
`pytest` 744 passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — watch many devices at once (2026-09-26)

### Added

- **Watch selected** on the Devices page. Tick devices in the list (the box
  in the header ticks every one on the page) and a bar appears at the bottom
  of the window: **Watch N** gives each a ping check, exactly as its own
  Watch button would, and marks it reviewed. Devices already watched,
  ignored, or without an address have no box and are skipped if sent anyway.
- You stay on the Devices page, filters and page kept, with a line saying how
  many were added and a link to Targets. The first checks are queued a fifth
  of a second apart rather than run inside the request, so watching fifty
  devices answers at once and pings them over ten seconds, not in one burst.
- While anything is ticked, the page's live refresh waits, so a sweep
  finishing does not untick what you picked. Without JavaScript the bar is
  always shown and still works.

### Tests

14 new (`test_watch_selected.py`). Ten deliberate breaks each caught. A
browser run ticks, selects all, clears, watches 14 devices and sees the
Monitored tile go from 0 to 14, and checks the bar on a phone. `pytest` 717
passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — navigation on phones (2026-09-25)

### Fixed

- **Phones had no navigation.** Below 640px the page links were hidden with
  nothing in their place, so a phone could only reach other pages by typing
  the address. They now sit in their own row under the SPARK bar: Dashboard,
  Targets, Devices, Settings, sharing the width so all four fit without
  scrolling (checked at 320, 360, 390 and 412px). Anything wider than 640px
  is untouched: the top bar was measured element by element against the
  previous stylesheet at seven widths from 641 to 1400px, and is identical.

### Tests

2 new in `test_layout.py`: the phone rules keep the nav, and they come after
the tablet rules (both apply on a phone, so the later one wins). Both caught
a deliberate break. `pytest` 703 passed with the agent.

## Unreleased — input limits; Preferences saves only changes (2026-09-25)

An audit threw hostile input at every form field (572 attempts across 18
forms) and every id in a URL. SQL, markup, template and shell payloads were
all stored and shown as plain text, as intended. What it did find:

### Fixed

- **An id too large for SQLite** (over 2^63 − 1) in a URL or a form was a 500
  on 14 routes. Every id in a URL is now range-checked and answers **404**
  with a page; ids inside forms (`device_id`, `profile_id`,
  `depends_on_target_id`, the device page's `?port=`) are checked the same way.
- **`nan` as a target's timeout** got past the clamp and was a 500. `nan` and
  `inf` are now refused.
- **No length limits**: a 1 MB device name, target name or address was stored
  and shown on every page. Every form field now has a cap (`limits.py`),
  enforced by the server and matched by `maxlength` on the page, and any
  request body over **64 KB** is refused with 413 before a route reads it.
- **Malformed forms** (a missing field, a word where a number goes) got
  FastAPI's JSON error. They now get a page naming the field, with a link
  back to the page they came from (never off the site).
- A target name or address of only spaces was saved as blank; setup accepted
  a username of only spaces. Both are refused.
- **Signing in could bounce straight back to the sign-in page.** Setup, sign
  in and sign out left their database writes to `get_session`, whose commit
  FastAPI runs *after* the response is sent; the browser follows the redirect
  at once, so the next page could arrive before the new session row existed.
  Intermittent (about one sign-in in eight in a browser test), and with the
  sign-in timeout it read as "signed out after 30 minutes". They now commit
  before answering, as every other form route already did; a failed sign-in
  is counted toward the lockout at once for the same reason.

### Changed

- **Preferences**: each card's **Save** appears only when its setting differs
  from what is saved, and goes again if changed back. Without JavaScript it
  is always shown.

### Tests

45 new: 43 in `test_input_limits.py` (including one that fails if any future
form field has no length cap), one in `test_preferences.py`, and one in
`test_idle_timeout.py` that checks the session row exists at the moment each
sign-in response is sent. Fifteen deliberate breaks each caught. The fuzz run
was repeated after the fixes: no 5xx, no unescaped output. Browser runs check
the Save buttons (including the Back-button case and no JavaScript) and eight
sign-ins in a row with no bounce. `pytest` 701 passed with the agent,
`smoke_test.py` 76 passed.

## Unreleased — Find SNMP on the Devices page (2026-09-25)

### Added

- **Find SNMP** at the top of the Devices page, beside Scan ports: the same
  read-only search as Settings → SNMP, started from where the devices are.
  While it runs the page says so; when it finishes (a few seconds, no reload)
  each device that answered gets an **Add** button in its SNMP column, with
  the profile it answered, and a line at the top counts them, with **Show
  them** and **Add all**. A device that refused the credentials says
  `refused`, with the profiles it refused on hover.
- The SNMP filter gains **Answered Find, not polled**.
- Without a profile the button reads **Set up SNMP** and opens Settings → SNMP.
- Every button returns to the page as it was, filters and paging included,
  and only ever to `/devices`.

### Changed

- Starting a search and "add everything it found" moved into
  `snmp_discover` (`mark_started`, `add_all_found`), shared by both pages.

### Tests

14 new in `test_snmp_discover.py`: the button and its no-profile form;
searching is shown at once; return address confined to `/devices`; a device
that answered gets Add and nothing is added until it is pressed; Add lists it
with its profile and starts polling; a second Add is harmless; the filter;
Add all drops the emptied filter and keeps the rest; "nothing new" versus
"all added"; refused. Nine deliberate breaks each caught. A browser run
presses Find SNMP and sees the Add buttons arrive by live refresh, then adds
one. `pytest` 655 passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — sign-in timeout (2026-09-25)

### Added

- **Sign-in timeout** under Preferences: a session ends after 30 minutes
  without use, adjustable to 15 minutes, 1, 2, 4, 8 or 24 hours (no "never").
  Clicking, typing and opening pages count as use. The live refresh, the event
  stream and the timeout check send `X-Requested-With: fetch` and do not, so
  a dashboard left open still times out.
- An open tab goes to the sign-in page by itself when the session ends, with
  a note saying why, and signing in returns to the same page. The page's
  timer asks `GET /session` before acting, since another tab may have kept
  the session going. Typing or clicking sends `POST /session` at most once a
  minute, so filling in a long form counts as use.
- Raising the timeout deletes sessions that had already timed out, so none
  come back to life. `auth.session_days` (30 days) still caps every session.
- Proxy mode: no timeout of SPARK's own; the card says the proxy decides.

### Removed

- README: the "Upgrading an install from before the non-root container" block
  in Quick start. SPARK's startup error already prints the `chown` command,
  and "Docker settings that are not optional" covers it.

### Tests

22 new (`test_idle_timeout.py`): the timeout is enforced on pages and on the
event stream; page loads and `POST /session` count as use, background
refreshes and `GET /session` do not; changes apply at once; only the listed
choices are accepted; raising it revives nothing; proxy mode has none. Nine
deliberate breaks each caught. A browser run with a fake clock covers the
tab going to sign-in on its own, not doing so while another tab keeps the
session going, the once-a-minute click ping, and landing back on the same
page. `pytest` 641 passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — no routed-subnet banner (2026-09-25)

### Removed

- The dashboard banner "… are marked as routed rather than directly
  attached". Routed is a setting, not a fault, and the subnet table already
  marks each one "Routed · IP identity only".

### Tests

1 new (`test_dashboard.py`): a routed subnet raises no banner and is still
marked in the subnet table. Putting the banner back fails it. The smoke
test's routed-VLAN check now asserts the same. `pytest` 619 passed,
`smoke_test.py` 76 passed.

## Unreleased — time zone preference (2026-09-25)

### Added

- **Preferences** (`/preferences`), opened by clicking your name in the top
  bar. **Time zone** is the zone every time in SPARK is shown in: page
  timestamps (dashboard, targets, device pages, SNMP last test), chart axes and
  hover readouts, and the zone quiet hours are kept in. Offers the browser's
  own zone when it differs from the saved one.
- **Setup asks for it**, with the browser's zone preselected.
- A `local` template filter (`{{ when | local('%b %-d, %H:%M %Z') }}`), and
  a README convention: no `.strftime` on stored times in templates.

### Changed

- Quiet hours follow the Preferences zone. The Alerts card no longer captures
  the browser's zone on save; it names the zone and links to Preferences. An
  install that saved quiet hours before keeps that zone until a preference is
  set, so upgrading does not move anyone's quiet hours.
- Pages that said "UTC" after a time now show the zone's abbreviation
  (CDT, BST, …), since the time is no longer in UTC.
- The account chip in the top bar is a link to Preferences; Sign out is its
  own button beside it.

### Tests

12 new (`test_preferences.py`): setup saves the zone and refuses a bad one;
Preferences saves and refuses; the chip links there; the zone list has no
legacy aliases; page times, chart axes and quiet hours use the zone; the
alerts card names it; an older install keeps its quiet-hours zone. Six
deliberate breaks each caught. `pytest` 618 passed with the agent,
`smoke_test.py` 76 passed.

## Unreleased — no sideways jolt between pages (2026-09-25)

### Fixed

- **The layout jumped sideways when moving between some pages.** Where
  scrollbars take up space (Windows, Linux, macOS with a mouse attached), a
  page short enough to fit the window had no scrollbar and a longer one did,
  so the centred layout moved by half a scrollbar's width on every click
  between them — most noticeably Settings → Port scanning and SNMP on a tall
  window. The scrollbar's space is now always reserved (`scrollbar-gutter:
  stable`, with `overflow-y: scroll` for browsers without it). Reproduced in a
  headed Chromium at 1400×1150: the page header moved between x=64 and x=57;
  after the fix it is at 57 on every page, at 1150 and 800 px tall.

## Unreleased — one-click "public" SNMP profile (2026-09-25)

### Added

- **Add common defaults** on Settings → SNMP creates a v2c profile named
  `public` with the community `public`, the factory read-only default. Shown
  only while no v2c profile uses `public` — by name or by the community inside
  it — so it cannot be added twice, and refused with a message if posted
  anyway.
- `private` is deliberately not offered: it is conventionally the read-write
  community, SPARK only reads, and v2c sends it in clear text, which Find
  would do to every device on the network.

### Tests

3 new in `test_snmp_settings.py`; four deliberate breaks each caught.
`pytest` 604 passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — Settings split into pages (2026-09-25)

### Changed

- **Settings is four pages with a menu** instead of one long page:
  **Subnets** (`/settings`), **Port scanning** (`/settings/ports`), **SNMP**
  (`/settings/snmp`) and **Alerts** (`/settings/alerts`). The menu is a column
  beside the page on a wide screen and a wrapped row of tabs on a narrow one,
  and each entry carries one line of state — how many subnets, scanning on or
  off, how many devices SNMP polls, and whether alerts are on, off or have no
  webhook (the last in amber, since nothing can alert you).
- Every form returns to the page it was on, including with an error; only the
  open page's data is loaded.
- Old links to `/settings#snmp` and `/settings#alerts` are forwarded to the
  new pages. Links from the Devices list and device pages point at
  `/settings/snmp` directly.

### Tests

13 new (`test_settings_menu.py`): each page shows only its own card and marks
itself in the menu; unknown pages go to the first; each kind of form returns
to its own page; errors render on their own page; the menu's state hints; old
links are forwarded. Five deliberate breaks each caught. Existing tests now
load the page they test. `pytest` 601 passed with the agent, `smoke_test.py`
76 passed. No page is wider than the window at 1400, 900 or 390 px.

## Unreleased — alerting (increment 8, 2026-09-24)

SPARK now tells you, in Discord, when something goes down and when it comes
back. **Migration 8** runs by itself on start. **Rebuild the image**
(`docker compose up -d --build`): the Dockerfile gains `tzdata`, for quiet
hours in a named time zone. No new Python dependency.

### Added

- **Alerts** (`alerts.py`, Settings → Alerts): a target down and back up (with
  how long); an SNMP device silent for three polls and at least three minutes,
  and answering again; new devices found by a sweep, one message per sweep.
  Toggles for recovery, SNMP and new-device alerts, and for alerts as a whole.
- **Not sent, on purpose:** a failure explained by the target it depends on
  (the incident is still recorded, flagged, as before); a recovery whose
  outage was never alerted; degraded; SNMP silence on a device a target
  already reports down, or on one that has never answered; the first sweep's
  devices; anything for a target with `muted_until` in the future.
- **An outbox.** The decision is written in the same transaction as the change
  that caused it (`notification` table), and a job every 15 s sends what is
  due holding no database connection. A crash cannot lose an alert or send
  one for a change that rolled back; a slow Discord never delays a check.
  Each event has a dedupe key, so the same outage cannot be queued twice.
- **Delivery:** retries after 30 s, 2, 10 and 30 min, then failed; a 4xx other
  than 429 (a deleted webhook) fails at once; 429 `retry_after` is honoured
  for everything queued behind it; four or more due at once go as one message.
  `allowed_mentions` is empty, so a device named `@everyone` pings nobody.
- **Quiet hours** hold alerts and send one summary when the window ends. The
  window is kept in the IANA time zone of the browser that saved it.
- **Send a test** reports what Discord answered. **Recent** lists the last 15
  alerts and their fate (sent, held, pending with the error, failed, dropped).
- The dashboard warns when there is no webhook, and when alerts are off.

### Security

- **The Discord webhook is encrypted at rest** with the same vault as SNMP
  credentials, and never rendered back into a page. A webhook stored in
  plaintext by an earlier version is sealed on the first start after
  upgrading, and the plaintext removed. Closes the review item open since
  increment 2 (#9). Back up `secret.key` with the database — without it the
  webhook has to be pasted in again.
- Only `https://discord.com` (and `discordapp.com`, `ptb.`, `canary.`) webhook
  URLs are accepted, in Settings and at first-run setup. SPARK fetches this
  URL itself, so any other host would make the field a way to send requests
  into the LAN.

### Changed

- Migration 8 drops the old `notification` table, which nothing had ever
  written to, and creates the outbox in its place.
- README: alerting moved ahead of increments 4d, 5 and 7 on the roadmap.
- Tests and `smoke_test.py` can no longer reach Discord: `tests/conftest.py`
  replaces the HTTP client the dispatcher uses by default, because the app's
  dispatcher runs on a timer during any test that starts it and the test
  webhooks are shaped like real ones. The smoke test's setup webhook is now a
  valid-looking URL, since setup refuses anything else.

### Tests

53 new (`test_alerts.py`), with Discord faked by httpx's `MockTransport`:
webhook validation (ten hostile URLs), plaintext sealing at start; quiet hours
overnight, daytime, in a named zone, unset; targets through the real check
runner (threshold, flapping, a second outage, dependency, recovery without a
down, recovery off, muted, degraded); SNMP silence, never-answered, covered by
a target, switched off; new devices through the real sweep runner (first
sweep is a baseline); dispatch (sent once, backoff, give-up, 404, 429,
batching, no webhook, quiet hours then digest, and that nothing holds the
database while Discord answers); the Settings card and setup; migration 8.
Twenty-two deliberate breaks each caught. `pytest` 588 passed with the agent,
`smoke_test.py` 76 passed. End to end: a real target going down produced a
dispatched alert 9 s after the first failed check.

### Known, unfixed

- No per-target mute or maintenance window in the UI yet; `muted_until` is
  honoured but only settable in the database.
- Speed-test window suppression (DESIGN §1a) waits for the speed test itself.
- One webhook, one channel. No email, no second destination.

## Unreleased — a quieter Devices list (2026-09-24)

### Changed

- **MAC and Vendor are off the Devices list** and on each device's page
  (Details), next to the address. The list had grown to nine columns. The
  "random" MAC warning and the "IP identity" note moved with them, as
  "random MAC" and "IP identity" in the device page's header. The name field
  on the list is 11rem again, using some of the room freed; the table fits its
  card from 1100 px up.
- `smoke_test.py` checks the vendor on the device's page instead of the list.

## Unreleased — find SNMP devices, and see which are polled (2026-09-24)

SPARK polled only devices added by hand, and nothing on the Devices list said
which those were — finding out meant opening each device.

### Added

- **Find SNMP devices** (Settings → SNMP, `snmp_discover.py`). Tries every
  profile against every discovered device not already listed — one read-only
  GET of name, object id and description, 1.5 s timeout, no retry, 50 at a
  time — and lists the ones that answer with the profile that worked. **Add**
  and **Add all** put them on the list; Find itself never adds anything.
  Devices that refuse every profile (SNMPv3 authentication failures) are
  listed separately. Runs in the background and the results swap in through
  the page's existing event stream, no reload. Measured against a real agent:
  5 devices × 2 profiles in 4.1 s.
- It runs **only when pressed**. A v2c profile sends its community string in
  clear text to every device tried; that should be a choice, not a timer.
- **SNMP column on Devices** — `polling`, `no answer`, `paused` or `waiting`,
  each a link to the device's charts. Status colours only for the two real
  statuses (answering or not); paused and waiting are neutral.
- **SNMP filter on Devices** — all, polled, or not polled. Kept across pages
  and the live refresh, like the subnet filter.

### Changed

- The Devices table's name field is a set 9rem and the services column's floor
  9rem (was the browser's 20-character default and 11rem), so the table,
  with its new column, fits its card from 1280 px up instead of scrolling.

### Tests

14 new (`test_snmp_discover.py`): the column's wording; a stale "running" state
cannot disable the button forever; the first profile that answers wins and
the rest are not sent; listed and ignored devices are not tried; a crash is
recorded and clears "running"; Find needs a profile; results are offered, not
added, and Add all adds each with its own profile and skips devices ignored
since; the filter and paging links; and a real agent found over v2c and
reported as refusing a wrong v3 password. Eight deliberate breaks each caught.
`pytest` 534 passed with the agent, `smoke_test.py` 76 passed.

## Unreleased — narrow windows no longer scroll sideways (2026-09-24)

### Fixed

- **Tables widened the page.** On Devices, Targets and the dashboard, a table
  wider than the window ran on past its card while the card stayed the width
  of the window, and the whole page scrolled sideways. Devices was the worst —
  the Details button from stage 3 made its row wider still. Those tables now
  scroll inside their card, as the Settings tables already did.
  `tests/test_layout.py` fails if any template adds a table without the
  wrapper; it fails on the previous commit for all three pages.
- **The top bar widened the page between about 640 and 830 px.** Five links,
  the user's name and role and Sign out did not fit, so every page scrolled
  sideways in that range. Below 62rem the name and role are hidden (the avatar
  stays), the not-yet-built Service map link is hidden, spacing tightens, and
  if the links still do not fit they scroll within the bar.
- Measured on every page at 1400, 1100, 900, 830, 760, 700, 641 and 390 px:
  the page is never wider than the window.

### Known, unfixed

- Below 640 px the nav links are hidden altogether (unchanged; the brand links
  to the dashboard). A phone menu is a design change, not a fix.

## Unreleased — SNMP, stage 3: device pages and charts (2026-09-24)

Every device now has a page at `/devices/<id>`, linked as **Details** on the
Devices list and from each device's name on the SNMP card. No migration and no
new dependency.

### Added

- **Health charts** — CPU (or load average where there is no percentage),
  memory, and the hottest temperature sensor, over 1h, 24h, 7d or 30d. A
  chart with nothing to show is left out rather than drawn empty.
- **Interfaces** — status, current rate, peak in the range, errors, and a
  sparkline per port; choose a port to chart its traffic, the busiest charted
  first. Only `up` is coloured: an empty port reads `down` to SNMP, and a page
  of red for unplugged ports would be a page of false alarms.
- **Reading history across tables** (`snmp_history.py`). Each range has a
  fixed bucket width (1 min, 5 min, 30 min, 2 h: 60-360 points), and every
  bucket combines raw samples, five-minute and hourly rollups weighted by
  sample count, peaks by maximum. The 7-day line where raw turns into rollups
  does not show on a 30-day chart. Unanswered stretches are gaps, not zeros,
  and the page reports what share of polls were answered.
- **Charts drawn by SPARK** (`charts.py`): SVG stretched to its box with
  non-scaling strokes, labels in HTML so they never stretch, gridlines at
  quarters of a scale rounded so each quarter is readable. No chart library,
  no CDN, nothing the CSP needs to be loosened for. `static/charts.js` shows
  times in the browser's time zone and draws the hover readout; with it
  blocked, charts still render and read in UTC.
- Not-polled devices get the same page with sweep details, open services and
  a pointer to the SNMP card.

### Changed

- The static asset cache key covers `charts.js` as well as `app.css`, so a
  changed script is not served stale.
- Links in running text (card and page descriptions, muted notes) are accent
  instead of the browser's default blue and visited purple. There was no link
  colour at all before.
- README conventions: no inline styles (the CSP drops them silently), and
  charts use cyan for the first series and grey for the second — status colours
  never appear on a chart.

### Tests

45 new (`test_device_page.py`): window alignment; weighted combination of raw
and rolled-up history, including in the same bucket; gaps kept as gaps; the
window start and other devices' data excluded; per-interface traffic, errors
and peaks; scale, gap-breaking paths, clamping, formatting, sparkline
thinning; the page itself (busiest port first, choosing a port, empty ports
neutral, the range picker, no inline style attributes, not-polled devices,
unknown ids, links in); and the asset key following `charts.js`. Thirteen
deliberate breaks each caught. `pytest` 510 passed with the agent,
`smoke_test.py` 76 passed. Rendered against a real net-snmp agent polled over
v3 authPriv, and against 30 days of synthetic history in both themes and at
390 px.

### Known, unfixed

- **Big switches are slowest on long ranges.** Measured with 48 ports and 30
  days of history: 1h 17 ms, 24h 0.23 s, 7d 0.9 s, 30d 1.1 s for the page's
  queries. Fine for a homelab; a pre-aggregated series table would be the fix
  if it ever is not.
- The page does not refresh itself; reload for newer polls.
- pysnmp 7.1 imports AES CFB from a `cryptography` module path that warns it
  will move. Works on the pinned `cryptography` 50.0.1; an unpinned upgrade
  could break SNMPv3 privacy until pysnmp follows.

## Unreleased — SNMP, stage 2: scheduled polling and history (2026-09-24)

Devices on the SNMP list are now polled on a schedule and the answers kept.
**Migration 7** adds six tables; it runs by itself on start. No new
dependency, so `docker compose up -d --build` is only needed because the code
changed; there is nothing to chown this time.

### Added

- **Polling** (`snmp_poll.py`), every 60 seconds by default, set in
  Settings → SNMP (30 s to 1 h). Each poll records uptime, CPU % or load
  average, memory %, the hottest temperature sensor, and every interface's
  status, speed and counters. Per-device **Pause**/**Resume**. A device added
  to the list is polled within seconds; jobs whose interval has not changed
  are left alone when the list is saved, so editing one device does not push
  every other device's next poll back.
- **Traffic as rates**, bits per second per interval, stored only for
  interfaces that are up. 32-bit wraps are added back once and the result is
  discarded if it is faster than the link (two wraps look like one); a 64-bit
  counter going backwards, a reboot (`sysUpTime` went backwards, or grew by
  less than the time that passed), a change of counter width, or a gap of more
  than three intervals each give a new baseline and no rate. Each rule has a
  test worked out by hand, and each test was checked by breaking the rule and
  watching it fail.
- **History on the check ladder**: raw for 7 days, 5-minute average *and
  peak* for 90, hourly for 730, same settings and the same nightly job.
  Health averages are weighted by answered polls, traffic averages by sample
  count, so a quiet or silent stretch does not drag an average toward zero.
- **The card** shows each device's latest poll — `polling` with the numbers,
  `no answer` with the reason, or `paused` — and when the next one is due.
- `snmp_poll` rows cascade from `snmp_device`: removing a device from the SNMP
  list deletes its history. Pause keeps it.
- A `Counter64` column type. SNMP counters run to 2^64 − 1 and SQLite integers
  stop at 2^63 − 1; the top half is stored in two's complement and read back
  exact, instead of raising `OverflowError` on the agents that start their
  counters high.

### Fixed

- **Retention lost samples every night.** The raw cutoff was "now minus seven
  days" to the second, so it fell inside a five-minute bucket. That night folded
  the part before it; the next night's fold of the rest collided with the
  existing bucket, `INSERT OR IGNORE` dropped it, and the delete then removed
  the raw rows. Up to one bucket's worth per target per night, silently —
  reproduced as 14,397 of 14,400 samples surviving two nights. Cutoffs are now
  aligned to the bucket width. Applies to check history as well as SNMP.
- **A missing counter was recorded as 0.** `collect_interfaces` filled any
  counter that did not come back with zero, so the next good reading looked
  like the whole counter arrived in one interval. Now `None`.
- **Counter widths could be mixed.** An interface was flagged 64-bit if
  *either* direction answered from the 64-bit table, so a 64-bit "in" could be
  paired with a 32-bit "out". Now both directions or neither.
- **The Settings tables made the whole page scroll sideways on a phone.** They
  now scroll inside their card.
- `test_an_undecryptable_credential_says_what_to_do` failed whenever the SNMP
  agent was running: the key is now cached per process (hardening review),
  so rewriting `secret.key` mid-test changed nothing. It skipped in the
  review's run, which had no agent. The test now clears the cache, which is
  what a restore onto another install actually replaces.

### Corrected

- **net-snmp does serve CPU %.** Code review item 6 (increment 2) concluded
  modern net-snmp answers neither `hrProcessorLoad` nor `ssCpuIdle`, and
  planned a raw-tick fallback for this stage. Re-measured: both are one-minute
  averages, empty for the first minute or so after `snmpd` starts (measured:
  still empty at 60 s, answering at 75 s) and normal after that. The review's agent had just started. The
  fallback would have covered only that first minute, so it was not built;
  the README, `oids.py` and the collector comments now say what is true.

### Tests

42 new: counter arithmetic (wrap, reset, ceiling, width, gap, reboot),
recording (first poll, rates, a missed poll bridged, reboot, status changes,
vanished interfaces, 2^64 − 1 round trip, cascade), scheduling, SNMP
retention, the collector's counters with faked walks, migration 7 against a
fresh install, the card's controls, a live poll of the net-snmp agent, and
retention across two consecutive nights.
Fifteen deliberate breaks of the new rules were each caught. `pytest` 465
passed with the agent (447 + 18 skipped without), `smoke_test.py` 76 passed.

### Known, unfixed

- **ifIndex renumbering.** Interfaces are keyed by ifIndex. A few cheap
  switches renumber on reboot; the names follow on the next poll, but history
  under an index briefly belongs to a different port. Keying by name breaks on
  the more common devices whose names are not unique.
- **32-bit-only devices** can't be measured above about 570 Mbps at 60 s
  polling. Shorter intervals raise that ceiling.
- **Reverse DNS logs `InvalidStateError` under uvloop.** Found while testing
  this stage, not caused by it: `discovery/sweep.py` wraps
  `loop.getnameinfo` in `wait_for`, and when the timeout cancels it uvloop
  logs an unhandled exception from its callback. Harmless to the sweep, noisy
  in the log. Production uses uvloop.
- No charts or device page yet — stage 3.

## Unreleased — security and hardening review (2026-09-24)

A full pass over the application, its dependencies and its container, and the
fixes for what it found. `pip-audit` against `requirements.lock`: no known
advisories in the 39-package closure. `bandit`: one real finding (below), two
false positives (the retention SQL interpolates a constant, not input).

**This release needs a rebuild and one command on the host.** The container no
longer runs as root, so the data directory has to belong to the new user:

```bash
cd spark
sudo chown -R 9700:9700 data
docker compose up -d --build
```

SPARK refuses to start, and prints exactly that command, if the directory is
not writable. Nothing else about the deployment changes.

### Security

- **The container runs as uid 9700, not root.** It holds a map of the network,
  every credential it has been given, and a raw socket on the LAN; a bug in it
  or in any package under it should not also be uid 0 in the VM's network
  namespace. Real ICMP still works: `CAP_NET_RAW` is attached to the Python
  binary as a file capability, which the kernel grants at exec to an
  unprivileged user as long as the capability is in the container's bounding
  set. Verified with a raw ICMP socket opened as uid 65534. The compose file
  now drops every other capability (`cap_drop: [ALL]`) and mounts
  `./config` read-only. **Do not add `no-new-privileges`**: it tells the
  kernel to ignore file capabilities, and ping would silently degrade.

- **Security headers on every response** (`web/hardening.py`, a raw ASGI
  middleware so the `/events` stream is untouched). A `Content-Security-Policy`
  that permits only SPARK's own origin, with a per-request nonce for the four
  inline scripts — no `unsafe-inline` — plus `frame-ancestors 'none'`,
  `form-action 'self'`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, and
  `Cache-Control: no-store` on HTML so a page full of the network's inventory
  does not sit in a shared machine's back button. The stylesheet and fonts
  stay cacheable; they are versioned by content hash.

- **Cross-site writes are refused.** Every POST is checked against `Origin`
  (or `Referer`) and answered 403 if it names another host. `SameSite=Lax`
  already stopped a cross-site form post carrying the cookie; this is the
  second, independent layer. Requests with neither header — curl, the test
  client — are allowed, because a browser carrying a cookie across sites
  always sends one. Behind a reverse proxy, the `Host` header must be
  preserved (proxies do this by default).

- **The OpenAPI document and Swagger page are gone.** `/openapi.json` and
  `/api/docs` were served to anyone without a session: every route and every
  form field, on the box that holds the map. There is no API to document.

- **Password login is refused in proxy mode.** `POST /login` still verified
  passwords when `auth.mode: proxy`, and minted a cookie nothing would read —
  so the admin password could be guessed from behind the proxy on an instance
  that never asks for it.

- **Rate limiting could be bypassed with a header in proxy mode.**
  `client_ip()` used the *first* `X-Forwarded-For` entry, which is the one the
  client writes; a proxy appends its own observation last. Now the last entry.

- **The session cookie is `Secure` when the login arrived over TLS.** It was
  never `Secure`, so a TLS-fronted install sent its session over any `http://`
  link. On plain HTTP nothing changes: forcing it there would mean the cookie
  is silently never sent. The scheme is uvicorn's view of the request —
  direct, or from `X-Forwarded-Proto` when the proxy is one it trusts.

- Two committed zip archives (`spark-increment-2.zip`,
  `_to_delete_spark-increment-1.zip`) are untracked; `*.zip` is ignored at the
  repository root as it already was under `spark/`.

- `bandit`'s one real hit: the CSS cache-buster's MD5 is marked
  `usedforsecurity=False`, so a FIPS-mode Python does not refuse to start.

- **The base image is pinned by digest**, as the Dockerfile's comment and
  CLAUDE.md always said it was. `python:3.12-slim@sha256:2f17fc04…`, taken
  from `docker inspect` on the VM on 2026-09-24. Rebuilds now reproduce the
  same base until someone changes that line on purpose.

### Fixed

- **A bad target form was a 500, not a 400.** An unknown `check_type` raised
  out of the route; a dependency on a target that did not exist failed at
  commit; a target could be made to depend on itself, so its every failure was
  a symptom of its own failure and never alerted. All three are now messages
  on the form. Tuning values are clamped to what the form offers
  (interval 5 s–24 h, timeout 0.5–60 s, thresholds 1–100): a `0` timeout
  fails every probe and a 3600 s one holds a scheduler slot for an hour.

- **"Check now" could record `database is locked`.** The route awaited the
  check inside its own request transaction, which had a pending write
  whenever the session's last-seen time was stale — and SQLite has one
  writer. The transaction is now committed before the check runs, the same
  fix the scan buttons got earlier.

- **Ping `count` and `interval` are bounded** (1–20 and 0.05–5 s). They come
  from the free-text params box, and `"count": 500` was a check that never
  finished.

### Performance

- Resolving a session is one query (a join) rather than two, on every
  authenticated request.
- `secret.key` is read once per process rather than on every credential the
  vault seals or opens.

### Tests

- `tests/test_hardening.py`, 27 tests, each written to fail against the code
  before this pass. Mutation-checked: removing the middleware fails five.
- Suite: 407 passed (was 380). `smoke_test.py`: 76.

### Known, unfixed

- `app.host: 0.0.0.0` with host networking binds every interface. The VM has
  one NIC, so there is nothing else to bind to today; set `SPARK__APP__HOST`
  if that changes.
- Two people completing first-run setup in the same instant could each
  create an admin. The window is one transaction on a fresh install.
- The Discord webhook URL is still stored in plaintext (alerting is last on
  the roadmap; it should go through `vault.py` when it lands).
- The CSP allows only SPARK's origin, so any future third-party script or
  stylesheet needs a nonce or the policy needs a source; `hardening.py` is
  the one place to change.

## Unreleased — SNMP, stage 1: credentials and Test (2026-09-24)

The SNMP collector has existed since increment 2 and nothing called it. This is
the first stage of wiring it in: somewhere to keep credentials, and a way to
find out what each device will answer before anything is polled.

### Fixed — SNMPv3 with encryption never worked

pysnmp needs the `cryptography` package for AES and DES privacy, and it was not
a dependency. pysnmp copes with that by quietly flagging encryption as
unavailable instead of failing at import, so every **authPriv** request — the
one v3 mode worth using — died locally with "Ciphering services not
available". Nothing noticed because nothing tested it: the live agent only
spoke v2c.

Verified against a real net-snmp agent rather than inferred: authPriv SHA/AES
fails as shipped, and works with `cryptography` installed. It is now declared,
and the lock gains exactly one line (`cryptography==50.0.1`; hash-checked
wheels confirmed for x86_64 and arm64 on the image's glibc).

`cryptography` has scheduled the removal of a cipher mode pysnmp calls, so a
future lock upgrade could break this again just as silently.
`tests/test_snmp_crypto.py` drives pysnmp's own AES path with no agent needed,
on every run; uninstalling `cryptography` fails it.

### Fixed — every SNMP failure was reported as a timeout

A device that was off, a wrong v3 password, and SPARK being unable to encrypt
all surfaced as `TimeoutError`, so they looked identical and needed three
different fixes. The collector already declared `Unreachable` and `AuthFailed`
and never raised them. Failures are now classified by pysnmp's error *type* —
its message text overlaps, "Ciphering services not available" meaning either a
missing library or a wrong key — into `AuthFailed`, `Unreachable` or the new
`CipherUnavailable`. Where the difference cannot be seen from outside (an agent
silently drops a request it cannot authenticate), the message says credentials
are a suspect rather than pretending to know.

### Added

- **Settings → SNMP.** Credential profiles (v2c, v3 authNoPriv, v3 authPriv),
  devices assigned to a profile, and a **Test** button that runs the capability
  probe and records what the device supports. It replaces the `spark-probe`
  step for anyone who would rather not open a shell.
- **Encrypted credential storage** (`vault.py`): Fernet under a key derived
  with HKDF from `secret.key`. Checked by reading the raw bytes of the SQLite
  file and its WAL — no secret appears in them. Secrets are never rendered back
  into a page, including after a validation error, and the password fields ask
  the browser not to autofill the SPARK login into them.
- Migration 6 creates `snmp_profile` and `snmp_device` from model metadata.
- `.pill.neutral` for information that is not a status; a security level is
  shown neutral, since v2c is not *degraded*.
- Disabled buttons now look disabled. There was no rule for it.

### Also fixed

- **`secret.key` was briefly world-readable when first created**: written with
  the default mode, then chmod-ed. Now created 0600 with `O_EXCL`. Harmless
  while nothing used the key; not once it guards credentials.
- A failed profile edit could leave the profile half-changed. Validation now
  finishes before anything is assigned.

### Tests

- 61 new: `test_snmp_crypto.py`, `test_vault.py`, `test_snmp_settings.py`.
  `tests/local_agent.sh` now also answers v3 (authPriv and authNoPriv users),
  so the six live SNMP tests that always skipped here now run.
- Mutation-checked — nine deliberate breakages, all caught. Two tests needed
  rewriting before they could fail: the key-file permission test checked only
  the final mode, which the old write-then-chmod code also reached, so it
  passed against the bug it was written for; and the secrets-in-the-page test
  was confirmed to fail only once *both* layers of protection were removed.
- Suite: 396 passed, **0 skipped** (was 329 passed, 6 skipped). `smoke_test.py`: 76.

### Not yet

Nothing is polled on a schedule and nothing is stored beyond the Test result.
That is stage 2; the device page is stage 3.

## Unreleased — status tiles only colour when something is wrong (2026-09-23)

"Let colour mean status" made the Degraded, Down and Open incidents icon tiles
amber and red — unconditionally. So a healthy network showed a permanent red
icon above a neutral **0**: the tile and the number disagreed about whether
anything was wrong. A red that is always there is a red you learn to stop
seeing, which is the one habit a monitoring dashboard cannot afford to teach.

### Fixed

- The three status tiles now take their colour on the same condition as their
  number. At zero they are cyan like every other tile; they turn amber or red
  only when there is something to look at.

### Tests

- `tests/test_dashboard.py`, 7 tests. Checked against the bug rather than
  assumed: with the old unconditional tiles restored, five fail. One of those
  had to be rewritten first — `test_tile_and_number_agree` originally tried
  only non-zero states, where tile and number agreed even with the bug in
  place, so it passed against the exact code it was written to catch. The
  disagreement lived at zero, and the test now checks a healthy network too.
- The README's statement of the tile rule is updated to match.
- Suite: 329 passed, 6 skipped. `smoke_test.py`: 76 passed.

## Unreleased — design document catches up (2026-09-23)

- **DESIGN.md's stack table said HTMX + Alpine.js + Tailwind over WebSocket.**
  None of those is used: the frontend is Jinja templates, one hand-written
  stylesheet and small inline scripts, with live updates over Server-Sent
  Events. The row now says so, and notes what was originally planned.
- DESIGN.md gains a **Visual identity** section recording the brand decisions
  (name, mark, colour rules, theme, type, licence). Bumped to v0.4.

## Unreleased — licensed (2026-09-23)

### Added

- **SPARK is licensed under the PolyForm Noncommercial License 1.0.0.** Free
  for personal, homelab, hobby and educational use; commercial use needs a
  separate license. `LICENSE.md` carries the official text unmodified, with a
  `Required Notice:` copyright line above it that anyone passing SPARK on must
  keep.
- There are two identical copies: `LICENSE.md` at the repo root, where GitHub
  looks, and `spark/LICENSE.md`, inside the Docker build context. The
  Dockerfile copies the second beside `pyproject.toml` so hatchling puts it
  in the wheel's `dist-info/licenses/`. Change both together.
- `pyproject.toml` declares `license = "PolyForm-Noncommercial-1.0.0"` (an
  SPDX identifier) and the author. Metadata only: no dependency change, and
  the lock files are untouched.
- The footer shows the copyright and licence in place of the tagline.

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
  are standing on — `172.16.10.7/24` — is accepted and stored as `172.16.10.0/24`.

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
