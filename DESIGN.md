# SPARK — Network Monitoring

*Design document · v0.4 · September 2026*

---

## 1. Purpose

**An Auvik-class network monitoring service for a homelab: one always-on container on a dedicated VM, one web dashboard reachable from any machine on the LAN.**

The gap this fills: homelab tooling forces a choice between *uptime checkers* (Uptime Kuma — pretty, dumb about networks) and *enterprise NMS* (LibreNMS, Zabbix, Observium — capable but heavy, ugly, and built for a NOC with a full-time operator). Auvik's actual value is that it **discovers the network itself** and presents it as a coherent picture rather than a list of hosts you had to type in. Nothing self-hosted does that at homelab scale with a modern UI.

**Non-goals:** multi-tenancy, MSP billing, remote-site VPN tunnels, enterprise config backup, log aggregation. Not competing with Grafana for dashboards or Wazuh for security.

**Definition of success (v1):** you stop SSH-ing into things to answer "is it up?", you can see every application on your network and what port it's on without grepping a compose file, and you find out about an outage from a Discord message rather than from a family member.

**Conventions:** binary/container `spark` · database `spark.db` · default web port `9700` · config `spark.yaml`.

---

## 2. Environment

| Component | Detail | Why it matters |
|---|---|---|
| Router / firewall | Dedicated (pfSense/OPNsense/UniFi-class) | DHCP leases, ARP table, WAN state, NetFlow export. Root of the tree. |
| Managed switch(es) | SNMP-capable | MAC/port tables, LLDP neighbors, per-port counters — raw material for auto-topology |
| Docker host(s) | **Separate box(es) from the monitor** | Container inventory must be collected over the network, not a local socket |
| Bare-metal services | systemd units on various hosts | Found by port scan; not enumerable via any API |
| **SPARK VM** | Dedicated Linux VM, Docker | Must sit on a VLAN with reachability to everything it polls |

---

## 3. Feature phases

Every phase ships something usable on its own. Do not build phase N+1 before N is running in production.

### Phase 1 — Health, Alerting & Service Map *(v1 scope)*

#### 1a. Check engine & alerting

- **Check types**, each on an independent schedule:
  - ICMP ping (up/down, RTT, packet loss)
  - TCP port open
  - HTTP(S) — status code, response time, optional body-content match, **TLS cert expiry countdown**
  - DNS resolution (catches the classic "internet is fine, DNS is dead")
  - Docker container state (over the remote transports in §4)
- **State machine with hysteresis** — `UP → DEGRADED → DOWN`, requiring N consecutive failures before firing. This single detail is the difference between a useful tool and one you mute within a week.
- **Alerting via Discord webhook**, on state *transitions* only:
  - Recovery notifications ("back up after 4m 12s")
  - **Dependency suppression** — switch goes down, you get one alert, not thirty
  - Quiet hours / maintenance windows
- **History** — raw for 7 days, 5-minute rollups for 90 days, hourly for 2 years.

#### 1b. Internet health & speed testing

- **WAN reachability** against multiple external anchors, so one dead target isn't a false alarm.
- **Throughput tests** via the official Ookla `speedtest` CLI, bundled in the image. Default schedule `0 3 * * *` (3 a.m. daily), editable as a cron expression in the UI, plus a manual **Run now** button.
- **Optional iperf3 mode** against a server you control, for testing internal links rather than the WAN.
- **Test-window suppression.** A running speed test saturates the WAN and *will* spike every latency check on the box. SPARK marks the test window, suppresses WAN latency alerts during it, and flags affected samples so they don't pollute the trend charts. Without this, a nightly speed test means a nightly false alarm.
- Retained history so you can screenshot it at your ISP.

#### 1c. Service map

The tree view, the reason this isn't Uptime Kuma:

```
🌐 Internet          WAN up · 892↓/41↑ · 47ms
└─ 🛡️ opnsense       192.168.1.1     ● up
   ├─ 🔀 sw-core     192.168.1.2     ● up · 11/24 ports active
   │  ├─ 🖥️ docker-01    192.168.1.10   ● up
   │  │  ├─ 📦 home-assistant  :8123    ● 200 OK · 12ms
   │  │  ├─ 📦 plex            :32400   ● 200 OK · 31ms
   │  │  └─ 📦 postgres        :5432    ● tcp open
   │  └─ 💾 truenas      192.168.1.20   ● up
   └─ 🔀 sw-office   192.168.1.3     ● up
```

