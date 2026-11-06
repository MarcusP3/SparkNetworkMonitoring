# Getting started

The first run, from an empty install to a network SPARK can see, poll and
alert on. SPARK asks for most of it itself: creating the account opens a
**setup wizard**. This page is what to have ready for each question, and
what to do once it is done. The detail behind each step is in the
[guide](guide.md#first-run-setup-and-banners).

1. [Before you install: plan your segments](#1-before-you-install-plan-your-segments)
2. [Deploy and create the account](#2-deploy-and-create-the-account)
3. [The wizard: subnets](#3-the-wizard-subnets)
4. [The wizard: SNMP](#4-the-wizard-snmp)
5. [The wizard: integrations](#5-the-wizard-integrations)
6. [The wizard: alerts and finish](#6-the-wizard-alerts-and-finish)
7. [After the wizard](#7-after-the-wizard)

Skip anything you are not ready for. Banners under each page's heading keep
track of what is left, and the wizard stays at `/welcome`.

---

## 1. Before you install: plan your segments

Write down each network segment you want watched:

| Name | CIDR | VLAN | Can the SPARK host have a NIC on it? |
|---|---|---|---|
| LAN | `192.168.1.0/24` | 1 | yes |
| Servers | `192.168.30.0/24` | 30 | yes |
| IoT | `192.168.20.0/24` | 20 | no — routed only |

The last column matters most.

- **On a segment SPARK has an interface on**, it reads ARP, so every device is
  known by its MAC address and keeps its identity when DHCP moves it.
- **On a segment it reaches only through a router**, it can ping hosts but
  never learn their MACs. Those devices are known by IP, and look new
  whenever their address changes, until SNMP or UniFi fills the MAC in.

So if you can, **give the SPARK VM a NIC (or a tagged sub-interface) in each
VLAN** before installing — on Proxmox, one more network device on the VM per
VLAN tag. The wizard then finds them all by itself.

Also have ready:

- addresses SPARK must never touch — a fragile printer, a PLC, a range you
  may not scan;
- SNMP turned on, read-only, on your switches and servers (section 4);
- read-only API keys for TrueNAS, Proxmox and UniFi, if you run them
  (section 5);
- a Discord webhook, if that is where alerts should go.

None of it is required to finish: everything can be added later.

## 2. Deploy and create the account

Follow [Deploy](../README.md#deploy) in the README: a Linux VM with Docker
(not Docker Desktop — its host networking cannot do ARP or ICMP), `./data`
owned by uid 9700, and `docker compose up -d --build`. You do not need to put
subnets in `spark.yaml`; the wizard asks.

1. Open `https://<host>:9700` and check the certificate fingerprint against
   `docker compose logs spark | grep -A8 fingerprint` before accepting it.
2. Create the admin account with the **setup code** from
   `docker compose logs spark | grep -A3 "setup code"`. Pick your time zone
   here: quiet hours, maintenance windows and reports use it.

The wizard opens next.

## 3. The wizard: subnets

The networks the SPARK host already has an address on are filled in and
ticked as **directly attached**; a VLAN tag is read from interface names like
`eth0.30`. Container bridges, loopback and VPN interfaces are left out.

- **Name** them (LAN, Servers, IoT). The VLAN column is a label only.
- **Add routed segments** in the blank rows — those from your plan with
  "no" in the last column — and leave *Directly attached* unticked.
- **Untick** anything you do not want swept. A subnet over 1,024 hosts (a
  `/16`) is shown unticked: narrow it to the ranges you use.
- **Already listed** shows anything from `spark.yaml`. The example file
  lists `192.168.1.0/24`; **Remove** it if that is not your network.
- **Never scan these**: one address or `start-end` range per line.

## 4. The wizard: SNMP

SNMP gives CPU, memory, temperature, interface traffic and storage per
device, and the switch MAC tables the network map is built from.

**In your devices first**, read-only only:

- **SNMPv3 (authPriv) wherever it is offered** — SHA (or better) and AES.
- **v2c** if that is all there is. The community travels in clear text: keep
  it read-only, on a network you trust, and never a real password.
- **UniFi:** SNMP is one global setting (UniFi Network → Settings → System).
  Switches answer; UDM consoles, USW Flex/Ultra and access points do not — the
  UniFi integration covers those.
- **Linux and Proxmox hosts:** `snmpd` with a read-only v3 user or community.

Not sure what a device answers? Ask it from the SPARK host:

```bash
docker compose run --rm spark spark-probe 192.168.1.2 -c your-community
# v3:  -v v3 --username spark --auth-key ... --priv-key ...
```

**In the wizard**, choose v2c or v3 and enter the credentials. One profile
covers most networks; add more later under Settings → SNMP. Nothing is
asked with it until you say so (section 6).

## 5. The wizard: integrations

Tick what you run. Give an address and key now, or tick it and add the key
later — a banner reminds you until you do. All three are read-only.

| | Key | Compare the fingerprint with |
|---|---|---|
| **TrueNAS** | A user with the **Read-only Administrator** role → user menu (top right) → My API Keys | System → Certificates |
| **Proxmox** | The `pveum` commands the page shows: user `spark@pve`, role PVEAuditor, token `monitor`. Token ID `spark@pve!monitor`; the secret is shown once | `pvenode cert info` on the host |
| **UniFi** | UniFi Network → **Integrations** → create an API key. The address is the console's | Your browser's padlock on the console's own page |

**Connect** fetches each certificate and **sends nothing** until you have
compared the fingerprint and pressed **Trust this certificate**. It then
logs in and shows *connected*. Each one is tied to its device as soon as the
first scan finds that device at the address you gave.

Hermes Agent needs nothing here: the port scan finds its dashboard.

## 6. The wizard: alerts and finish

**Alerts:** paste a Discord webhook (Server Settings → Integrations →
Webhooks → New Webhook → Copy Webhook URL) and **Save and send a test**.
ntfy, email and webhooks are under Alerts → Channels. Until something is set
up, SPARK records problems but tells no one.

**Finish** shows what was set up and **starts the first scan**. With an SNMP
profile you can tick **look for devices that answer SNMP**: once the scan
has run, SPARK asks each device found for its name with your profile and
lists the ones that answer. It is off by default because a v2c community
goes to every device in clear text; you can press **Find SNMP** on Devices
later instead.

## 7. After the wizard

On **Devices**, when the scan has finished:

1. **Name** the devices you care about.
2. **Watch** what you want alerted on (tick several and **Watch N** for many
   at once). Start with the gateway, switches, NAS, hypervisors and DNS.
3. **Add** the SNMP devices Find SNMP listed, and press **Test** on each.
   `Unreachable` means down, SNMP off, a wrong community or a wrong v3
   privacy password; `AuthFailed` means a wrong v3 user or auth password.
4. **Set dependencies** — each host behind its switch, the switch behind the
   gateway — so a dead switch is one alert, not thirty
   ([Dependencies](guide.md#dependencies)).

Then, before walking away:

- [ ] Clear the banners: each links to what it needs.
- [ ] Stop one watched thing and see both the down and the recovery alert.
- [ ] Settings → Backup → **Download** an encrypted backup and keep it off the
      machine. It holds the database *and* `secret.key`; without the key no
      stored credential can be read.
- [ ] Optional: [heartbeats](guide.md#heartbeats) for backups and cron jobs,
      [SNMP traps and syslog](guide.md#events-traps-and-syslog), and the
      [Internet](guide.md#internet) card.

From here, [Using SPARK](guide.md) covers every page.
