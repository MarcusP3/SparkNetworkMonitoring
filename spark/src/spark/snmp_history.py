"""Reading SNMP history back out, as chart-sized series.

Polling writes a row a minute; retention folds those into five-minute and then
hourly buckets. A chart wants neither: it wants a few hundred evenly spaced
points across whatever range was asked for, whichever of the three tables the
data happens to live in by now.

So every range has a fixed bucket width, chosen to give 60-360 points, and each
query groups its rows into those buckets in SQL. Raw samples and rollups cover
disjoint stretches of time -- retention folds and deletes in one transaction,
on aligned cutoffs -- so a bucket's value can be combined from all three tables
without counting anything twice.

Combining is a weighted average, never an average of averages: a five-minute
rollup of five polls and a raw bucket of one poll are not equal votes. Peaks
combine as a maximum, which is the point of keeping them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import FIVE_MINUTES, ONE_HOUR

# name -> (span in seconds, bucket width in seconds)
RANGES: dict[str, tuple[int, int]] = {
    "1h": (3600, 60),
    "24h": (86400, 300),
    "7d": (7 * 86400, 1800),
    "30d": (30 * 86400, 7200),
}
DEFAULT_RANGE = "24h"


def parse_range(value: str | None) -> str:
    return value if value in RANGES else DEFAULT_RANGE


@dataclass
class Window:
    """The buckets a chart is drawn on. `start` is aligned to `width`."""

    start: datetime
    width: int
    count: int

    @classmethod
    def ending(cls, now: datetime, range_name: str) -> Window:
        span, width = RANGES[parse_range(range_name)]
        # Aligned to the width, so a bucket means the same interval on every
        # load and the last one is the one "now" is in.
        end = int(now.timestamp()) // width * width + width
        start = end - span
        return cls(
            start=datetime.fromtimestamp(start, tz=timezone.utc),
            width=width,
            count=span // width,
        )

    @property
    def start_epoch(self) -> int:
        return int(self.start.timestamp())

    @property
    def since(self) -> str:
        """The start in the text form the ORM stores, for comparing in SQL.

        Bound as a string on purpose: a datetime parameter goes through the
        sqlite3 module's deprecated default adapter, which writes an offset
        ("+00:00") the stored values do not have. Same format, and the
        comparison is exact and can use the (device, ts) index.
        """
        return self.start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    def time_of(self, index: int) -> datetime:
        return self.start + timedelta(seconds=index * self.width)


@dataclass
class Series:
    """Average and peak per bucket. None where there is nothing to show."""

    avg: list[float | None]
    peak: list[float | None]

    @property
    def has_data(self) -> bool:
        return any(v is not None for v in self.avg) or any(v is not None for v in self.peak)

    def overall_peak(self) -> float | None:
        values = [v for v in self.peak if v is not None]
        return max(values) if values else None

    def latest(self) -> float | None:
        for value in reversed(self.avg):
            if value is not None:
                return value
        return None


class _Acc:
    """Weighted sums per bucket, to be combined across tables."""

    def __init__(self, count: int) -> None:
        self.total = [0.0] * count
        self.weight = [0.0] * count
        self.peak: list[float | None] = [None] * count

    def add(self, index: int, avg: float | None, weight: float | None,
            peak: float | None) -> None:
        if not 0 <= index < len(self.total):
            return
        if avg is not None and weight:
            self.total[index] += avg * weight
            self.weight[index] += weight
        if peak is not None:
            current = self.peak[index]
            self.peak[index] = peak if current is None else max(current, peak)

    def series(self) -> Series:
        return Series(
            avg=[t / w if w else None for t, w in zip(self.total, self.weight)],
            peak=list(self.peak),
        )


# Bucket index of a timestamp column, relative to the window. Integer division
# of epoch seconds, the same arithmetic retention uses to build its buckets.
_INDEX = "CAST((strftime('%s', {column}) - :start) / :width AS INTEGER)"


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

HEALTH_METRICS = ("cpu", "memory", "temperature", "load")


@dataclass
class HealthHistory:
    window: Window
    cpu: Series
    memory: Series
    temperature: Series
    load: Series
    polls: int = 0
    answered: int = 0

    @property
    def answered_fraction(self) -> float | None:
        return self.answered / self.polls if self.polls else None


async def health_history(session: AsyncSession, snmp_device_id: int,
                         window: Window) -> HealthHistory:
    accs = {name: _Acc(window.count) for name in HEALTH_METRICS}
    params = {"id": snmp_device_id, "start": window.start_epoch, "width": window.width,
              "since": window.since}
    polls = answered = 0

    raw = await session.execute(text(f"""
        SELECT {_INDEX.format(column="ts")} AS i,
               COUNT(*), SUM(CASE WHEN reachable THEN 1 ELSE 0 END),
               COUNT(cpu_percent), AVG(cpu_percent), MAX(cpu_percent),
               COUNT(memory_percent), AVG(memory_percent), MAX(memory_percent),
               COUNT(temperature_max), MAX(temperature_max),
               COUNT(load_1min), AVG(load_1min), MAX(load_1min)
        FROM snmp_health_sample
        WHERE snmp_device_id = :id AND ts >= :since
        GROUP BY i
    """), params)
    for (i, n, ok, n_cpu, cpu, cpu_max, n_mem, mem, mem_max,
         n_temp, temp_max, n_load, load, load_max) in raw.all():
        polls += n or 0
        answered += ok or 0
        accs["cpu"].add(i, cpu, n_cpu, cpu_max)
        accs["memory"].add(i, mem, n_mem, mem_max)
        # Temperature is the hottest sensor, and rollups keep only its peak,
        # so its "average" line is the peak everywhere. One number, honestly.
        accs["temperature"].add(i, temp_max, n_temp, temp_max)
        accs["load"].add(i, load, n_load, load_max)

    rolled = await session.execute(text(f"""
        SELECT {_INDEX.format(column="bucket_start")} AS i,
               SUM(samples), SUM(reachable),
               SUM(CASE WHEN cpu_avg IS NOT NULL THEN reachable END),
               SUM(cpu_avg * reachable), MAX(cpu_max),
               SUM(CASE WHEN memory_avg IS NOT NULL THEN reachable END),
               SUM(memory_avg * reachable), MAX(memory_max),
               MAX(temperature_max),
               SUM(CASE WHEN load_avg IS NOT NULL THEN reachable END),
               SUM(load_avg * reachable)
        FROM snmp_health_rollup
        WHERE snmp_device_id = :id AND bucket_start >= :since
          AND bucket_seconds IN (:five, :hour)
        GROUP BY i
    """), {**params, "five": FIVE_MINUTES, "hour": ONE_HOUR})
    for (i, n, ok, w_cpu, s_cpu, cpu_max, w_mem, s_mem, mem_max,
         temp_max, w_load, s_load) in rolled.all():
        polls += n or 0
        answered += ok or 0
        accs["cpu"].add(i, s_cpu / w_cpu if w_cpu else None, w_cpu, cpu_max)
        accs["memory"].add(i, s_mem / w_mem if w_mem else None, w_mem, mem_max)
        accs["temperature"].add(i, temp_max, 1 if temp_max is not None else 0, temp_max)
        accs["load"].add(i, s_load / w_load if w_load else None, w_load, None)

    return HealthHistory(
        window=window,
        cpu=accs["cpu"].series(),
        memory=accs["memory"].series(),
        temperature=accs["temperature"].series(),
        load=accs["load"].series(),
        polls=polls,
        answered=answered,
    )


# --------------------------------------------------------------------------
# Traffic
# --------------------------------------------------------------------------


@dataclass
class TrafficHistory:
    """One interface's traffic across the window."""

    inbound: Series
    outbound: Series
    in_errors: int = 0
    out_errors: int = 0

    @property
    def has_data(self) -> bool:
        return self.inbound.has_data or self.outbound.has_data

    @property
    def busy(self) -> float:
        """How much it carried, for choosing which port to show first."""
        return sum(v or 0 for v in self.inbound.avg) + sum(v or 0 for v in self.outbound.avg)


