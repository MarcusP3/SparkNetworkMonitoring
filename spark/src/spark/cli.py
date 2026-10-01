"""Command-line tools.

`spark-probe` exists because vendor SNMP support is uneven and badly
documented. Point it at a switch and it reports what that device actually
answers, so you find out before building anything on an assumption.

`spark-reset-password` is the way back in when the admin password is lost.
It runs on the machine SPARK runs on (`docker compose exec spark
spark-reset-password`), which is the one place a person has to be to use it:
there is no reset by email, no security question, no back door from the
network. It signs out every session as it goes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from .collectors import SnmpCollector, SnmpCredential

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _colour(enabled: bool):  # type: ignore[no-untyped-def]
    if enabled:
        return GREEN, RED, YELLOW, DIM, BOLD, RESET
    return "", "", "", "", "", ""


def _human_bytes(value: int | None) -> str:
    if not value:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


async def _run_probe(args: argparse.Namespace) -> int:
    green, red, yellow, dim, bold, reset = _colour(sys.stdout.isatty() and not args.json)

    credential = SnmpCredential(
        version=args.version,
        community=args.community,
        username=args.username or "",
        auth_protocol=args.auth_protocol,
        auth_key=args.auth_key or "",
        priv_protocol=args.priv_protocol,
        priv_key=args.priv_key or "",
        port=args.port,
        timeout=args.timeout,
        retries=args.retries,
    )

    collector = SnmpCollector(args.host, credential)
    try:
        report = await collector.probe()

        if not report.reachable:
            if args.json:
                print(json.dumps(asdict(report), indent=2, default=str))
            else:
                print(f"{red}No SNMP response from {args.host}:{args.port}{reset}")
                print(f"  {report.error}")
                print()
                print("Things worth checking:")
                print("  · SNMP is enabled and the community string matches")
                print("  · UDP 161 is not blocked between here and the device")
                print("  · On UniFi, SNMP is a global Network setting, not per-device")
                print("  · UniFi consoles (UDM/UDM-Pro) do not expose SNMP through the UI")
            return 1

        health = await collector.collect_health()
        interfaces = await collector.collect_interfaces()

        if args.json:
            print(json.dumps(
                {
                    "probe": asdict(report),
                    "health": asdict(health),
                    "interfaces": [asdict(i) for i in interfaces],
                },
                indent=2,
                default=str,
            ))
            return 0

        print()
        print(f"{bold}{report.sys_name or args.host}{reset}  {dim}({args.host}){reset}")
        print(f"  Vendor      {report.vendor or 'unrecognised'}")
        print(f"  Model       {health.model or '—'}")
        print(f"  Serial      {health.serial or '—'}")
        print(f"  Uptime      {report.uptime_human or '—'}")
        if report.sys_descr:
            descr = report.sys_descr.replace("\n", " ")[:100]
            print(f"  Description {dim}{descr}{reset}")
        print(f"  Probed in   {report.duration_seconds}s")

        print()
        print(f"{bold}Health{reset}")
        cpu = f"{health.cpu_percent}%" if health.cpu_percent is not None else "—"
        mem = f"{health.memory_percent}%" if health.memory_percent is not None else "—"
        print(f"  CPU         {cpu}  {dim}{health.sources.get('cpu', 'not reported')}{reset}")
        if health.load_1min is not None:
            print(f"  Load        {health.load_1min:.2f} / {health.load_5min:.2f} / "
                  f"{health.load_15min:.2f}  {dim}1m / 5m / 15m average{reset}")
        print(f"  Memory      {mem}  {dim}{health.sources.get('memory', 'not reported')}"
              f"{reset}")
        if health.memory_total_bytes:
            print(f"              {dim}{_human_bytes(health.memory_used_bytes)} of "
                  f"{_human_bytes(health.memory_total_bytes)}{reset}")
        if health.temperatures:
            for reading in health.temperatures:
                print(f"  Temp        {reading.celsius}°C  {dim}{reading.name}{reset}")
        else:
            print(f"  Temp        —  {dim}no ENTITY-SENSOR-MIB sensors reported{reset}")

        print()
        print(f"{bold}Capabilities{reset}")
        for capability in report.capabilities:
            if capability.supported:
                mark = f"{green}yes{reset}"
                detail = f"{capability.sample_count} row(s)"
            else:
                mark = f"{red} no{reset}"
                detail = capability.error or "no data"
            print(f"  {mark}  {capability.label:<48} {dim}{detail}{reset}")
            if capability.notes and not capability.supported:
                print(f"       {dim}{yellow}{capability.notes}{reset}")

        if interfaces:
            shown = [i for i in interfaces if args.all_interfaces or i.is_up]
            print()
            print(f"{bold}Interfaces{reset}  {dim}({len(shown)} of {len(interfaces)} shown"
                  f"{'' if args.all_interfaces else ', up only'}){reset}")
            print(f"  {'#':>4}  {'name':<20} {'status':<8} {'speed':>9}  "
                  f"{'in':>12} {'out':>12}  errors")
            for interface in shown[: args.limit]:
                status = interface.oper_status or "?"
                colour = green if status == "up" else dim
                speed = f"{interface.speed_mbps} Mb" if interface.speed_mbps else "—"
                errors = (interface.in_errors or 0) + (interface.out_errors or 0)
                error_text = f"{red}{errors}{reset}" if errors else f"{dim}0{reset}"
                print(f"  {interface.index:>4}  {interface.label[:20]:<20} "
                      f"{colour}{status:<8}{reset} {speed:>9}  "
                      f"{_human_bytes(interface.in_octets):>12} "
                      f"{_human_bytes(interface.out_octets):>12}  {error_text}")
            if len(shown) > args.limit:
                print(f"  {dim}… {len(shown) - args.limit} more (use --limit){reset}")

            if interfaces and not any(i.counters_are_64bit for i in interfaces):
                print()
                print(f"  {yellow}Note:{reset} this device only offers 32-bit counters. "
                      "They wrap in\n        under six minutes on a saturated gigabit "
                      "link, so throughput\n        figures will be unreliable.")
        print()
        return 0
    finally:
        await collector.close()


def probe_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spark-probe",
        description="Ask a device what it actually supports over SNMP.",
    )
    parser.add_argument("host", help="IP address or hostname")
    parser.add_argument("-c", "--community", default="public", help="v2c community string")
    parser.add_argument("-v", "--version", choices=["v2c", "v3"], default="v2c")
    parser.add_argument("-p", "--port", type=int, default=161)
    parser.add_argument("-t", "--timeout", type=float, default=3.0)
    parser.add_argument("-r", "--retries", type=int, default=1)
    parser.add_argument("--username", help="SNMPv3 username")
    parser.add_argument("--auth-protocol", default="SHA",
                        choices=["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"])
    parser.add_argument("--auth-key", help="SNMPv3 authentication key")
    parser.add_argument("--priv-protocol", default="AES",
                        choices=["DES", "3DES", "AES", "AES192", "AES256"])
    parser.add_argument("--priv-key", help="SNMPv3 privacy key")
    parser.add_argument("--all-interfaces", action="store_true",
                        help="Include interfaces that are down")
    parser.add_argument("--limit", type=int, default=40, help="Max interfaces to print")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")

    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run_probe(args))
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------
# spark-reset-password
# --------------------------------------------------------------------------


async def _reset_password(username: str | None, password: str) -> str:
    """Set the password on the one account (or the named one). Returns the
    username it changed, or raises SystemExit with a message for a person."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from .auth import AuthError, set_password
    from .config import load_config
    from .db import open_engine
    from .models import User

    config = load_config()
    if not config.app.db_path.exists():
        raise SystemExit(f"No database at {config.app.db_path}. Is SPARK_CONFIG (or "
                         "SPARK__APP__DATA_DIR) pointing where SPARK keeps its data?")
    # Its own engine, not the app's process-wide one: this is a separate
    # process in real use, and disposing of it must not affect anything else.
    engine = open_engine(config)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            if username:
                user = await session.scalar(select(User).where(User.username == username))
                if user is None:
                    raise SystemExit(f"There is no account called {username!r}.")
            else:
                users = list((await session.execute(select(User))).scalars())
                if not users:
                    raise SystemExit("There is no account yet. Open /setup in a browser; "
                                     "the setup code is in SPARK's log.")
                if len(users) > 1:
                    names = ", ".join(u.username for u in users)
                    raise SystemExit(f"More than one account ({names}); say which with --username.")
                user = users[0]
            try:
                await set_password(session, user, password)
            except AuthError as exc:
                raise SystemExit(str(exc)) from None
            from .auth import note_password_changed

            await note_password_changed(session, "reset with spark-reset-password")
            await session.commit()
            return user.username
    finally:
        await engine.dispose()


def reset_password_main(argv: list[str] | None = None) -> int:
    import getpass

    parser = argparse.ArgumentParser(
        prog="spark-reset-password",
        description="Set a new administrator password and sign out every session. "
                    "Run it where SPARK runs: docker compose exec spark spark-reset-password",
    )
    parser.add_argument("--username", help="Which account, if there is more than one")
    parser.add_argument("--password-stdin", action="store_true",
                        help="Read the new password from standard input instead of prompting "
                             "(for scripts; the terminal is the safer place to type it)")
    args = parser.parse_args(argv)

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("New password again: "):
            print("The two passwords do not match; nothing changed.", file=sys.stderr)
            return 1
    try:
        changed = asyncio.run(_reset_password(args.username, password))
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(f"Password changed for {changed!r}. Every session has been signed out; "
          "sign in again with the new password.")
    return 0


if __name__ == "__main__":
    sys.exit(probe_main())
