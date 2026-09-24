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
- **Checks never raise.** They return a `CheckOutcome` carrying a reason. A
  poller that throws when the thing it polls is broken has failed at its job.
  The state machine interprets sequences of outcomes; individual checks do not.
- **Migrations** are a numbered list in `db.py`, not Alembic. Append only —
  never edit or reorder an entry that has shipped. The runner has not yet
  executed a real migration, so the first one needs care and a test.
- **Secrets** are files or env vars. The database stores references, not secret
  material. (The Discord webhook URL currently violates this; see CHANGELOG.)
- **No new dependency without a reason that survives the question "what does
  this do that the standard library does not".** Every package in the closure
  is code that runs in a container holding `NET_RAW` on someone's
  home network. The closure is 39 packages; keep it that way.

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

## Deployment reality

Runs on a dedicated Ubuntu VM with Docker, host networking and `NET_RAW` — not
Docker Desktop, whose host networking is layer 4 only and cannot do the ARP and
ICMP discovery depends on. The container runs as uid 9700 with `CAP_NET_RAW`
as a file capability on the Python binary; `./data` must be owned by 9700 and
the compose file must never gain `no-new-privileges` (it disables file
capabilities). `docker compose up -d --build` is needed after any
dependency change; a plain `up -d` reuses the existing image.