**Key architectural point: the tree and the eventual graph map are the same data, rendered differently.** Building the tree now is not throwaway work — Phase 3 adds a second renderer over the same tables.

Also available as a **flat filterable list** — "show me every HTTP service", "what's listening on 8080 anywhere", "everything on docker-01" — which in practice you'll use more than the tree.

**How the hierarchy is built (v1): declared.** Each device carries a `role` (`gateway` / `switch` / `host`) and an optional `parent_device_id`, set once in the UI. For ~5 pieces of infrastructure this is a five-minute setup and is 100% accurate. Phase 3 replaces it with automatic SNMP-derived parenting.

**How devices are found:** ICMP + ARP sweep across configured CIDRs, MAC OUI vendor lookup, reverse DNS. Devices are keyed on MAC and get a user-assigned friendly name that survives IP changes.

**How applications are found**, in descending order of trustworthiness:

1. **Docker API** over the remote transports in §4 — container name, image, published port bindings, health status, restart count. Authoritative, zero guessing, and it covers most of a homelab.
2. **Curated TCP port scan** — ~200 common service ports per host on a schedule, async connect scan. Catches the bare-metal systemd services no API knows about.
3. **Port → application name table**, built in (8123 → Home Assistant, 32400 → Plex, 8006 → Proxmox, 3000 → Grafana, 8096 → Jellyfin, 81 → Nginx Proxy Manager…). Open ports display as names, not numbers, with zero configuration.
4. **HTTP fingerprint** — request `/` on open web ports and read the `<title>` and `Server:` header to confirm the guess and produce a good label.

**Promotion loop:** any discovered service is one click from becoming a monitored target, and it automatically inherits dependency suppression from its host and switch. This is what ties 1c back into 1a.

### Phase 2 — Inventory depth & change tracking

- **New device alerts** — "unknown device joined the network" (doubles as your security feature).
- DHCP lease import from the firewall; mDNS/Bonjour names; richer OS fingerprinting.
- Full change history: what appeared, vanished, changed IP, or started/stopped listening on a port.
- Service change alerts — a container that used to be there isn't, or a new port opened on a host.

### Phase 3 — SNMP & true topology

The Auvik signature. Only possible because you have managed switches.

- **SNMP v2c/v3 collection** — interface table, bridge MAC-address table, LLDP/CDP neighbors, ARP table.
- **Automatic parenting** — the bridge table says which switch port each MAC lives on, so hosts place themselves under the correct switch *and port* with no manual declaration. LLDP resolves switch↔switch and switch↔firewall uplinks.
- **Graph map** — auto-laid-out topology diagram, colored by live health, over the same data as the Phase 1 tree.
- **Port faceplate view** — per-switch: what's plugged into each port, link speed, error counters, PoE draw.

### Phase 4 — Traffic

- NetFlow/sFlow/IPFIX collector, or SNMP interface counters as the poor-man's version.
- Per-device bandwidth over time; top talkers; external destinations with ASN/geo enrichment.
- Anomaly alerts (a device suddenly uploading 40 GB at 3 a.m.).

### Phase 5 — Nice-to-haves

Switch config backup with diffs · syslog/trap receiver · PoE control and port bounce from the UI · Prometheus `/metrics` export · mobile PWA · public status page.

---

## 4. Architecture

```
                    ┌──────────────────────────────────────┐
                    │  SPARK VM · Docker (host networking) │
                    │                                      │
  ┌──────────┐      │  ┌────────────┐    ┌───────────────┐ │
  │ Browser  │◄────►│  │  FastAPI   │◄──►│  Scheduler    │ │
  │ (LAN)    │  WS  │  │  + auth    │    │ (APScheduler) │ │
  └──────────┘      │  └─────┬──────┘    └───────┬───────┘ │
                    │        │                   │         │
                    │        │      ┌────────────┼────────┐│
                    │        │      │  Check     │ Disco  ││
                    │        │      │  workers   │ workers││
                    │        │      └────────────┼────────┘│
                    │  ┌─────▼───────────────────▼──────┐  │
                    │  │   SQLite (WAL) · config + TS   │  │
                    │  └────────────────┬───────────────┘  │
                    │            ┌──────▼──────┐           │
                    │            │  Notifier   │──────────────► Discord
                    │            └─────────────┘           │
                    └──────────────────────────────────────┘
                          │            │              │
                   ssh:// to      ICMP / TCP     speedtest CLI
                   Docker hosts   across LAN      → WAN
```

