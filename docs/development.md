# Development

Running the tests, CI, the code layout and conventions, what is built, what is next, and the decisions behind it.

## Contents

- [Status](#status)
- [Development](#development)
- [Roadmap](#roadmap)
- [Design decisions](#design-decisions)

---

## Status

SPARK is built in increments, and this table is the honest account of what each
one has actually delivered. Anything marked *not yet* does nothing at all today.

| Area | State |
|---|---|
| Configuration, database, authentication, web shell | ✅ working |
| Check engine — ping, TCP, HTTP(S), DNS | ✅ working |
| Live-updating pages (server-sent events) | ✅ working |
| Device discovery — ICMP/ARP sweep, MAC identity, vendor lookup | ✅ working |
| Device inventory — naming, review state, watch one or many in one click | ✅ working |
| Scan schedule — on/off and interval, set on the Devices page | ✅ working |
| Subnet management and VLAN tags (`/settings`) | ✅ working |
| Subnet filter on the Devices page | ✅ working |
| Service discovery — TCP port scan, watch a service in one click | ✅ working |
| History retention — nightly downsample and prune | ✅ working |
| Hysteresis, incident tracking, dependency suppression | ✅ working |
| Target management UI (`/targets`) | ✅ working |
| SNMP collection — profiles, Test, scheduled polling, history, device pages with charts | ✅ working |
| Alerting (Discord) — down/recovered, SNMP silence, new devices, quiet hours | ✅ working |
| Docker inventory — container lists via a read-only socket proxy | ❌ not yet |
| Network map and Services — declared tree, services list, alerts follow it | ✅ working (topology from SNMP not yet) |
| Device APIs — TrueNAS (drives, alerts), Proxmox (guests, storage, SMART) and UniFi (devices, clients) | ✅ working |
| Backup and restore — nightly, encrypted download, `spark-restore` | ✅ working |

SPARK tells you when something goes down, in Discord, and when it comes back —
see [Alerts](guide.md#alerts).

---

## Development

### Running tests

```bash
pip install -e ".[dev]"

pytest -q                      # unit and integration suite
python smoke_test.py           # end-to-end walk through the running app

./tests/local_agent.sh start   # optional: a local net-snmp agent
pytest -q                      #   the live collector tests then run instead of skipping
./tests/local_agent.sh stop
```

`pytest` is the developer suite. `smoke_test.py` is a standalone "did my install
work" script that needs no test framework and exercises setup, login, rate
limiting, target CRUD and the check engine over real HTTP.

### Running without Docker

For development (discovery needs real layer-2 access, so this is for the UI
and checks, not sweeps):

```bash
pip install -e .
SPARK_CONFIG=./config/spark.yaml SPARK__APP__DATA_DIR=./data SPARK__APP__TLS=off spark
```

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request, and every
Monday on its own: the two test suites, installed from the hash-pinned locks
the way the image is; `pip-audit` over both locks and `bandit` over `src`; and
a build of the image scanned by Trivy, then started with the healthcheck
polled over TLS. The Monday run is the point — a vulnerability published
after the last commit still fails a run and sends the mail. Dependabot
proposes base-image digests and action versions; Python packages it leaves
alone, because the lock is generated (`uv pip compile … --generate-hashes`)
and a hand-edited one fails `test_supply_chain.py`. For the base image it
proposes only new `3.12-slim` digests, not a new Python: moving to 3.13 or
3.14 is a deliberate change to the locks, the tests and CI together.

Every action in the workflow is pinned to a full commit SHA, with the
version in a comment (Dependabot updates both). Tags can be moved: in March
2026 attackers rewrote nearly all of `aquasecurity/trivy-action`'s version
tags to steal CI secrets, and the old tags were later deleted. Pin any new
action the same way.

Two things only the repository owner can switch on, and should: two-factor
authentication on the GitHub account (a hardware key), and branch
protection on `main` (no force-push, no deletion). Everyone who runs SPARK
does `git pull` and rebuilds; the account is the supply chain.

### Project layout

Everything below is relative to the repository root. The application is in
`spark/`; the top holds only the documents (`docs/`), the security policy and CI.

```
README.md         what SPARK is; deploy, upgrade, back up, restore
DESIGN.md         the design document
CLAUDE.md         working agreements for this repo
docs/
  guide.md          using SPARK, page by page
  configuration.md  spark.yaml, TLS, Docker settings, cost, sign-in, upgrading
  development.md    this file: status, tests, CI, layout, conventions, roadmap, design decisions
SECURITY.md       how to report a vulnerability privately, and what is in scope
.github/
  workflows/ci.yml   pytest + smoke test, pip-audit + bandit, image build + Trivy; weekly on a schedule
  dependabot.yml     proposes base-image digests and action versions (not pip: the lock is uv's)
spark/
  docker-compose.yml, Dockerfile
  requirements.lock     every package pinned to a version and SHA-256 hashes
  requirements-build.lock  the build toolchain, pinned the same way
  config/spark.example.yaml  the pre-database config template; copy to spark.yaml (git-ignored)
  CHANGELOG.md
  smoke_test.py         end-to-end walk through the running application
  src/spark/
    config.py           YAML + env config loading
    models.py           full Phase 1 schema, UTC datetime and enum column types
    db.py               engine, sessions, migration runner
    auth.py             Argon2 passwords (hashed off the event loop), sessions, setup code, proxy mode, sign-in events
    proxies.py          which peers are trusted proxies and what their X-Forwarded-* headers say
    tls.py              the self-signed certificate: made once, fingerprint in the log
    cli.py              spark-probe, spark-reset-password, spark-restore
    backup.py           backups: the snapshot, nightly job, encryption, restore checks
    main.py             app factory and entry point
    scheduler.py        APScheduler jobs, reconciled against the database
    events.py           in-process pub/sub for live page updates
    retention.py        nightly downsample and prune; never VACUUMs
    subnets.py          subnet CRUD, validation, and the one-shot YAML seed
    snmp_config.py      SNMP credential profiles, devices, and Test
    snmp_poll.py        scheduled SNMP polling; counters to rates, wraps and resets
    snmp_history.py     SNMP history as chart-sized series, across raw and rollups
    snmp_discover.py    Find SNMP devices: try the profiles, suggest what answers
    prefs.py            preferences: time zone (the `local` filter's formatting), sign-in timeout
    limits.py           how long any input may be, the largest request, the largest id
    alerts.py           deciding what to alert on; the mute list; the Discord outbox and dispatcher
    snmp_alerts.py      SNMP threshold rules: starred ports down or busy, CPU, memory, temperature
    storage.py          TrueNAS pools and drives, filesystems anywhere; read every 5 minutes, and their alerts
    truenas.py          the TrueNAS API client: JSON-RPC over wss only, certificate pinning, login
    credentials.py      API credentials (Settings → Credentials): add, edit, Test, Trust
    suppressions.py     one alert rule, for one device, off or with its own line
    truenas_health.py   drive health and TrueNAS's alerts over the API: the alerts, the Storage card
    proxmox.py          the Proxmox API client: an API token over pinned HTTPS, GET only
    proxmox_health.py   Proxmox guests, storage and drives: the alerts, the Proxmox card
    unifi.py            the UniFi Network Integration API: an API key over pinned HTTPS, GET only; the UniFi card
    servicemap.py       the network map: the tree, each device's status, the services list
    hierarchy.py        a device's place: parents, loops refused, "is anything above it down"
    merge.py            merging a duplicate device into the real one; what moves, what is refused
    identity.py         SNMP own addresses and ARP tables: merge suggestions, MACs across routers
    topology.py         the map from SNMP MAC tables and LLDP: where each device is plugged in
    charts.py           server-rendered SVG charts; no chart library
    vault.py            encryption for stored credentials (key from secret.key)
    port_catalogue.py   which ports the scan looks at, and what they cost
    discovery/
      sweep.py          ICMP sweep, ARP table, reverse DNS
      oui.py            MAC prefix to vendor
      ports.py          the default port list and the TCP connect scan
      services.py       recording what a scan found, without losing history
      store.py          the device identity rules
    checks/
      base.py           CheckSpec and CheckOutcome; no database access
      net.py            ping, tcp, http, dns
    engine/
      state.py          hysteresis and the incident lifecycle
      runner.py         joins a check to the database, owns its session
    collectors/
      base.py           collector Protocol and the normalised result shapes
      oids.py           numeric OID catalogue and capability probes
      snmp.py           the SNMP collector
    web/                routes and dependencies
      routes_device_page.py  the per-device page
      routes_credentials.py  Settings → Credentials
      routes_backup.py    Settings → Backup: the encrypted download
      hardening.py      security headers, CSP nonces, same-origin check on writes, proxy headers from trusted proxies only, Host allowlist, HSTS
    templates/          Jinja templates
    static/             hand-written CSS, no build step
      charts.js         local times and hover readouts on charts
      fonts/            Inter + JetBrains Mono, self-hosted (OFL-1.1)
      brand/            favicon (SVG + ICO) and Apple touch icon
  tests/
    test_engine.py      hysteresis, incidents, dependency suppression, the checks
    test_events.py      what live updates publish, and what they stay quiet about
    test_discovery.py   device identity across DHCP churn, ARP parsing, OUI
    test_retention.py   downsampling keeps outages; weighted averages; idempotence
    test_schedule.py    when the first sweep is due; the scan schedule form
    test_subnets.py     CIDR/VLAN validation, membership, seeding, migration 3
    test_targets_page.py  what the targets list says about failures, and when
    test_supply_chain.py  the lock matches pyproject; everything pinned and hashed
    test_incidents.py   one open incident per target, across pause/resume
    test_alert_incidents.py  incidents for alert rules: opened on firing, closed on clearing, on the dashboard
    test_suppressions.py  per-device rule overrides: quiet, their own lines, afresh when saved; the page
    test_ports.py       the scanner against real sockets; the service store
    test_paging.py      one device per page, exactly once, whatever the filter
    test_port_catalogue.py  the editable port list, and what it costs to scan
    test_snmp.py        pure-function tests, live tests that skip without an agent
    test_snmp_crypto.py SNMPv3 privacy actually works; failures are told apart
    test_snmp_settings.py  profiles, devices, Test; secrets absent from DB and pages
    test_snmp_poll.py   counter wraps and resets, recording, scheduling, SNMP history
    test_device_page.py history read back weighted and gap-true; charts; the page
    test_layout.py      every table scrolls inside its card
    test_snmp_discover.py  Find suggests and never adds, from Settings or Devices; the SNMP column and filter
    test_alerts.py      what is sent and what is not; retries, rate limits, quiet hours
    test_snmp_alerts.py  thresholds held for their time, no flapping, starred ports, the mute list
    test_storage.py     TrueNAS and hrStorage parsing (from a real 25.10 box), storage alerts, the Storage card
    test_credentials.py  the TrueNAS client against a TLS fake: nothing sent before Trust, pins, renewals; the page
    test_truenas_health.py  pool topology, disks and alerts in the shapes a real 25.10 box returns; drive and TrueNAS alerts
    test_proxmox.py     the Proxmox client against a TLS fake: nothing sent before Trust; parsing; guest, storage and SMART alerts; the card
    test_unifi.py       the UniFi client against a TLS fake: nothing sent before Trust; paging; 9.x and 10.x answers read alike; the card
    test_service_map.py  the tree, statuses, search, placing devices, alerts quiet below a down device
    test_merge.py       merging duplicates; sweeps afterwards count the address as the kept device
    test_identity.py    SNMP address and ARP parsing, what is suggested and what never is, MACs filled in
    test_topology.py    the map from MAC tables and LLDP across network shapes; accept, dismiss, never overwrite
    test_preferences.py the time zone: set at setup and in Preferences, used on pages, charts, quiet hours
    test_idle_timeout.py  sign-in timeout: enforced, not extended by background requests, tab sent to sign-in
    test_vault.py       credential encryption, key derivation, the key file
    test_hardening.py   headers, CSP nonces, cross-site POSTs, proxy headers and proxy mode, form bounds
    test_live_server.py  under a real uvicorn: loopback cannot pick its address, same-host proxies work, one admin however many race
    test_setup_code.py  the setup code: logged not shown, required, rate-limited, spent; one administrator enforced by the database
    test_account.py     change password, sessions listed, sign out everywhere else, spark-reset-password
    test_backup.py      backup while running, encryption edges and tampering, restore end to end, refusals, the page
    test_http_check.py  the HTTP check against a local server: 1 MB body cap, endless bodies cut
    test_data_privacy.py  data/ files are 0600, an older database is made private at start
    test_tls.py         the self-signed certificate, app.tls spellings, and HTTPS under a real uvicorn: __Host- cookie, no HSTS, healthcheck
    test_security_events.py  what sign-in events queue an alert, and the toggle that silences them
    test_no_homelab_details.py  no real host names or address scheme in anything shipped
    test_input_limits.py  impossible ids, oversized fields and bodies, nan, blank names; injection stays inert
    test_watch_selected.py  tick devices and watch them all; skips, duplicates, junk, first checks queued
    local_agent.sh      throwaway net-snmp agent on 127.0.0.1:11161, v2c and v3
```

No npm, no bundler, no Alembic. Clone it and read it top to bottom.

### Conventions

Ten things that will bite you if you don't know them:

- **Timestamps** use the `UTCDateTime` column type, not `DateTime(timezone=True)`.
  SQLite has no offset, so the latter silently returns naive datetimes and the
  first `utcnow() - stored` raises `TypeError`. Storage format is unchanged.
- **Enums** use `enum_column()` and are `enum.StrEnum`, so values store
  lowercase, read back as members, and format as `down` rather than
  `HealthStatus.DOWN` in an alert message.
- **Checks never raise.** They return a `CheckOutcome` with a reason. The state
  machine decides what a sequence of outcomes means; the check does not.
- **Migrations are a numbered list in `db.py`**, not Alembic. Append; never edit
  or reorder an entry that has shipped.
- **SNMP counters** use the `Counter64` column type. They run to 2^64 − 1 and a
  plain `Integer` raises `OverflowError` above 2^63 − 1.
- **Every table sits in `<div class="table-scroll">`.** Without it, a table
  wider than the window widens the whole page instead of scrolling inside its
  card. `tests/test_layout.py` checks every template.
- **Show times with `| local`**, never `.strftime` in a template. Stored
  times are UTC; the filter shows them in the zone chosen under Preferences.
- **No inline styles.** The CSP allows styles from `'self'` only, so a
  `style="…"` attribute is silently ignored. Position things with classes (the
  chart labels sit at fixed quarters for exactly this reason); a script may set
  `element.style`, which the policy allows.
- **Retention cutoffs are aligned to the bucket width** (`retention._floor`).
  A new downsampled series must use the aligned cutoffs, or a bucket straddling
  the cutoff loses its later half the next night, silently.
- **Cyan is the brand, never a status.** `--accent` is for SPARK itself and for
  things you can click. Up, degraded and down are green, amber and red; unknown
  and paused are grey. The reverse holds too: status colours appear only on
  statuses, and never on charts: the first series on a chart is cyan, the
  second neutral grey. Icon tiles are cyan unless what they count is actually happening:
  the Degraded, Down and Open incidents tiles turn amber or red only when their
  number is above zero, on the same condition as the number itself. A red that
  is always there is a red you learn to stop seeing. No blue "info" state — it
  read as a fourth status beside
  the cyan. Nothing loads from a CDN, fonts included: SPARK runs on LANs with no
  internet. The mark is the flat-top bolt in `templates/_brand.html`; its path
  is duplicated in `static/brand/favicon.svg`, so change both together.


---

## Roadmap

| # | Increment | Status |
|---|---|---|
| 1 | Foundation — config, schema, auth, dashboard shell | ✅ done |
| 2 | SNMP collection engine + capability probe | ✅ done |
| 3 | Check engine — ping, TCP, HTTP, DNS, hysteresis, incidents, targets UI | ✅ done |
| 4 | Device discovery — sweep, MAC identity, devices page | ✅ done |
| 4c | History retention, scan schedule, subnet management + filter | ✅ done |
| 4b | Service discovery — TCP port scan, services on devices | ✅ done |
| 4d | Docker inventory — read-only socket proxy | next |
| 5 | Network map and Services — tree and filterable list views | ✅ done (Docker containers join it with 4d) |
| 6 | SNMP — credentials, Test, polling, history, device pages | ✅ done |
| 7 | UniFi Network API collector (console CPU/temp, uplink topology) | planned |
| 8 | Alerting — Discord, dependency suppression, quiet hours | ✅ done (ahead of 4d, 5, 7) |

Reordered twice. After increment 2 the check engine moved ahead of SNMP
storage, because a monitor that cannot tell you anything is down is not yet a
monitor. After increment 3, alerting moved to last: the inventory and map work
is the substance, and the live-updating pages cover "is anything wrong" well
enough to watch while the rest is built.

Then reordered back: once SNMP polling and device history existed there was
enough watching going on that "only while someone is looking at the page" had
become the biggest gap, so alerting (8) was built before 4d, 5 and 7.

---

## Design decisions

Four choices that are expensive to change later, written down so they don't get
"simplified" away.

**Devices are keyed on MAC, not IP.** DHCP reassigns addresses. A tool keyed on
IP silently loses a device's entire history the first time a lease churns. This
is why `attached` per subnet is not cosmetic: ARP does not cross a router, so a
routed VLAN yields no MAC and falls back to the weaker identity.

**Incidents are rows, not a query.** Deriving "was it down, and for how long"
from raw check results at read time gets painful fast, and it is the question
you ask most.

**`service` and `target` are separate tables.** A service is a fact about the
network, discovered whether or not you care. A target is a decision to watch
something. Merge them and you either monitor everything you find (noise) or lose
the inventory of what you chose to ignore.

**Live updates push, they do not poll.** `/events` holds one connection per
open tab and the check runner publishes on state transitions only. Polling
costs a request per tab per interval forever and is still late; publishing on
every check would refetch the page once per check per tab, which turns a
monitor into load on the thing being monitored.

**History is downsampled, never deleted outright.** Raw results for 7 days,
5-minute buckets for 90, hourly for 2 years, with per-status counts kept at
every width. Retention never VACUUMs: freed SQLite pages are reused by new
inserts, so the file plateaus instead of being rewritten nightly.

**Hysteresis is not optional.** A target needs N consecutive failures before it
changes state. This single detail is the difference between a tool you trust and
one you mute within a week.

See `DESIGN.md` for the full design document and `spark/CHANGELOG.md` for what
changed when.
