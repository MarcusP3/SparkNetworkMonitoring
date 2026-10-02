# <img src="spark/src/spark/static/brand/favicon.svg" width="36" height="36" alt="" align="top"> SPARK

**Network monitoring for homelabs.** Health checks, alerting, and a live map of
every device and service on your network — in one container, with one SQLite
file to back up.

Homelab tooling makes you choose between uptime checkers that know nothing about
your network and enterprise NMS platforms built for a NOC with a full-time
operator. SPARK aims at the middle: it discovers the network itself and shows
you what is actually running, rather than making you type it all in.

**Version** 0.1.0 · **Python** 3.12+ · **Runtime** one Docker container on a
Linux VM · **Storage** one SQLite file

## What it does

- **Finds your network.** ICMP/ARP sweeps of your subnets, devices keyed on
  MAC (they survive DHCP), vendor lookup, and a TCP port scan for services.
- **Checks what matters.** Ping, TCP, HTTP(S) and DNS checks with hysteresis,
  incidents, and dependency suppression — one alert for the switch, not thirty.
- **Reads your gear over SNMP.** CPU, memory, temperature, interface traffic,
  storage and history charts per device, v2c and v3.
- **Talks to TrueNAS, Proxmox and UniFi** over their own APIs: drive health,
  pools, TrueNAS alerts, VMs and containers, SMART, UniFi devices and
  clients — read-only, certificate-pinned.
- **Draws the map.** A searchable network map built from switch MAC tables,
  with services on their own tab; alerts stay quiet below a device that is down.
- **Tells you in Discord** when something goes down and when it comes back,
  with quiet hours, a mute list and per-device suppressions.
- **Backs itself up** every night, with encrypted downloads and a one-command
  restore.

## Deploy

Needs a Linux host (a small VM is ideal) with Docker and the Compose plugin,
and a network interface on the network you want to watch. The application is
in the `spark/` folder; every command below runs from there.

```bash
git clone https://github.com/MarcusP3/SparkNetworkMonitoring.git
cd SparkNetworkMonitoring/spark
cp config/spark.example.yaml config/spark.yaml   # yours; git ignores it
$EDITOR config/spark.yaml                        # set your first subnet
mkdir -p data && sudo chown 9700:9700 data       # the container runs as uid 9700, not root
docker compose up -d --build
```

Then:

1. Open `https://<host>:9700`. The browser warns once: SPARK made its own
   certificate. Check the fingerprint against the one in the log before
   accepting:
   `docker compose logs spark | grep -A8 fingerprint`
2. Create the admin account. The form asks for a **setup code** from the log:
   `docker compose logs spark | grep -A3 "setup code"`
3. Add a subnet or two under Settings, press **Scan now** on Devices, and
   **Watch** what you care about.

Check it is healthy:

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:9700/healthz   # 200
docker compose logs --tail=20 spark                                      # "ready ..."
```

Two things that matter: the compose file uses `network_mode: host` (on a
bridge network discovery finds nothing), and **Docker Desktop for Mac and
Windows will not work** for discovery — use a Linux VM. Details, and the
optional hardening, are in [Configuration](docs/configuration.md).

## Upgrade

```bash
cd ~/SparkNetworkMonitoring/spark       # wherever the clone is
docker compose stop
sudo cp -a data data.bak-$(date +%F)    # spark.db, secret.key, tls/, backups/
git pull
docker compose up -d --build
docker compose logs --tail=40 spark
```

Database changes migrate on start. The [CHANGELOG](spark/CHANGELOG.md) says
when an upgrade needs anything more.

## Back up and restore

Settings → **Backup**. SPARK makes a backup every night at 04:00 into
`data/backups/` and keeps the last seven. A backup holds the database,
`secret.key` (without it no stored credential can be read) and the HTTPS
certificate. Copy one off the machine now and then — a backup on the same
disk does not survive the disk.

**Download** one from that page: it is encrypted with a passphrase you type
(12+ characters, never stored). Lose the passphrase and the file is useless.

**Restore**, with SPARK stopped:

```bash
docker compose stop spark
docker compose run --rm spark spark-restore /data/backups/spark-backup-YYYYMMDD-HHMMSS.tar.gz
docker compose up -d
```

For a downloaded `.sparkbackup`, copy it into `data/` first and give
`/data/<file>`; it asks for the passphrase. Nothing changes until the whole
file checks out, and the data that was there is kept in
`data/pre-restore-<when>/`.

## Lost password

```bash
docker compose exec spark spark-reset-password
```

It sets a new password and signs out every session. There is no reset from
the network: being at the machine is the credential.

## Documentation

- [Using SPARK](docs/guide.md) — monitoring, the map, alerts, SNMP,
  TrueNAS/Proxmox/UniFi credentials, backups, page by page.
- [Configuration and security](docs/configuration.md) — `spark.yaml`, TLS,
  Docker settings, resource use, sign-in and how the web UI is protected.
- [Development](docs/development.md) — what is built, tests, CI, code layout,
  conventions, roadmap and design decisions.
- [DESIGN.md](DESIGN.md) — the original design · [SECURITY.md](SECURITY.md) —
  reporting a vulnerability · [CHANGELOG](spark/CHANGELOG.md) — every change.

## License

Copyright © 2026 Marcus Pierce. SPARK is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE.md): free for personal,
homelab, hobby and educational use. Any commercial use — including use inside
a business, resale, rebranding, or hosting it as a service — needs a separate
license. Contact me through GitHub.
