# Working on SPARK

Homelab network monitoring. The application lives in `spark/`, not the repo
root — every `docker compose` and `pytest` command runs from `spark/`.

Design document: `DESIGN.md`. Current state and conventions: `README.md` (repo
root — GitHub only renders a README from there). History: `spark/CHANGELOG.md`.

---

## Documentation is part of the change, not a follow-up

**Any change to behaviour, configuration, dependencies or project layout updates
the docs in the same commit.** A README that describes what the code was
supposed to do is worse than no README, because it is believed.

On every change, check each of these and update the ones the change touched:

| Changed | Update |
|---|---|
| A feature became usable | README **Status** table, **Roadmap** table |
| A check type, param, or default | README **Monitoring** section |
| Config keys or env overrides | README **Configuration** section |
| A new dependency | `pyproject.toml` **and `requirements.lock`** (see below), and note in CHANGELOG that a rebuild is required |
| New module or directory | README **Project layout** |
| A convention future code must follow | README **Conventions** |
| Anything at all | `CHANGELOG.md` under the current increment |

The README **Status** table is the honest account of what works. Never mark
something ✅ that has not been exercised end to end. "Not yet" is a perfectly
good entry and has been for most of this project's life.

If a change deliberately leaves something broken, unfinished, or decided
against, say so in the CHANGELOG's *Known, unfixed* section rather than letting
it be rediscovered later.

---

## Dependencies are hash-pinned

`requirements.lock` and `requirements-build.lock` pin every package, direct and
transitive, to a version and a set of SHA-256 hashes. The Docker build installs
from them with `--require-hashes` and never re-resolves. Adding a dependency to
`pyproject.toml` alone is therefore **not enough** — it will work in your venv
and fail in the image.

```bash
cd spark
uv pip compile pyproject.toml --generate-hashes --python-version 3.12 -o requirements.lock
```

`tests/test_supply_chain.py` fails if the two files disagree, so this cannot be
forgotten silently. Commit the regenerated lock in the same commit as the
dependency.

Two things that look like details and are not:

- The build toolchain is locked separately in `requirements-build.lock` and
  installed with `--no-build-isolation`. Without it, `pip install .` downloads
  hatchling unverified at build time and the runtime lock guards a door with no
  wall around it.
- The base image is pinned by digest, not tag. Re-pin it deliberately; that is
  the point. `docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim`
  after a `docker pull` gives the current one.

---

## Conventions that are load-bearing

- **Timestamps** use the `UTCDateTime` column type, never `DateTime(timezone=True)`.
  SQLite drops the offset, so the latter returns naive datetimes and the first
  `utcnow() - stored` raises `TypeError`.
- **Enums** use `enum_column()` and subclass `enum.StrEnum`. Plain `String`
  columns return bare strings, and `(str, Enum)` members format as
  `HealthStatus.DOWN` instead of `down`.
- **SNMP counters** use the `Counter64` column type, not `Integer`: they run to
  2^64 − 1 and SQLite integers stop at 2^63 − 1.
- **Retention cutoffs are aligned to bucket width.** Any new downsampled series
  folds with the aligned cutoffs from `run_retention`, or the bucket straddling
  the cutoff loses its later half the following night.
- **Alerts are decided in the transaction that makes the change** -- call
  `alerts.enqueue` (or an `on_*` helper) from the same session, never send
  from a check or a request. The dispatcher sends; nothing else posts to
  Discord except the Settings test button.
- **Checks never raise.** They return a `CheckOutcome` carrying a reason. A
  poller that throws when the thing it polls is broken has failed at its job.
  The state machine interprets sequences of outcomes; individual checks do not.
- **Migrations** are a numbered list in `db.py`, not Alembic. Append only —
  never edit or reorder an entry that has shipped. The runner has not yet
  executed a real migration, so the first one needs care and a test.
- **Secrets** are files or env vars, or -- when they are entered in the UI and
  must be recovered to be used (SNMP credentials, the Discord webhook) --
  sealed with `vault.py` before they touch the database. Never plaintext in a
  table or a setting, and never rendered back into a page.