**Single-process design on purpose.** No Redis, no Celery, no Postgres, no orchestration. One container, one SQLite file to back up. Complexity is the enemy of a homelab tool you actually maintain.

### Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12+ | Best network/SNMP library ecosystem; you'll extend this yourself |
| API | FastAPI + Uvicorn | Async-native, free OpenAPI docs, WebSocket built in |
| Scheduling | APScheduler | In-process, persistent job store, no broker |
| Checks & scanning | `asyncio`, `httpx`, `icmplib`, `dnspython` | Non-blocking; hundreds of targets plus a port sweep on one core |
| Docker | `docker` SDK for Python | Container inventory over SSH, socket-proxy, or TLS |
| Speed test | Ookla `speedtest` CLI | Bundled in image, license pre-accepted |
| Auth | `argon2-cffi` + signed session cookie | See below |
| SNMP *(Ph. 3)* | `pysnmp` | v3 support, pure Python |
| Storage | SQLite in WAL mode | Handles this write volume easily; revisit only if proven necessary |
| Frontend | Jinja templates, one hand-written stylesheet, small inline scripts | No build step, no npm, no separate SPA deploy, nothing from a CDN. Live updates over Server-Sent Events (`/events`). *(Planned as HTMX + Alpine + Tailwind over WebSocket; the plain version turned out to need none of them.)* |
| Graph map *(Ph. 3)* | Cytoscape.js | Only heavyweight JS dependency, loaded on one page |
| Packaging | Docker image + compose file | |

