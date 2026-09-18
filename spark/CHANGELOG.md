# Changelog

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
