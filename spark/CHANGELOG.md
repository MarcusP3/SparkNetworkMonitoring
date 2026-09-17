# Changelog

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
