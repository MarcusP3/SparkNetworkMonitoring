# Configuration and security

What goes in `config/spark.yaml`, the Docker settings SPARK depends on, what it costs to run, and how sign-in and the web interface are protected.

## Contents

- [First start](#first-start)
- [Configuration](#configuration)
- [Authentication](#authentication)
- [Upgrading](#upgrading)

---

## First start

The [README](../README.md#deploy) has the commands; this is why each step is
there.

**Create `data/` yourself, before the first `up`.** If Docker creates it for
the bind mount it belongs to root, and SPARK refuses to start (with the
`chown` command in the message) rather than run as root.

**The certificate warning.** SPARK made itself a certificate on first start,
and nobody has vouched for it. Before accepting it, compare the fingerprint
the browser shows with the one SPARK logged — the same way SPARK asks you to
check TrueNAS's and Proxmox's.

**The setup code.** The account form asks for a code SPARK printed in its
log when it started with no account. Only whoever can read that log can
create the account, so the first person to find the port on the network
cannot. The code changes whenever SPARK restarts and is spent once the
account exists. The password minimum is 12 characters.

**Docker Desktop for Mac and Windows will not work** for discovery. Its host
networking operates at layer 4, so ARP (layer 2) and real ICMP (layer 3)
never reach your LAN. Checks over TCP, HTTP and DNS work fine there, which
makes it usable for development but not for deployment.

---

## Configuration

Two layers, deliberately:

| Where | What lives there |
|---|---|
| `config/spark.yaml` (your copy of `config/spark.example.yaml`; git ignores it) | Things needed *before the database exists*: bind address, data directory, TLS, auth mode, the names SPARK answers as. Its `network.subnets` block seeds the database once and is then ignored |
| Web UI | Everything you would change routinely: targets, subnets and VLAN tags, the scan schedule, SNMP profiles and the polling interval, the Discord webhook and alert settings, and later retention |

Any YAML value can be overridden by environment variable, nesting with double
underscores: `SPARK__APP__PORT=9800`, `SPARK__AUTH__MODE=proxy`,
`SPARK__APP__TLS=off`. A container whose `SPARK_CONFIG` names a file that
does not exist stops and says so, rather than running on defaults.

### TLS

SPARK serves HTTPS by itself. `app.tls` is one of:

| Value | What it means |
|---|---|
| `auto` (default) | A self-signed certificate SPARK makes on first start, kept in `data/tls/`, its SHA-256 fingerprint in the log. Browsers warn once; you compare the fingerprint and accept. It is the same fingerprint every start until you delete the files. |
| `off` | Plain HTTP. For a reverse proxy that terminates TLS in front — list that proxy in `auth.proxy.trusted_proxies` so the cookie is still `Secure`. |
| `{cert: …, key: …}` | Your own certificate and key (a real CA, or one your devices trust), e.g. mounted under `/config/tls/`. The only mode that sends `Strict-Transport-Security`: HSTS plus a certificate a browser does not trust is a lockout, so the self-signed one never sends it. |

The self-signed certificate names `localhost`, the loopback addresses, the
instance name if it is a valid host name, and the addresses the machine had
when it was made. A changed address does not change the fingerprint; the
browser will note the name mismatch, which for a fingerprint-pinned
certificate is not the thing you are checking. Over HTTPS the session cookie
is `__Host-spark_session`: the browser itself then refuses to send it over
http or let an http page overwrite it.

### Names SPARK answers as

`app.allowed_hosts` is empty by default, meaning any `Host` header is
answered. Set it to every name and address you type to reach SPARK (and a
reverse proxy's public name) and any other `Host` gets a 421 — the defence
against DNS rebinding, where a page on another site re-points its own name
at SPARK's address so your browser talks to SPARK under that name. Loopback is
always allowed, for the healthcheck. Leave it empty until you are sure of
the list: a name you forgot is a page you cannot open.

### Dependencies and the supply chain

18 direct dependencies, 37 packages in the full closure, no npm and no build
step. Everything is pinned by version **and SHA-256 hash** in
`requirements.lock`, and the image installs from it with `--require-hashes`.

That matters more than the list does. Against `>=` constraints a build resolves
fresh from PyPI every time, so you never build the same image twice and have no
record of what shipped — and a single hijacked maintainer account is enough to
put code on your network. With hashes, an artifact has to match bytes recorded
in this repository or the build fails having installed nothing. Verified by
corrupting a hash and watching `pip` refuse.

Adding a dependency means regenerating the lock in the same commit:

```bash
uv pip compile pyproject.toml --generate-hashes --python-version 3.12 -o requirements.lock
```

`tests/test_supply_chain.py` fails if `pyproject.toml` and the lock disagree, so
this cannot rot quietly. Check for known advisories with `pip-audit -r
requirements.lock`.

The base image is pinned by digest for the same reason: `python:3.12-slim` is
rebuilt regularly and points at different bytes over time. Re-pin it when you
want a newer base, deliberately, as a commit.

### Docker settings that are not optional

```yaml
network_mode: host      # ARP/ICMP sweeps only see the LAN from the host netns
cap_drop: [ALL]         # nothing Docker grants by default is needed...
cap_add: [NET_RAW]      # ...except real ICMP; without it ping degrades to TCP probes
volumes: [./data:/data] # spark.db and the session key must survive a rebuild
```

On a bridge network SPARK sits behind NAT and discovery finds **nothing**. This
is the single most common way to end up with an empty dashboard.

The container runs as **uid 9700, not root**. The `./data` bind mount has to be
owned by that uid — `sudo chown -R 9700:9700 data` from `spark/` — and SPARK
refuses to start, printing that command, if it is not. ICMP still works for
the unprivileged user because `CAP_NET_RAW` is attached to the Python binary
as a file capability; that is also why the compose file must **not** set
`no-new-privileges` while that capability is in use — it makes the kernel
ignore file capabilities, and an ignored *effective* file capability makes
`execve` fail, so the container would not start at all.

**Without `NET_RAW` (optional, stronger).** The kernel can let ordinary users
send ICMP echo through datagram sockets, and SPARK's ping already falls back
to that. On the VM, once:

```bash
echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/90-spark-ping.conf
sudo sysctl --system
```

Then in `docker-compose.yml`: `SETCAP_NET_RAW: "0"` under `build.args`
(the image is built without the file capability), delete the `cap_add`
lines, uncomment `security_opt: [no-new-privileges:true]`, and
`docker compose up -d --build`. Press **Scan now** on the Devices page and
confirm it still finds devices; "ICMP unavailable" means the sysctl did not
take. The compose file also carries a commented `read_only: true` block:
SPARK writes only under `/data`, so it should simply work, and a
"Read-only file system" error afterwards is a bug worth reporting.

### What it costs to run

Measured on 2026-10-01, a real `spark` process sampled from `/proc` with
thirty SNMP devices on a thirty-second poll against a local agent: about
**100 MB of resident memory is Python and its libraries**
before SPARK does anything; a real home network sat at **141 MB**, and a
test with thirty SNMP devices polling every thirty seconds at **160 MB**,
flat for as long as it ran. Logins peak **128 MB above that** for a moment
(Argon2 hashes with 64 MB each, two allowed at once), which is why the
compose file's `mem_limit` is `512m` and not something tighter; below about
`384m` two people signing in during a discovery get the container killed
(`docker compose ps` shows exit 137). CPU is a few percent of one core for a
dozen devices. Disk is the one number worth watching: every check and every
poll is one SQLite commit of about 40 KB, so a busy box writes a few GB a day
— nothing to an SSD's lifetime, but visible under `docker stats` and
amplified on ZFS.

The SNMP engine is shared across polls, one per credential (`collectors/
snmp.py` says why): building one compiles pysnmp's MIB modules, and doing
that per poll was a third of each poll's CPU and the reason memory crept
up after every burst of overlapping polls.

### Networks and device identity

Subnets live in the database and are managed at `/settings` — add, rename,
retag and remove them in the browser, no restart. The `network.subnets` block
in `spark.yaml` is a **seed**: its entries are copied in on the first start and
the section is never read again, so editing it on a running install does
nothing.

Each subnet is either directly attached or routed:

```yaml
network:
  subnets:
    - name: LAN
      cidr: 172.16.10.0/24
      attached: true      # SPARK has an interface on this segment

    - name: Servers
      cidr: 172.16.30.0/24
      vlan: 30            # documentation only; nothing reads it
      attached: false     # reachable only through a router
```

This matters more than it looks. ARP only works on directly-attached layer 2
segments, and device identity keys on MAC address. Across a router SPARK can
ping a host but cannot learn its MAC, so devices on routed VLANs fall back to
IP-based identity — which breaks the moment DHCP hands out a different address.

Two ways to fix it: give the SPARK VM an interface in each VLAN, or put the
router on the SNMP list. Every 15 minutes SPARK reads the router's ARP table,
which has the MAC for every VLAN it routes, and fills in the MAC of each
device there that lacks one. A device that later turns up at a new address is
listed under **Possible duplicates** (see Duplicates under [Network map and Services](guide.md#network-map-and-services)) rather
than followed automatically.

`vlan:` is a display label; nothing reads it functionally. It is editable at
`/settings` and shows in its own column on the Devices page, which also filters
by subnet — including a "not on a configured subnet" option, the quickest way
to notice a segment you never configured.

A device's subnet is worked out from its address every time the page renders,
not stored when it was discovered. Renaming a subnet keeps its devices, and
adding one classifies devices found before it existed. The most specific match
wins, so documenting a `/8` does not swallow the `/24`s inside it.

---

## Authentication

A single admin account with a password: Argon2id hash, a random 256-bit session
token stored only as a SHA-256 hash, an HTTP-only `SameSite=Lax` cookie, and a
rate-limited login endpoint. A failed login costs the same Argon2 work whether
or not the username exists, so the login form cannot be used to enumerate
accounts.

The cookie carries an opaque token and is not itself signed — a database leak
hands over no usable sessions, and revocation is a row update. It is marked
`Secure` when the login itself arrived over HTTPS.

A session ends after 30 minutes without use (changeable under
[Preferences](guide.md#preferences)), and in any case after `auth.session_days` (30
days). The timeout is enforced by the server against the session's last-seen
time, which is written at most once a minute, so a session can end up to a
minute early but never late. Requests a page makes by itself — the live
refresh, the event stream, the timeout check — send `X-Requested-With: fetch`
and do not count as use. Raising the timeout does not revive sessions that
had already timed out: they are deleted when it is saved.

Every response carries a Content-Security-Policy that allows only SPARK's own
origin (inline scripts run on a per-request nonce), `frame-ancestors 'none'`,
`nosniff`, and `no-store` on pages. Every POST is checked against its `Origin`
header and refused if it came from another site, as a second layer over
`SameSite=Lax`. Behind a reverse proxy, keep the `Host` header intact (the
default everywhere) or every form will answer 403. There is no OpenAPI
document or Swagger page; there is no API.

**Input.** Every query is parameterised, every page is escaped by the template
engine, and nothing runs a shell (ping is ICMP from Python, ARP is read from
`/proc`), so a name or address containing SQL, markup or shell syntax is only
ever text. On top of that, every form field has a length cap (`limits.py`)
that the page enforces with `maxlength` and the server enforces again; a
request body over 64 KB is refused with 413 before any route reads it; an id
too large to exist is a 404 rather than a database error; and a malformed
form gets a page saying which field was wrong, never a stack trace or a JSON
dump.

SPARK ends up holding a map of your entire network, an inventory of every
service on it, and references to credentials that reach your Docker hosts. That
makes it the highest-value target on the LAN, which is why there is no
"it's internal, skip the login" mode.

Passwords are hashed in a thread, at most two at a time, so a burst of login
attempts costs bounded threads and memory rather than stalling every check
and poll for the length of each attempt.

**First run.** With no account in the database, SPARK prints a setup code to
its log and `/setup` requires it (see [Deploy](../README.md#deploy)). Wrong
codes count toward the same lockout as wrong passwords, and the database
itself allows exactly one administrator (a partial unique index on
`user.is_admin`), so concurrent attempts to claim a fresh install produce one
account, not several.

**Reverse proxies.** SPARK, not the web server underneath it, decides which
peers are proxies. `auth.proxy.trusted_proxies` is that list, in **both** auth
modes: only a connection from an address on it has its `X-Forwarded-For`
(the last entry — the one that proxy added) and `X-Forwarded-Proto`
believed. Everything else is taken at its TCP address. In password mode the
list is optional; set it when a proxy fronts SPARK so the login lockout keys
on real clients rather than on the proxy, and so the cookie is `Secure` when
the proxy terminated TLS. uvicorn's own proxy-header handling is switched
off: it trusted loopback by default, and with host networking loopback is
every container and process on the VM.

To put it behind Authelia, Tailscale or Cloudflare Access instead, the proxy
authenticates and passes an identity header:

```yaml
auth:
  mode: proxy
  proxy:
    header: Remote-User
    trusted_proxies: [172.16.10.5]   # the proxy's address as SPARK sees it
```

Two layouts, and the difference matters:

- **Proxy on another machine:** list its address, as above, and firewall port
  9700 on the SPARK host so only that address can reach it. The identity
  header is only as private as the port.
- **Proxy on the same VM** (Caddy, nginx, Traefik on the host, or a container
  with host networking): list `127.0.0.1`, and set `app.host: 127.0.0.1` so
  the port is not on the LAN at all. SPARK logs a warning at start if it is
  listening on every interface in proxy mode.

In either case the proxy must **set** the identity header itself, never pass
through one a client sent. The identity header is checked against the
connection's real peer address, never against anything a header claims, so a
request from anywhere else carrying `X-Forwarded-For: <proxy>` is still a
request from anywhere else.

SPARK refuses to start in proxy mode with an empty `trusted_proxies`. Trusting
an identity header from any source is forgeable by anything on the network —
worse than no auth, because it looks like security.

**Files.** Everything under `data/` is created readable by SPARK alone
(`0600`), the database and its write-ahead log included; a database from an
older version is made private at the next start. Reading it from the host
therefore takes `sudo`.

**Transport.** HTTPS by default, with SPARK's own self-signed certificate
pinned by fingerprint (see [TLS](#tls)); the cookie is `Secure` and
`__Host-`-prefixed over it. Every response also carries
`Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy: same-origin`.
`app.allowed_hosts` closes the DNS-rebinding angle once set (see
[Names SPARK answers as](#names-spark-answers-as)).

**Sign-in events** — a lockout, a sign-in from a new address, a password
change or reset, first-run setup — go to Discord like any other alert, so the
account has a detective control as well as the preventive ones.

---

## Upgrading

```bash
cd ~/NetworkMonitoringApp/spark        # wherever the clone is
docker compose stop
sudo cp -a data data.bak-$(date +%F)   # spark.db, secret.key, tls/, backups/
git pull
docker compose up -d --build
docker compose logs --tail=40 spark
```

Every commit on `main` is meant to be deployable, and the CHANGELOG says
when one needs more than this (a migration, a config change, a new URL).

`docker compose stop` and `restart` finish in about three seconds. SPARK
stops waiting for idle browser connections after that long and runs its
shutdown (the scheduler, then the database); a TLS close to a tab that is
not reading would otherwise wait 30 s, past Docker's 10 s and into a
SIGKILL. The compose file's `stop_grace_period` is set above SPARK's own
limit, so a clean exit is what you get.

The short version is in the [README](../README.md#upgrade).