@dataclass
class _TrafficAcc:
    inbound: _Acc
    outbound: _Acc
    in_errors: int = 0
    out_errors: int = 0


async def traffic_history(session: AsyncSession, interface_ids: list[int],
                          window: Window) -> dict[int, TrafficHistory]:
    """Traffic for several interfaces at once: one query per table, not per port."""
    if not interface_ids:
        return {}
    accs = {
        iid: _TrafficAcc(_Acc(window.count), _Acc(window.count)) for iid in interface_ids
    }
    # The ids are integers from our own rows, formatted in, because a bound
    # parameter per id is how a 48-port switch meets SQLite's variable limit.
    ids = ",".join(str(int(i)) for i in interface_ids)
    params = {"start": window.start_epoch, "width": window.width, "since": window.since}

    raw = await session.execute(text(f"""
        SELECT interface_id, {_INDEX.format(column="ts")} AS i,
               COUNT(in_bps), AVG(in_bps), MAX(in_bps),
               COUNT(out_bps), AVG(out_bps), MAX(out_bps),
               SUM(in_errors), SUM(out_errors)
        FROM snmp_interface_sample
        WHERE interface_id IN ({ids}) AND ts >= :since
        GROUP BY interface_id, i
    """), params)
    for iid, i, n_in, a_in, m_in, n_out, a_out, m_out, e_in, e_out in raw.all():
        acc = accs[iid]
        acc.inbound.add(i, a_in, n_in, m_in)
        acc.outbound.add(i, a_out, n_out, m_out)
        if 0 <= i < window.count:
            acc.in_errors += e_in or 0
            acc.out_errors += e_out or 0

    rolled = await session.execute(text(f"""
        SELECT interface_id, {_INDEX.format(column="bucket_start")} AS i,
               SUM(CASE WHEN in_avg IS NOT NULL THEN samples END),
               SUM(in_avg * samples), MAX(in_max),
               SUM(CASE WHEN out_avg IS NOT NULL THEN samples END),
               SUM(out_avg * samples), MAX(out_max),
               SUM(in_errors), SUM(out_errors)
        FROM snmp_interface_rollup
        WHERE interface_id IN ({ids}) AND bucket_start >= :since
          AND bucket_seconds IN (:five, :hour)
        GROUP BY interface_id, i
    """), {**params, "five": FIVE_MINUTES, "hour": ONE_HOUR})
    for iid, i, w_in, s_in, m_in, w_out, s_out, m_out, e_in, e_out in rolled.all():
        acc = accs[iid]
        acc.inbound.add(i, s_in / w_in if w_in else None, w_in, m_in)
        acc.outbound.add(i, s_out / w_out if w_out else None, w_out, m_out)
        if 0 <= i < window.count:
            acc.in_errors += e_in or 0
            acc.out_errors += e_out or 0

    return {
        iid: TrafficHistory(
            inbound=acc.inbound.series(),
            outbound=acc.outbound.series(),
            in_errors=acc.in_errors,
            out_errors=acc.out_errors,
        )
        for iid, acc in accs.items()
    }
