# Working on SPARK

Homelab network monitoring. The application lives in `spark/`, not the repo
root — every `docker compose` and `pytest` command runs from `spark/`.

Design document: `DESIGN.md`. Current state and conventions: `spark/README.md`.
History: `spark/CHANGELOG.md`.

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
| A new dependency | `pyproject.toml`, and note in CHANGELOG that a rebuild is required |
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
ICMP discovery depends on. `docker compose up -d --build` is needed after any
dependency change; a plain `up -d` reuses the existing image.
