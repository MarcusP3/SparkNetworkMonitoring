# Changelog

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