**Deliberate rejections:** React/Next (build toolchain overhead for a single-user LAN tool), Postgres (nothing here needs it), Prometheus+Grafana as the backend (great stack, but then you've built a config generator, not an app), microservices (no).

### Visual identity — decided

- **Name:** SPARK, all caps, wherever a person reads it; `spark` for the package, CLI, container and config file.
- **Mark:** a flat-top lightning bolt, drawn as inline SVG (`templates/_brand.html`) and repeated in the favicon set under `static/brand/`. Never reused inside the app to mean something else.
- **Colour:** electric cyan is the brand and the interactive colour (`#22d3ee` dark, `#0e7490` light) and is never a status. Green, amber and red mean up, degraded and down, and appear only on statuses; unknown and paused are grey. There is no blue "info" state.
- **Theme:** follows the OS light/dark setting, with dark as the tuned default.
- **Type:** Inter for text, JetBrains Mono for data (addresses, MACs, OIDs, latency), both self-hosted. Nothing loads from the internet: SPARK has to work on a LAN with no internet access.
- **Licence:** PolyForm Noncommercial 1.0.0, © Marcus Pierce. See `LICENSE.md`.

### Authentication — decided

**Single admin account with a password, set on first run.** Argon2id hash, HTTP-only `SameSite=Lax` session cookie, rate-limited login endpoint, sessions revocable from the UI.

Rationale: SPARK will hold a complete map of your network, an inventory of every service you run, and credentials that reach your Docker hosts. That makes it the single highest-value target on the LAN. "It's internal, skip the login" is exactly the assumption that turns one compromised IoT device into a full network map for whoever owns it. One password is proportionate; OIDC is not.

**Also supported: trusted-header mode.** If you later front SPARK with Authelia, Tailscale, or Cloudflare Access, set `auth.mode: proxy` and SPARK trusts a `Remote-User` header — *but only from an explicitly allowlisted proxy IP*, because a trusted-header setup with no source restriction can be spoofed by anything on the LAN. Building this in now means no rework when you decide to expose it.

### Reaching the Docker hosts — decided

The monitor is a separate VM, so container inventory comes over the network. Three transports, in recommended order:

1. **SSH — `ssh://user@dockerhost`** *(default)*. The Docker SDK supports this natively. Uses SSH keys you already have, opens no new listening port, and needs no certificates. Best fit for a homelab.
2. **Socket proxy** — run `docker-socket-proxy` on each Docker host, exposing a filtered **read-only** subset of the API (containers + info endpoints only). Most privilege-minimal option; SPARK cannot start, stop, or exec anything even if compromised.
3. **TLS TCP on 2376** with client certificates. Standard, most setup effort.

**Never plain TCP 2375.** That is an unauthenticated remote root shell on the host, full stop.

Secrets (SSH keys, client certs, Discord webhook URL) are mounted as files or passed as env vars; the database stores only references, never the secret material. **SNMP credentials are the exception, and deliberately:** a community string or v3 key has to be recovered to be sent, and they are entered in the UI, so they are stored *encrypted* — Fernet, under a key derived with HKDF from `secret.key` in the data directory (`vault.py`). That protects a copied or backed-up database, not a compromised VM, since SPARK must be able to read the key. The Discord webhook is treated the same way since increment 8: entered in the UI, sealed with the same vault, and moved out of plaintext automatically on the first start after upgrading.

### Deployment notes

Three settings are non-optional or discovery silently returns nothing:

- **`network_mode: host`** — required for ARP/ICMP sweeps to see the LAN. Bridge networking puts the scanner behind NAT and it will find zero devices.
- **`cap_add: [NET_RAW]`** — required for real ICMP. Without it, ping falls back to TCP probes and latency numbers get less meaningful.
- **Persistent volume** for `spark.db`, so history survives upgrades.

---

## 5. Data model (initial sketch)

- **`device`** — `id`, `mac` *(natural key)*, `friendly_name`, `vendor`, `role` (`gateway`/`switch`/`host`/`client`), `parent_device_id`, `first_seen`, `last_seen`, `notes`
- **`interface`** — device NICs/ports; `ip`, `speed`, `admin_state`, `oper_state`
- **`service`** — `id`, `device_id`, `port`, `protocol`, `name`, `source` (`docker`/`scan`/`manual`), `image`, `container_id`, `state`, `first_seen`, `last_seen`, `target_id?`
- **`docker_host`** — `id`, `name`, `transport` (`ssh`/`proxy`/`tls`), `uri`, `secret_ref`, `enabled`, `last_ok_at`, `last_error`
- **`target`** — a thing being checked: `device_id?`, `service_id?`, `type`, `address`, `params` (JSON), `interval`, `enabled`, `depends_on_target_id`
- **`check_result`** — `target_id`, `ts`, `status`, `latency_ms`, `detail` — the hot table; indexed on `(target_id, ts)`, downsampled nightly
- **`incident`** — `target_id`, `opened_at`, `closed_at`, `severity`, `cause`, `acknowledged_by`
- **`speedtest_result`** — `ts`, `down_mbps`, `up_mbps`, `latency_ms`, `jitter_ms`, `server`, `method`
- **`notification`** — outbound log; prevents duplicate sends, gives an audit trail
- **`user_session`** — `token_hash`, `created_at`, `last_seen_at`, `user_agent`, `revoked`
- **`link`** *(Ph. 3)* — `a_device`, `a_port`, `b_device`, `b_port`, `source` (`lldp`/`mac`/`declared`), `confidence`

Three decisions worth flagging now, because they're expensive to retrofit:

1. **MAC as device identity.** DHCP reassigns IPs; a tool keyed on IP silently loses a device's history the first time your lease table churns.
2. **Incidents as first-class rows.** Deriving "was it down, and for how long?" from raw results at query time gets painful fast. Open and close incident records in the state machine instead.
3. **`service` separate from `target`.** A service is a fact about the network, discovered whether or not you care about it. A target is a decision to watch something. Conflating them means you either monitor everything you find (noise) or lose the inventory of what you chose not to watch.

---

## 6. Resolved decisions

| Question | Decision |
|---|---|
| Name | **SPARK** |
| Deployment | Docker container, host networking, on a dedicated VM |
| Auth | Single admin password (Argon2id + session cookie), optional trusted-header proxy mode |
| Speed test | Ookla official CLI, daily at 3 a.m. by default, cron-editable in UI, manual run button, alert suppression during test window |
| Docker access | SSH transport by default; socket-proxy and TLS also supported; never plain 2375 |
| v1 scope | Health + alerting + service map (declared hierarchy, Docker inventory, port scan) |

---

## 7. Next step

Build **Phase 1**: project scaffold, config schema, auth, check engine (ping/TCP/HTTP/DNS), state machine with hysteresis, Discord notifier, SQLite persistence and migrations, subnet sweep, remote Docker inventory, curated port scan, speed-test runner, and the tree + list UI over live status.