- **Client addresses** are settled once, in `web/hardening.py`, from
  `auth.proxy.trusted_proxies` (`proxies.py`). Routes read `request.client`;
  anything deciding *trust* (proxy mode's identity header) reads
  `request.state.peer`, the TCP peer. Never read `X-Forwarded-*` anywhere
  else, and never turn uvicorn's `proxy_headers` back on. Anything about
  addresses needs a test under a real uvicorn (`tests/test_live_server.py`);
  the test client cannot see uvicorn's middleware.
- **Argon2 runs in a thread** (`auth._hash_in_thread`), never on the event
  loop: a login attempt is 120 ms and 64 MiB, and the loop is also every
  check and poll.
- **Never construct `SnmpEngine()` in a poll.** `SnmpCollector` takes its
  engine from `collectors.snmp.engine_for` -- one per credential, for the
  life of the loop -- and `close()` lets go of the target only. Building an
  engine compiles thirteen MIB modules (70-80 ms, ~5 MB) and doing it per
  poll was a third of each poll's CPU and the reason memory ratcheted up
  after every burst of overlapping polls. A new scheduled job that uses
  SNMP goes through `SnmpCollector`; the lifespan closes the engines.
- **Shutdown is bounded** (`main.GRACEFUL_SHUTDOWN_SECONDS`, 3 s): over
  HTTPS an idle browser connection would otherwise hold uvicorn for 30 s
  and Docker kills at 10 s. Anything that starts uvicorn -- `run`, the
  live-server test fixtures -- passes `main.server_arguments(config)`, so
  the tests run the server the way the container does. Keep the compose
  file's `stop_grace_period` above the constant.
- **Logs are for the operator.** APScheduler's per-job INFO lines are off
  in `configure_logging`; a new scheduled job must not log its own
  "running" line either. Anything at WARNING or above should be something
  a person would act on.
- **HTTPS is the default** (`app.tls: auto`, `tls.py`). Anything that reads
  the session cookie goes through `auth.session_token` (two names: plain and
  `__Host-`); anything that sets it goes through `routes_auth._set_session_cookie`.
  HSTS only with an operator's certificate, never the self-signed one.
- **`config/spark.yaml` is not tracked.** The shipped file is
  `config/spark.example.yaml`; `test_no_homelab_details.py` scans it. Tests
  build `Config` objects directly and never depend on either file.
- **Sign-in events** (`alerts.on_security_event`) are queued in the
  transaction that recorded the event, like every other alert.
- **No new dependency without a reason that survives the question "what does
  this do that the standard library does not".** Every package in the closure
  is code that runs in a container holding `NET_RAW` on someone's
  home network. The closure is 37 packages; keep it that way.

## Testing

```bash
cd spark
pip install -e ".[dev]"
pytest -q              # must pass before committing
python smoke_test.py   # end-to-end over real HTTP; must pass before committing
```

Both suites must be green. When adding behaviour, add tests that would fail
without it — particularly for the state machine, where a hysteresis off-by-one
does not crash, it just pages you for a dropped packet or stays silent through a
real outage.

Python 3.12+ is required. Some environments only have 3.10; build and test
somewhere with 3.12 rather than lowering `requires-python`.

## Docs and process files

`SECURITY.md` and `.github/` are part of the repository's surface: a change
to how a vulnerability should be reported, or to what CI runs, updates them
and the README's *Continuous integration* section together.

## Deployment reality

Runs on a dedicated Ubuntu VM with Docker, host networking and `NET_RAW` — not
Docker Desktop, whose host networking is layer 4 only and cannot do the ARP and
ICMP discovery depends on. The container runs as uid 9700 with `CAP_NET_RAW`
as a file capability on the Python binary; `./data` must be owned by 9700 and
the compose file must not gain `no-new-privileges` while that capability is
on the binary (an effective file capability the kernel cannot grant makes
`execve` fail). The documented alternative — `SETCAP_NET_RAW=0`, no
`cap_add`, `ping_group_range` on the host — is the only way to have both.
`docker compose up -d --build` is needed after any dependency change; a plain
`up -d` reuses the existing image. It serves HTTPS on 9700 with a self-signed
certificate unless `app.tls` says otherwise. The container has a 512 MB
memory limit, set from measurement (README, *What it costs to run*): the
floor is ~100 MB of Python and libraries, a real network sits at ~140 MB,
and a login peaks 64 MB above steady per hash. A change that moves those
numbers moves the limit and the README together.
