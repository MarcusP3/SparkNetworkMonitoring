# Changelog

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
