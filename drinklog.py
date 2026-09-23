#!/usr/bin/env python3
"""
Decode the Ember mug's on-device drink log (statistics characteristic fc540013),
track drinks live inside the menu bar app, and render the drink-log page.

    python drinklog.py                  # summary of drinks.jsonl (+ the drink in progress)
    python drinklog.py --json           # same, machine-readable
    python drinklog.py --html out.html  # the drink-log page
    python drinklog.py --from-capture   # ignore drinks.jsonl; rebuild everything from the raw capture

Two data sources feed each drink:
  * the mug's own log (pour, targets, set-downs, a level/temperature sample every 10 min,
    a battery snapshot) — survives the Mac being away, arrives as a backlog on connect;
  * the app's own readings (history.jsonl) — second-level detail while connected.
Format reference: docs/statistics-stream.md. No GUI dependencies.
"""

from __future__ import annotations

import argparse
import bisect
import html
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "EmberMug"
CAPTURE_PATH = SUPPORT_DIR / "stats-capture.jsonl"
HISTORY_PATH = SUPPORT_DIR / "history.jsonl"
DRINKS_PATH = SUPPORT_DIR / "drinks.jsonl"
CURRENT_PATH = SUPPORT_DIR / "current-drink.json"
DAILY_PATH = SUPPORT_DIR / "daily.json"

LIQUID_STATES = {0: "Standby", 1: "Empty", 2: "Filling", 3: "Cold", 4: "Cooling", 5: "Heating", 6: "At target", 7: "Warm"}
LIVE_LATENCY_S = 15  # live records arrive ~15-20 s after the event they describe
EPOCH_MIN = 1_000_000_000  # a record t above this is an absolute Unix time: the mug has been given a clock (fc540006)
NULL16 = 0x7FFF
MUG_CAPACITY_ML = 414  # 14 oz Mug 2; level is 0-30
CUP_MIN_LEVEL = 8  # below ~25 % full it was a rinse, not a drink
KIND_NAMES = {0x05: "state change", 0x07: "target set by app", 0x0F: "heater engaged", 0x10: "10-min sample", 0x15: "snapshot"}
BURST_WINDOW_S = 3  # packets this close together ...
BURST_MIN_PACKETS = 4  # ... and at least this many of them: a backlog flush, not a live record
HISTORY_KEEP_S = 2 * 86400  # app readings kept in memory for the drink in progress (finished drinks carry their own)


def is_burst(sorted_arrivals: list[float], ts: float) -> bool:
    """True if at least BURST_MIN_PACKETS packets (this one included) arrived within BURST_WINDOW_S of ts."""
    lo = bisect.bisect_left(sorted_arrivals, ts - BURST_WINDOW_S)
    hi = bisect.bisect_right(sorted_arrivals, ts + BURST_WINDOW_S)
    return hi - lo >= BURST_MIN_PACKETS


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a reader (or a crash) never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass
class Record:
    kind: int
    sub: int
    t: int
    payload: bytes
    arrived: float
    burst: bool = False  # part of a backlog flush rather than a live event
    raw_packets: list[str] = field(default_factory=list)
    t_abs: Optional[int] = None  # the mug's own Unix timestamp for this record, when it has a clock; t is then relative to the drink

    @property
    def name(self) -> str:
        return KIND_NAMES.get(self.kind, f"kind 0x{self.kind:02x}")

    def fields(self) -> dict[str, Any]:
        p = self.payload

        def i16(i: int) -> Optional[int]:
            if len(p) < i + 2:
                return None
            v = int.from_bytes(p[i : i + 2], "big")
            return None if v in (NULL16, 0xFFFF) else v

        out: dict[str, Any] = {}
        if self.kind == 0x05:
            out["state"] = LIQUID_STATES.get(self.sub, str(self.sub))
        elif self.kind in (0x07, 0x0F):
            v = i16(0)
            out["target_c"] = v / 100 if v is not None else None
        elif self.kind == 0x10:
            out["level"] = p[0] if p else None
            v = i16(1)
            out["temp_c"] = v / 100 if v is not None else None
        elif self.kind == 0x15:
            out["a_raw"] = int.from_bytes(p[2:4], "big") if len(p) >= 4 else None
            b = i16(4)
            out["b_c"] = b / 100 if b is not None else None
            out["tail_hex"] = p[8:].hex(" ") if len(p) > 8 else ""
        else:
            out["hex"] = p.hex(" ")
        return out

    def describe(self, temp: Callable[[Optional[float]], str]) -> str:
        f = self.fields()
        if self.kind == 0x05:
            return f"state → {f['state']}"
        if self.kind == 0x07:
            return f"app set target {temp(f['target_c'])}"
        if self.kind == 0x0F:
            return f"heater engaged, target {temp(f['target_c'])}"
        if self.kind == 0x10:
            return f"sample: level {f['level']}/30, {temp(f['temp_c'])}"
        if self.kind == 0x15:
            return f"snapshot: A={f['a_raw']}, B={f['b_c']} °C, tail {f['tail_hex']}"
        return f"unknown {f}"

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "sub": self.sub, "t": self.t, "t_abs": self.t_abs, "payload": self.payload.hex(), "arrived": self.arrived, "burst": self.burst, "raw": self.raw_packets}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Record":
        return cls(d["kind"], d["sub"], d["t"], bytes.fromhex(d["payload"]), d["arrived"], d.get("burst", False), d.get("raw", []), d.get("t_abs"))


class Reassembler:
    """Feed raw notifications one at a time; get complete records back."""

    def __init__(self) -> None:
        self.pending: Optional[tuple[bytes, float, list[str]]] = None

    def feed(self, raw: bytes, arrived: float, burst: bool) -> list[Record]:
        if not raw:
            return []
        hexs = raw.hex(" ")
        ptype = raw[0]
        if ptype == 0x05 and len(raw) == 1:
            return []  # end of batch
        if ptype == 0x01 and len(raw) >= 3:
            return [self._make(raw[3 : 3 + raw[2]], arrived, burst, [hexs])]
        if ptype == 0x02 and len(raw) >= 3:
            self.pending = (raw[3 : 3 + raw[2]], arrived, [hexs])
            return []
        if ptype == 0x04 and len(raw) >= 3 and self.pending is not None:
            body = self.pending[0] + raw[3 : 3 + raw[2]]
            rec = self._make(body, self.pending[1], burst, self.pending[2] + [hexs])
            self.pending = None
            return [rec]
        return [Record(kind=-1, sub=0, t=0, payload=raw, arrived=arrived, burst=burst, raw_packets=[hexs])]

    @staticmethod
    def _make(body: bytes, arrived: float, burst: bool, packets: list[str]) -> Record:
        if len(body) < 8 or body[0] != 0x00:
            return Record(kind=-1, sub=0, t=0, payload=body, arrived=arrived, burst=burst, raw_packets=packets)
        return Record(kind=body[1], sub=body[2], t=int.from_bytes(body[3:7], "big"), payload=body[8:], arrived=arrived, burst=burst, raw_packets=packets)


def reassemble_rows(rows: list[dict[str, Any]]) -> list[Record]:
    arrivals = sorted(r["ts"] for r in rows)
    ra = Reassembler()
    out: list[Record] = []
    for row in rows:
        out += ra.feed(bytes.fromhex(row["hex"].replace(" ", "")), row["ts"], is_burst(arrivals, row["ts"]))
    return out


# --------------------------------------------------------------------------- #
# Drinks
# --------------------------------------------------------------------------- #


def starts_new_drink(r: Record, last_t: int) -> bool:
    """A 'filling' state record starts a drink. Without a clock the mug's counter restarts at each pour
    (so t < 60, or t running backwards, means a new drink); with a clock every t is absolute."""
    if r.t > EPOCH_MIN:
        return r.kind == 0x05 and r.sub == 2
    return (r.kind == 0x05 and r.sub == 2 and r.t < 60) or (0 <= last_t < EPOCH_MIN and r.t < last_t - 30)


@dataclass
class Drink:
    records: list[Record] = field(default_factory=list)
    t0: Optional[float] = None  # epoch of the pour
    anchor_source: str = "none"  # "app" (saw the pour), "clock" (the mug's own timestamps), "live" (record latency), "none"
    clock_base: Optional[int] = None  # mug-clock epoch that this drink's relative t values count from
    ended_at: Optional[float] = None
    end_reason: str = ""
    finished: bool = False

    # ---- building ----
    def add(self, r: Record) -> None:
        if r.t > EPOCH_MIN and r.t_abs is None:
            if self.clock_base is None:
                # first stamped record: the pour itself, unless the clock was set mid-drink, in which case keep t continuous
                self.clock_base = r.t if not self.records else r.t - (self.last_t + LIVE_LATENCY_S)
            r.t_abs, r.t = r.t, r.t - self.clock_base
        self.records.append(r)

    def reanchor(self, arrivals: list[float]) -> None:
        """Classify records as backlog-flush vs live from packet timing, then anchor t0 from a live one.
        Done lazily because the first packets of a flush look 'live' until the rest arrive."""
        if self.anchor_source == "app":
            return
        arrivals = sorted(arrivals)
        for r in self.records:
            r.burst = is_burst(arrivals, r.arrived)
        if self.clock_base is not None:
            self.t0, self.anchor_source = float(self.clock_base), "clock"
            return
        live: list[Record] = []
        for r in self.records:
            if not r.burst and r.kind >= 0:
                live.append(r)
        if live:
            self.t0 = min(r.arrived - r.t - LIVE_LATENCY_S for r in live)
            self.anchor_source = "live"
        else:
            self.t0, self.anchor_source = None, "none"

    @property
    def last_t(self) -> int:
        return max((r.t for r in self.records), default=-1)

    @property
    def id(self) -> str:
        if self.t0:
            return datetime.fromtimestamp(self.t0).strftime("%Y%m%d-%H%M%S")
        first = min((r.arrived for r in self.records), default=0)
        return "unanchored-" + datetime.fromtimestamp(first).strftime("%Y%m%d-%H%M%S")

    # ---- metrics ----
    def mug_samples(self) -> list[tuple[int, Optional[int], Optional[float]]]:
        return [(r.t, r.fields()["level"], r.fields()["temp_c"]) for r in self.records if r.kind == 0x10]

    def target_series(self) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        for r in sorted(self.records, key=lambda r: r.t):
            if r.kind in (0x07, 0x0F):
                tc = r.fields().get("target_c")
                if tc:
                    out.append((r.t, tc))
        return out

    def to_row(self, history: list[dict[str, Any]], now: Optional[float] = None) -> dict[str, Any]:
        now = now or datetime.now().timestamp()
        end = self.ended_at or ((self.t0 + self.last_t) if (self.finished and self.t0) else now)
        duration = int(max(self.last_t, (end - self.t0) if self.t0 else 0))
        app = [h for h in history if self.t0 and self.t0 - 60 <= h["ts"] <= end] if self.t0 else []
        levels_app = [(h["ts"] - self.t0, h["level"]) for h in app if h.get("level") is not None]
        temps_app = [(h["ts"] - self.t0, h["current_c"], h.get("target_c")) for h in app if h.get("current_c")]
        mug = self.mug_samples()

        # level: prefer the app's readings, fall back to the mug's 10-minute samples
        if levels_app:
            start_level = max(lv for t, lv in levels_app if t <= 600) if any(t <= 600 for t, _ in levels_app) else levels_app[0][1]
            end_level = levels_app[-1][1]
        elif mug:
            start_level, end_level = mug[0][1], mug[-1][1]
        else:
            start_level = end_level = None
        consumed_ml = round((start_level - end_level) / 30 * MUG_CAPACITY_ML) if start_level is not None and end_level is not None and start_level >= end_level else None

        # time to target: app state stream first, then the mug's "reached target" state record
        reached: Optional[int] = None
        for h in app:
            if h.get("state") == "TARGET_TEMPERATURE" and h["ts"] > self.t0 + 30:
                reached = int(h["ts"] - self.t0)
                break
        if reached is None:
            reached = next((r.t for r in self.records if r.kind == 0x05 and r.sub == 6), None)

        targets = self.target_series()
        first_target = targets[0][1] if targets else None
        last_temp = temps_app[-1][1] if temps_app else (mug[-1][2] if mug else None)
        snap = next((r.fields() for r in self.records if r.kind == 0x15), None)
        is_cup = start_level is not None and start_level >= CUP_MIN_LEVEL
        abandoned = self.finished and end_level is not None and end_level >= CUP_MIN_LEVEL
        return {
            "id": self.id,
            "poured_at": datetime.fromtimestamp(self.t0).isoformat(timespec="seconds") if self.t0 else None,
            "poured_ts": self.t0,
            "anchor": self.anchor_source,
            "ended_at": datetime.fromtimestamp(self.ended_at).isoformat(timespec="seconds") if self.ended_at else None,
            "end_reason": self.end_reason,
            "in_progress": not self.finished,
            "duration_s": duration,
            "first_target_c": first_target,
            "targets_set": [(t, tc) for t, tc in ((r.t, r.fields().get("target_c")) for r in self.records if r.kind == 0x07)],
            "set_downs": sum(1 for r in self.records if r.kind == 0x0F),
            "reached_target_after_s": reached,
            "start_level": start_level,
            "end_level": end_level,
            "consumed_ml": consumed_ml,
            "last_temp_c": last_temp,
            "is_cup": is_cup,
            "abandoned": abandoned,
            "snapshot": {"a": snap.get("a_raw"), "b_c": snap.get("b_c")} if snap else None,
            "mug_samples": mug,
            "app_temps": [(round(t), c, tg) for t, c, tg in temps_app][-720:],
            "app_levels": [(round(t), lv) for t, lv in levels_app][-720:],
            "target_series": targets,
            "records": [r.to_json() for r in self.records],
        }


class DrinkTracker:
    """Holds the drink in progress, finalises drinks to drinks.jsonl, writes daily.json."""

    def __init__(self, drinks_path: Path = DRINKS_PATH, current_path: Path = CURRENT_PATH, daily_path: Path = DAILY_PATH, history_path: Path = HISTORY_PATH) -> None:
        self.drinks_path, self.current_path, self.daily_path, self.history_path = drinks_path, current_path, daily_path, history_path
        self.reassembler = Reassembler()
        self.current: Optional[Drink] = None
        self.rows: list[dict[str, Any]] = self._load_rows()
        self.arrivals: list[float] = []  # packet arrival times (recent), to tell a backlog flush from a live record
        self._history: list[dict[str, Any]] = []
        self._history_pos = 0
        self._history_ino: Optional[int] = None

    # ---- persistence ----
    def _load_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            with open(self.drinks_path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn line (e.g. power loss mid-write) must not take the whole log down
                    if isinstance(row, dict) and "id" in row:
                        rows.append(row)
        except OSError:
            pass
        return rows

    def load_history(self) -> list[dict[str, Any]]:
        """The app's readings from the last HISTORY_KEEP_S. history.jsonl only ever grows, and this runs on every
        packet and every menu refresh, so read it incrementally: only the bytes appended since the last call."""
        try:
            st = os.stat(self.history_path)
        except OSError:
            self._history, self._history_pos, self._history_ino = [], 0, None
            return []
        if st.st_ino != self._history_ino or st.st_size < self._history_pos:  # replaced or truncated: start over
            self._history, self._history_pos, self._history_ino = [], 0, st.st_ino
        if st.st_size > self._history_pos:
            try:
                with open(self.history_path, "rb") as f:
                    f.seek(self._history_pos)
                    chunk = f.read()
            except OSError:
                return self._history
            end = chunk.rfind(b"\n") + 1  # leave a half-written last line for next time
            self._history_pos += end
            for line in chunk[:end].splitlines():
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(row, dict) and isinstance(row.get("ts"), (int, float)):
                    self._history.append(row)
        cutoff = datetime.now().timestamp() - HISTORY_KEEP_S
        if self._history and self._history[0]["ts"] < cutoff:
            self._history = [h for h in self._history if h["ts"] >= cutoff]
        return self._history

    def _append_row(self, row: dict[str, Any]) -> None:
        self.rows = [r for r in self.rows if r["id"] != row["id"]] + [row]
        write_atomic(self.drinks_path, "".join(json.dumps(r) + "\n" for r in self.rows))

    def save_state(self) -> None:
        """Write the in-progress drink and the daily summary (cheap; called after every change)."""
        self.drinks_path.parent.mkdir(parents=True, exist_ok=True)
        history = self.load_history()
        if self.current:
            self.current.reanchor(self.arrivals)
        if self.current and (self.current.records or self.current.t0):
            write_atomic(self.current_path, json.dumps(self.current.to_row(history)))
        else:
            try:
                self.current_path.unlink()
            except OSError:
                pass
        write_atomic(self.daily_path, json.dumps(self.daily_summary(history=history), indent=2))

    # ---- events from the app ----
    def feed_packet(self, raw: bytes, arrived: float) -> list[Record]:
        self.arrivals = [t for t in self.arrivals if arrived - t <= 600] + [arrived]
        records = self.reassembler.feed(raw, arrived, False)
        for r in records:
            self._ingest(r)
        if records:
            self.save_state()
        return records

    def _ingest(self, r: Record) -> None:
        if r.kind < 0:
            return
        if self.current is None:
            self.current = Drink()
        elif self.current.records and starts_new_drink(r, self.current.last_t):
            # (no records yet = the app just marked this pour itself; the mug's "filling" record belongs to it)
            # A backlog can carry several finished drinks; a live "filling" record means a fresh pour.
            self.finalize("next pour", None)
            self.current = Drink()
        self.current.add(r)

    def mark_pour(self, ts: float) -> None:
        """The app saw the mug go Empty → Filling/Heating: the most trustworthy pour timestamp."""
        if self.current and self.current.records and self.current.ended_at is None:
            # If the current drink is young (< 3 min by its own clock) it IS this pour; otherwise a new one begins.
            young = self.current.last_t < 180 and (self.current.t0 is None or abs(self.current.t0 - ts) < 180)
            if not young:
                self.finalize("next pour", ts)
                self.current = Drink()
        elif self.current is None:
            self.current = Drink()
        self.current.t0 = ts
        self.current.anchor_source = "app"
        self.save_state()

    def mark_empty(self, ts: float) -> None:
        if self.current and (self.current.records or self.current.t0):
            self.finalize("emptied", ts)
            self.current = None
            self.save_state()

    def finalize(self, reason: str, ended_at: Optional[float]) -> None:
        if not self.current or not (self.current.records or self.current.t0):
            return
        self.current.reanchor(self.arrivals)
        self.current.finished = True
        self.current.ended_at = ended_at  # None = unknown (backlog drink); to_row uses the mug's own clock then
        self.current.end_reason = reason
        self._append_row(self.current.to_row(self.load_history()))

    # ---- summaries ----
    def all_rows(self, history: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
        rows = list(self.rows)
        if self.current:
            self.current.reanchor(self.arrivals)
        if self.current and (self.current.records or self.current.t0):
            rows.append(self.current.to_row(history if history is not None else self.load_history()))
        return sorted(rows, key=lambda r: r.get("poured_ts") or 0)

    def daily_summary(self, day: Optional[datetime] = None, history: Optional[list[dict[str, Any]]] = None, rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        day = day or datetime.now()
        start = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        end = start + 86400
        rows = [r for r in (rows if rows is not None else self.all_rows(history)) if r.get("poured_ts") and start <= r["poured_ts"] < end]
        cups = [r for r in rows if r["is_cup"]]
        ml = sum(r["consumed_ml"] or 0 for r in cups)
        finished = [r for r in cups if not r["in_progress"]]
        return {
            "date": day.strftime("%Y-%m-%d"),
            "cups": len(cups),
            "consumed_ml": ml,
            "first_pour": min((r["poured_at"] for r in cups), default=None),
            "last_pour": max((r["poured_at"] for r in cups), default=None),
            "avg_duration_s": round(sum(r["duration_s"] for r in finished) / len(finished)) if finished else None,
            "avg_time_to_target_s": round(sum(r["reached_target_after_s"] for r in finished if r["reached_target_after_s"]) / max(1, sum(1 for r in finished if r["reached_target_after_s"]))) if any(r["reached_target_after_s"] for r in finished) else None,
            "abandoned": sum(1 for r in cups if r["abandoned"]),
            "in_progress": any(r["in_progress"] for r in cups),
            "drinks": [{k: r[k] for k in ("id", "poured_at", "duration_s", "consumed_ml", "reached_target_after_s", "abandoned", "in_progress")} for r in cups],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def today_line(self, use_f: bool = True) -> str:
        s = self.daily_summary()
        if not s["cups"]:
            return "Today: no cups yet"
        parts = [f"Today: {s['cups']} cup{'s' if s['cups'] != 1 else ''}"]
        if s["consumed_ml"]:
            parts.append(f"{s['consumed_ml']} ml" if not use_f else f"{s['consumed_ml'] / 29.57:.0f} oz")
        if s["last_pour"]:
            parts.append(f"poured {s['last_pour'][11:16]}")
        return " · ".join(parts)

    def day_line(self, summary: dict[str, Any], label: str, use_f: bool = True) -> str:
        """One sentence for a briefing: 'Yesterday: 3 cups · 41 oz · 07:12–15:40 · ~22 min to target · 1 left to go cold'."""
        s = summary
        if not s["cups"]:
            return f"{label}: no cups"
        parts = [f"{label}: {s['cups']} cup{'s' if s['cups'] != 1 else ''}"]
        if s["consumed_ml"]:
            parts.append(f"{s['consumed_ml']} ml" if not use_f else f"{s['consumed_ml'] / 29.57:.0f} oz")
        if s["first_pour"]:
            span = s["first_pour"][11:16]
            if s["last_pour"] and s["last_pour"] != s["first_pour"]:
                span += f"–{s['last_pour'][11:16]}"
            parts.append(span)
        if s["avg_time_to_target_s"]:
            parts.append(f"~{fmt_dur(s['avg_time_to_target_s'])} to target")
        if s["abandoned"]:
            parts.append(f"{s['abandoned']} left to go cold")
        if s["in_progress"]:
            parts.append("one in progress")
        return " · ".join(parts)

    def briefing(self, use_f: bool = True, rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """Yesterday and today, as data and as lines, for morning summaries and scripts."""
        rows = rows if rows is not None else self.all_rows()
        today = self.daily_summary(rows=rows)
        yesterday = self.daily_summary(day=datetime.now() - timedelta(days=1), rows=rows)
        week_rows = [r for r in rows if r["is_cup"] and r.get("poured_ts") and r["poured_ts"] >= datetime.now().timestamp() - 7 * 86400]
        return {
            "today": today,
            "yesterday": yesterday,
            "today_line": self.day_line(today, "Today", use_f),
            "yesterday_line": self.day_line(yesterday, "Yesterday", use_f),
            "week_cups": len(week_rows),
            "week_ml": sum(r["consumed_ml"] or 0 for r in week_rows),
        }


def read_history(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    """Every reading in history.jsonl (for rebuilding old drinks; the tracker itself only keeps recent ones)."""
    out: list[dict[str, Any]] = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("ts"), (int, float)):
                    out.append(row)
    except OSError:
        pass
    return out


def drinks_from_capture(rows: list[dict[str, Any]], history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild every drink from the raw capture (validation path; ignores drinks.jsonl)."""
    tracker = DrinkTracker(drinks_path=Path("/dev/null"), current_path=Path("/dev/null"), daily_path=Path("/dev/null"))
    tracker.rows = []
    tracker.save_state = lambda: None  # type: ignore[method-assign]
    tracker._append_row = lambda row: tracker.rows.append(row)  # type: ignore[method-assign]
    tracker.load_history = lambda: history  # type: ignore[method-assign]
    tracker.arrivals = [r["ts"] for r in rows]
    for r in reassemble_rows(rows):
        tracker._ingest(r)
    if tracker.current:
        tracker.current.reanchor(tracker.arrivals)
    return tracker.all_rows(history)



INSTRUMENT_CSS = r""":root { color-scheme: dark;
  --bg: #101211; --panel: #171a18; --panel-2: #1d211e; --line: #2a2f2b; --line-2: #363c37;
  --ink: #e9ece8; --ink-2: #a7ada6; --ink-3: #9aa199;  /* ink-3 >= 5:1 on every surface (labels, ticks, units) */
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --amber: #e0a92a; --live: #2fd27a;
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", Inter, system-ui, sans-serif; }
* { box-sizing: border-box; }
html { background: var(--bg); }
body { margin: 0; padding: 0 0 80px; color: var(--ink); font: 13px/1.5 var(--sans);
  background: radial-gradient(1200px 500px at 50% -10%, #1b201d 0%, var(--bg) 60%); }
main { max-width: 1200px; margin: 0 auto; padding: 0 24px; }
.rl { display: block; font: 500 11px/1.2 var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--ink-3); }
:focus-visible { outline: 2px solid var(--amber); outline-offset: 2px; border-radius: 4px; }
.rv, .tv, .g-value { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.u { color: var(--ink-3); font-size: .7em; letter-spacing: .04em; }
.dim { color: var(--ink-3); } .mono { font-family: var(--mono); } .small { font-size: 11px; }
/* masthead */
.mast { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px 24px; padding: 26px 0 18px; border-bottom: 1px solid var(--line); margin-bottom: 20px; }
.mast h1 { margin: 0; font: 600 13px/1 var(--mono); letter-spacing: .28em; text-transform: uppercase; color: var(--ink); }
.mast h1 b { color: var(--amber); font-weight: 600; }
.mast .meta { font: 11px/1.4 var(--mono); color: var(--ink-3); letter-spacing: .04em; }
.mast .meta i { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--ink-3); margin-inline-end: 6px; vertical-align: 1px; }
.mast .meta i.live { background: var(--live); box-shadow: 0 0 8px var(--live); }
/* hero */
.hero { display: grid; grid-template-columns: auto 1fr; gap: 12px 32px; align-items: center; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px 26px; margin-bottom: 14px;
  box-shadow: inset 0 1px 0 #ffffff08, 0 20px 40px -30px #000; }
.hero-gauges { display: flex; gap: 8px; }
.gauge { width: 180px; height: 168px; }
.g-track { fill: none; stroke: var(--line-2); stroke-width: 8; stroke-linecap: round; }
.g-fill { fill: none; stroke: var(--s1); stroke-width: 8; stroke-linecap: round; filter: drop-shadow(0 0 6px #3987e577); }
.g-dot { fill: var(--ink); stroke: var(--panel); stroke-width: 2; }
.g-target { stroke: var(--s2); stroke-width: 3; stroke-linecap: round; }
.g-value { font-size: 30px; font-weight: 600; fill: var(--ink); letter-spacing: -.02em; }
.g-unit { font: 500 11px var(--mono); fill: var(--ink-3); letter-spacing: .12em; }
.g-label { font: 500 11px var(--mono); fill: var(--ink-3); letter-spacing: .14em; text-transform: uppercase; }
.hero-readouts { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 14px 20px; }
.readout .rv { display: block; font-size: 22px; font-weight: 600; letter-spacing: -.01em; margin-top: 3px; }
.readouts .readout .rv { font-size: 17px; }
.state { color: var(--ink-2); font-size: 14px !important; letter-spacing: .12em; } .state.live { color: var(--live); text-shadow: 0 0 10px #2fd27a66; }
/* tiles */
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 22px; }
.tile { background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px 10px; }
.tile .tv { display: block; font-size: 24px; font-weight: 600; margin-top: 4px; letter-spacing: -.02em; }
/* panels */
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px 22px 16px; margin-bottom: 14px; box-shadow: inset 0 1px 0 #ffffff08; }
.panel-h { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px 28px; margin-bottom: 14px; align-items: flex-start; }
.readouts { display: flex; flex-wrap: wrap; gap: 10px 26px; }
.panel-title h2 { margin: 3px 0 6px; font: 600 18px/1.2 var(--mono); letter-spacing: -.01em; }
.tag { display: inline-block; font: 500 11px/1 var(--mono); letter-spacing: .12em; text-transform: uppercase; padding: 4px 8px; border-radius: 4px; border: 1px solid var(--line-2); color: var(--ink-2); margin-inline-end: 6px; }
.tag.live { color: var(--live); border-color: #2fd27a55; background: #2fd27a12; } .tag.dimtag { color: var(--ink-3); }
.tag-mini { font: 500 11px var(--mono); letter-spacing: .12em; color: var(--live); margin-inline-start: 6px; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 18px; }
figure { margin: 0; background: #0f1210; border: 1px solid var(--line); border-radius: 10px; padding: 10px 10px 4px; }
figcaption { font: 500 11px var(--mono); letter-spacing: .12em; text-transform: uppercase; color: var(--ink-3); margin: 0 0 6px; margin-inline-start: 4px; }
.chart { width: 100%; height: auto; display: block; }
.grid { stroke: var(--line); stroke-width: 1; }
.tick { font: 11px var(--mono); fill: var(--ink-3); }
.series-1 { fill: none; stroke: var(--s1); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; filter: drop-shadow(0 0 4px #3987e566); }
.series-2 { fill: none; stroke: var(--s2); stroke-width: 2; stroke-linejoin: round; }
.series-3 { fill: none; stroke: var(--s3); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; filter: drop-shadow(0 0 4px #199e7066); }
.dashed { stroke-dasharray: 5 4; }
.dot-1 { fill: var(--s1); stroke: #0f1210; stroke-width: 2; } .dot-3 { fill: var(--s3); stroke: #0f1210; stroke-width: 2; }
.key { display: inline-block; width: 16px; height: 0; border-top: 2px solid; vertical-align: middle; margin: 0 6px 0 2px; }
.key-1 { border-color: var(--s1); } .key-2 { border-color: var(--s2); margin-inline-start: 12px; } .key-3 { border-color: var(--s3); } .key.dashed { border-top-style: dashed; }
details { margin-top: 12px; } summary { cursor: pointer; font: 500 11px var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--ink-3); }
summary:hover { color: var(--ink-2); }
.tablewrap { overflow-x: auto; margin-top: 10px; border: 1px solid var(--line); border-radius: 8px; background: #0f1210; }
table { border-collapse: collapse; width: 100%; font: 12px/1.4 var(--mono); }
th { text-align: start; color: var(--ink-3); font-weight: 500; font-size: 11px; letter-spacing: .12em; text-transform: uppercase; padding: 8px 10px; border-bottom: 1px solid var(--line); }
td { padding: 5px 10px; border-bottom: 1px solid #1c201d; vertical-align: top; white-space: nowrap; }
tr:nth-child(even) td { background: #ffffff04; }
td.num { font-variant-numeric: tabular-nums; }
td.hex { color: #6d8f75; font-size: 11px; }
td.k07 { color: var(--amber); } td.k05 { color: var(--ink); } td.k0f { color: var(--ink-2); } td.k10 { color: var(--s1); } td.k15 { color: #c9a0dc; }
.foot { margin-top: 30px; font: 12px/1.6 var(--mono); color: var(--ink-3); letter-spacing: .04em; }
"""

# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def fmt_dur(s: Optional[int]) -> str:
    if s is None:
        return "—"
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m} min"


def svg_line_chart(points: list[tuple[float, float]], secondary: list[tuple[float, float]], y_label: str, unit_fmt, width: int = 560, height: int = 180, y_min: Optional[float] = None, y_max: Optional[float] = None, duration: int = 1, dots: bool = True, cls: str = "1", aria: Optional[str] = None) -> str:
    if not points and not secondary:
        return '<p class="muted">no samples</p>'
    left, right, top, bottom = 48, 16, 12, 28
    all_y = [y for _, y in points] + [y for _, y in secondary]
    pad = 1 if points else 5
    lo = y_min if y_min is not None else min(all_y) - pad
    hi = y_max if y_max is not None else max(all_y) + pad
    if hi - lo < 1:
        hi = lo + 1
    span = max(duration, 60)

    def X(t: float) -> float:
        return left + min(t, span) / span * (width - left - right)

    def Y(v: float) -> float:
        return height - bottom - (v - lo) / (hi - lo) * (height - top - bottom)

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{html.escape(aria or y_label)}">']
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        y = Y(v)
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" class="tick">{unit_fmt(v)}</text>')
    for i in range(5):
        t = span * i / 4
        parts.append(f'<text x="{X(t):.1f}" y="{height - 8}" text-anchor="middle" class="tick">{int(t // 60)}m</text>')
    if secondary:
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(t):.1f},{Y(v):.1f}" for i, (t, v) in enumerate(secondary))
        parts.append(f'<path d="{d}" class="series-2 dashed"/>')
    if points:
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(t):.1f},{Y(v):.1f}" for i, (t, v) in enumerate(points))
        parts.append(f'<path d="{d}" class="series-{cls}"/>')
        if dots:
            for t, v in points:
                parts.append(f'<circle cx="{X(t):.1f}" cy="{Y(v):.1f}" r="4" class="dot-{cls}"><title>{fmt_dur(int(t))}: {unit_fmt(v)}</title></circle>')
    parts.append("</svg>")
    return "".join(parts)


def step_series(targets: list[tuple[int, float]], duration: int) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for t, v in targets:
        if out:
            out.append((t, out[-1][1]))
        out.append((t, v))
    if out:
        out.append((duration, out[-1][1]))
    return out


def svg_gauge(current: Optional[float], target: Optional[float], lo: float, hi: float, label: str, unit: str, aria: Optional[str] = None) -> str:
    """270° arc gauge: current value as the filled arc, target as a tick."""
    import math

    cx, cy, r = 90, 92, 70
    a0, a1 = 135, 405  # degrees, clockwise from +x, sweeping through the bottom

    def pt(angle: float, radius: float) -> tuple[float, float]:
        rad = math.radians(angle)
        return cx + radius * math.cos(rad), cy + radius * math.sin(rad)

    def arc(from_deg: float, to_deg: float, radius: float) -> str:
        x0, y0 = pt(from_deg, radius)
        x1, y1 = pt(to_deg, radius)
        large = 1 if to_deg - from_deg > 180 else 0
        return f"M{x0:.1f},{y0:.1f} A{radius},{radius} 0 {large} 1 {x1:.1f},{y1:.1f}"

    def ang(v: float) -> float:
        v = max(lo, min(hi, v))
        return a0 + (v - lo) / (hi - lo) * (a1 - a0)

    parts = [f'<svg viewBox="0 0 180 168" class="gauge" role="img" aria-label="{html.escape(aria or label)}">']
    parts.append(f'<path d="{arc(a0, a1, r)}" class="g-track"/>')
    if current is not None:
        parts.append(f'<path d="{arc(a0, ang(current), r)}" class="g-fill"/>')
        x, y = pt(ang(current), r)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" class="g-dot"/>')
    if target is not None:
        x0, y0 = pt(ang(target), r - 11)
        x1, y1 = pt(ang(target), r + 11)
        parts.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" class="g-target"/>')
    parts.append(f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" class="g-value">{f"{current:.0f}" if current is not None else "—"}</text>')
    parts.append(f'<text x="{cx}" y="{cy + 22}" text-anchor="middle" class="g-unit">{unit}</text>')
    parts.append(f'<text x="{cx}" y="{cy + 66}" text-anchor="middle" class="g-label">{html.escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_html(rows: list[dict[str, Any]], daily: dict[str, Any], use_f: bool = True, capture_packets: int = 0, mug: Optional[dict[str, Any]] = None) -> str:
    unit = "°F" if use_f else "°C"

    def temp(c: Optional[float], d: int = 0) -> str:
        if c is None:
            return "—"
        return f"{c_to_f(c):.{d}f}{unit}" if use_f else f"{c:.1f}{unit}"

    def conv(c: float) -> float:
        return c_to_f(c) if use_f else c

    def num(v: Any, suffix: str = "") -> str:
        return f'{v}<span class="u">{suffix}</span>' if v not in (None, "—") else '<span class="dim">—</span>'

    cups = [r for r in rows if r["is_cup"]]
    live = next((r for r in reversed(rows) if r["in_progress"]), None)
    latest = live or (rows[-1] if rows else None)

    # ---- hero: the drink in progress (or the last one) as an instrument cluster ----
    if latest:
        cur_c = latest["last_temp_c"]
        tgt_c = latest["first_target_c"]
        if latest["target_series"]:
            tgt_c = latest["target_series"][-1][1]
        lo_c, hi_c = (20, 70)
        g_temp = svg_gauge(conv(cur_c) if cur_c is not None else None, conv(tgt_c) if tgt_c else None, conv(lo_c), conv(hi_c), "drink temperature", unit)
        lvl = latest["end_level"]
        g_level = svg_gauge(lvl / 30 * 100 if lvl is not None else None, None, 0, 100, "liquid level", "%")
        poured = datetime.fromisoformat(latest["poured_at"]).strftime("%H:%M") if latest["poured_at"] else "—"
        state = "IN PROGRESS" if latest["in_progress"] else ("ABANDONED" if latest["abandoned"] else (latest["end_reason"] or "finished").upper())
        hero = f"""
    <section class="hero">
      <div class="hero-gauges">{g_temp}{g_level}</div>
      <div class="hero-readouts">
        <div class="readout"><span class="rl">{'poured' if latest['in_progress'] else 'last poured'}</span><span class="rv">{poured}</span></div>
        <div class="readout"><span class="rl">elapsed</span><span class="rv">{fmt_dur(latest['duration_s'])}</span></div>
        <div class="readout"><span class="rl">ready after</span><span class="rv">{fmt_dur(latest['reached_target_after_s'])}</span></div>
        <div class="readout"><span class="rl">set-downs</span><span class="rv">{latest['set_downs']}</span></div>
        <div class="readout"><span class="rl">consumed</span><span class="rv">{num(latest['consumed_ml'], ' ml')}</span></div>
        <div class="readout"><span class="rl">state</span><span class="rv state {'live' if latest['in_progress'] else ''}">{state}</span></div>
      </div>
    </section>"""
    else:
        hero = '<section class="hero"><p class="dim">No drinks logged yet. Pour something.</p></section>'

    tiles = f"""
    <section class="tiles">
      <div class="tile"><span class="rl">cups today</span><span class="tv">{daily['cups']}</span></div>
      <div class="tile"><span class="rl">consumed today</span><span class="tv">{num(daily['consumed_ml'] or 0, ' ml')}</span></div>
      <div class="tile"><span class="rl">avg time to target</span><span class="tv">{fmt_dur(daily['avg_time_to_target_s'])}</span></div>
      <div class="tile"><span class="rl">avg drink</span><span class="tv">{fmt_dur(daily['avg_duration_s'])}</span></div>
      <div class="tile"><span class="rl">abandoned</span><span class="tv">{daily['abandoned']}</span></div>
      <div class="tile"><span class="rl">log entries</span><span class="tv">{len(cups)}<span class="u"> cups / {len(rows)}</span></span></div>
    </section>"""

    cards = []
    for r in reversed(rows):
        duration = max(r["duration_s"], 60)
        temps = [(t, conv(c)) for t, c, _ in r["app_temps"]] or [(t, conv(c)) for t, _, c in r["mug_samples"] if c is not None]
        fine = bool(r["app_temps"])
        levels = [(t, lv / 30 * 100) for t, lv in r["app_levels"]] or [(t, lv / 30 * 100) for t, lv, _ in r["mug_samples"] if lv is not None]
        targets = [(t, conv(v)) for t, v in step_series(r["target_series"], duration)]
        poured = datetime.fromisoformat(r["poured_at"]).strftime("%a %-d %b · %H:%M") if r["poured_at"] else "unanchored backlog"
        state = "in progress" if r["in_progress"] else ("abandoned" if r["abandoned"] else r["end_reason"] or "finished")
        tags = f'<span class="tag {"live" if r["in_progress"] else ""}">{state}</span>'
        if not r["is_cup"]:
            tags += '<span class="tag dimtag">not a cup</span>'
        tags += f'<span class="tag dimtag">{r["anchor"]}-anchored</span>'
        snap = r.get("snapshot")
        snap_text = f"POUR SNAPSHOT  A={snap['a']}  B={snap['b_c']}°C   (battery % / battery temp, unconfirmed)" if snap else ""
        records = [Record.from_json(x) for x in r["records"]]
        t0 = r.get("poured_ts")
        tape = "".join(
            f"<tr><td class='num'>{fmt_dur(x.t)}</td><td class='num dim'>{datetime.fromtimestamp(t0 + x.t).strftime('%H:%M:%S') if t0 else '—'}</td>"
            f"<td class='k{x.kind:02x}'>{html.escape(x.describe(temp))}{'' if x.burst else ' <span class=tag-mini>LIVE</span>'}</td><td class='hex'>{html.escape(' · '.join(x.raw_packets))}</td></tr>"
            for x in sorted(records, key=lambda x: x.t)
        )
        cards.append(f"""
    <section class="panel">
      <header class="panel-h">
        <div class="panel-title"><span class="rl">drink</span><h2>{poured}</h2>{tags}</div>
        <div class="readouts">
          <div class="readout"><span class="rl">target</span><span class="rv">{temp(r['first_target_c'])}</span></div>
          <div class="readout"><span class="rl">ready after</span><span class="rv">{fmt_dur(r['reached_target_after_s'])}</span></div>
          <div class="readout"><span class="rl">duration</span><span class="rv">{fmt_dur(r['duration_s'])}</span></div>
          <div class="readout"><span class="rl">set-downs</span><span class="rv">{r['set_downs']}</span></div>
          <div class="readout"><span class="rl">level</span><span class="rv">{r['start_level'] if r['start_level'] is not None else '—'}<span class="u"> → </span>{r['end_level'] if r['end_level'] is not None else '—'}</span></div>
          <div class="readout"><span class="rl">consumed</span><span class="rv">{num(r['consumed_ml'], ' ml')}</span></div>
        </div>
      </header>
      <div class="charts">
        <figure>
          <figcaption><span class="key key-1"></span>drink temperature <span class="key key-2 dashed"></span>target{' <span class="dim">· app readings</span>' if fine else ' <span class="dim">· mug samples</span>'}</figcaption>
          {svg_line_chart(temps, targets, "temperature", lambda v: f"{v:.0f}{unit}", duration=duration, dots=not fine)}
        </figure>
        <figure>
          <figcaption><span class="key key-3"></span>liquid level</figcaption>
          {svg_line_chart(levels, [], "liquid level", lambda v: f"{v:.0f}%", y_min=0, y_max=100, duration=duration, dots=not fine, cls="3")}
        </figure>
      </div>
      {f'<p class="mono dim small">{html.escape(snap_text)}</p>' if snap_text else ''}
      <details><summary>tape · {len(records)} mug records</summary>
        <div class="tablewrap"><table><thead><tr><th>t+</th><th>clock</th><th>record</th><th>bytes</th></tr></thead><tbody>{tape}</tbody></table></div>
      </details>
    </section>""")

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    mug_line = ""
    if mug:
        mug_line = " · ".join(str(v) for v in (mug.get("name"), mug.get("model"), f"fw {mug.get('firmware')}" if mug.get("firmware") else None, mug.get("serial")) if v)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ember Drink Log</title>
<style>
{INSTRUMENT_CSS}</style>
</head>
<body>
<main>
  <header class="mast">
    <h1><b>Ember</b> · Drink Log</h1>
    <div class="meta">{'<i></i>LIVE · ' if live else ''}{html.escape(mug_line) + ' · ' if mug_line else ''}{daily['date']} · generated {generated}{f' · {capture_packets} packets' if capture_packets else ''}</div>
  </header>
  {hero}
  {tiles}
  {''.join(cards)}
  <p class="foot">Sources: the mug's own log (statistics characteristic fc540013, decoded in docs/statistics-stream.md) and the app's readings. Consumption assumes 30 level units = {MUG_CAPACITY_ML} ml; entries below {CUP_MIN_LEVEL}/30 at the pour are not counted as cups.</p>
</main>
</body>
</html>
"""



PANEL_CSS = r"""
body { padding: 0 0 14px; background: #101211; font-size: 12px; }
main { padding: 0 16px; max-width: none; }
.mast { padding: 14px 0 10px; margin-bottom: 12px; }
.mast h1 { font-size: 11px; letter-spacing: .24em; }
.mast .meta { font-size: 11px; }
.hero { grid-template-columns: 1fr; gap: 8px; padding: 12px 14px 8px; border-radius: 18px; }
.hero-gauges { justify-content: space-between; }
.gauge { width: 150px; height: 140px; }
.g-value { font-size: 26px; }
.hero-readouts { grid-template-columns: repeat(3, 1fr); gap: 10px 14px; margin-top: 4px; }
.readout .rv { font-size: 18px; }
.note { margin: 2px 0 4px; font: 11px/1.4 var(--mono); color: var(--ink-3); letter-spacing: .04em; }
.tiles { grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 12px; }
.tile { padding: 9px 11px 7px; border-radius: 10px; display: flex; flex-direction: column; }
.tile .rl { letter-spacing: .1em; } .tile .tv { margin-top: auto; }
.tile .tv { font-size: 19px; }
.panel { padding: 12px 14px 10px; margin-bottom: 12px; border-radius: 18px; }
.panel-title { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.panel-title h2 { font-size: 14px; }
.charts { grid-template-columns: 1fr; gap: 10px; }
figure { padding: 8px 8px 2px; border-radius: 6px; }
.tablewrap { border-radius: 6px; }
/* actions: presets + heating off, as real buttons */
.actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(84px, 1fr)); gap: 6px; margin: 0 0 12px; }
.actions button { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; min-height: 44px; padding: 8px 6px; cursor: pointer;
  font: 500 12px/1 var(--mono); letter-spacing: .06em; color: var(--ink); background: var(--panel-2); border: 1px solid var(--line-2); border-radius: 8px;
  transition-property: color, border-color, background-color, scale; transition-duration: 150ms; transition-timing-function: ease-out; }
.actions button .rl { font-size: 10.5px; color: var(--ink-3); }
.actions button:hover { border-color: var(--ink-3); }
.actions button:active { scale: .96; }
.actions button[aria-pressed="true"] { color: var(--amber); border-color: #e0a92a88; background: #e0a92a14; }
.actions button[aria-pressed="true"] .rl { color: var(--amber); }
.actions button[aria-pressed="true"]::after { content: "current"; font: 500 9.5px/1 var(--mono); letter-spacing: .12em; text-transform: uppercase; color: var(--amber); }
.more { font: 500 11px/1.2 var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--amber); text-decoration: none; white-space: nowrap; }
.more:hover { text-decoration: underline; text-underline-offset: 3px; }
.recent { width: 100%; }
.recent td, .recent th { padding: 6px 8px; }
.recent td:first-child { color: var(--ink); }
.recent td.empty { white-space: normal; color: var(--ink-2); padding: 10px 8px; }
.yday { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; padding: 10px 14px; }
.yday .yv { font: 500 12px/1.3 var(--mono); color: var(--ink-2); font-variant-numeric: tabular-nums; text-align: end; }
.foot { margin-top: 10px; font-size: 11px; }
.foot a { color: var(--ink-2); }
@media (prefers-reduced-motion: reduce) { .actions button { transition: none; } .actions button:active { scale: 1; } }
"""


def render_panel_html(rows: list[dict[str, Any]], daily: dict[str, Any], status: dict[str, Any], use_f: bool = True, presets_f: Optional[dict[str, int]] = None, yesterday: Optional[dict[str, Any]] = None) -> str:
    """Compact instrument panel for the menu-bar popover. Actions navigate to embermug:// URLs the app intercepts."""
    unit = "°F" if use_f else "°C"

    def temp(c: Optional[float], d: int = 0) -> str:
        if c is None:
            return "—"
        return f"{c_to_f(c):.{d}f}{unit}" if use_f else f"{c:.1f}{unit}"

    def conv(c: float) -> float:
        return c_to_f(c) if use_f else c

    def num(v: Any, suffix: str = "") -> str:
        return f'{v}<span class="u">{suffix}</span>' if v not in (None, "—") else '<span class="dim">—</span>'

    def hm(iso: Optional[str], fmt: str = "%H:%M") -> str:
        return datetime.fromisoformat(iso).strftime(fmt) if iso else "—"

    connected = bool(status.get("connected"))
    live = next((r for r in reversed(rows) if r["in_progress"]), None)
    latest = live or (rows[-1] if rows else None)

    # gauges from the *live* status when connected, else from the last drink (and say so)
    cur_c = status.get("current_c") if connected else (latest["last_temp_c"] if latest else None)
    tgt_c = (status.get("target_c") or None) if connected else (latest["target_series"][-1][1] if latest and latest["target_series"] else None)
    lvl_pct = status.get("liquid_level_percent") if connected else ((latest["end_level"] or 0) / 30 * 100 if latest and latest["end_level"] is not None else None)
    batt = status.get("battery_percent")
    drink_label = "drink" if connected else "last drink"
    g_temp = svg_gauge(conv(cur_c) if cur_c is not None else None, conv(tgt_c) if tgt_c else None, conv(20), conv(70), drink_label, unit,
                       aria=f"{drink_label} temperature {temp(cur_c)}" + (f", target {temp(tgt_c)}" if tgt_c else ", heating off"))
    g_level = svg_gauge(lvl_pct, None, 0, 100, "level", "%", aria=f"level {lvl_pct:.0f}%" if lvl_pct is not None else "level unknown")
    batt_label = "charging" if (connected and status.get("charging")) else "battery"
    g_batt = svg_gauge(batt, None, 0, 100, batt_label, "%", aria=f"{batt_label} {batt:.0f}%" if batt is not None else "battery unknown")

    state = status.get("liquid_state_label") if connected else (status.get("status") or "not connected")
    eta = status.get("eta_seconds")
    note = ""
    if not connected and latest:
        note = f'<p class="note">Mug not connected. Showing the last readings (drink poured {hm(latest["poured_at"], "%a %H:%M")}).</p>'
    readouts = f"""
      <div class="hero-readouts">
        <div class="readout"><span class="rl">state</span><span class="rv state {'live' if connected else ''}">{html.escape(str(state).upper())}</span></div>
        <div class="readout"><span class="rl">target</span><span class="rv">{temp(tgt_c) if tgt_c else '<span class="dim">off</span>'}</span></div>
        <div class="readout"><span class="rl">ready in</span><span class="rv">{('~' + fmt_dur(eta)) if eta else ('now' if state == 'Perfect' else '—')}</span></div>
        <div class="readout"><span class="rl">poured</span><span class="rv">{hm(latest['poured_at']) if latest else '—'}</span></div>
        <div class="readout"><span class="rl">elapsed</span><span class="rv">{fmt_dur(latest['duration_s']) if latest else '—'}</span></div>
        <div class="readout"><span class="rl">consumed</span><span class="rv">{num(latest['consumed_ml'] if latest else None, ' ml')}</span></div>
      </div>"""
    hero = f'<section class="hero" aria-label="Mug status"><div class="hero-gauges">{g_temp}{g_level}{g_batt}</div>{note}{readouts}</section>'

    tiles = f"""
    <section class="tiles" aria-label="Today">
      <div class="tile"><span class="rl">cups today</span><span class="tv">{daily['cups']}</span></div>
      <div class="tile"><span class="rl">drank today</span><span class="tv">{num(daily['consumed_ml'] or 0, ' ml')}</span></div>
      <div class="tile"><span class="rl">avg to target</span><span class="tv">{fmt_dur(daily['avg_time_to_target_s'])}</span></div>
      <div class="tile"><span class="rl">avg drink</span><span class="tv">{fmt_dur(daily['avg_duration_s'])}</span></div>
    </section>"""

    # presets are stored in °F; show them in the panel's unit and mark the one that matches the current target
    presets = presets_f or {}
    target_f = round(c_to_f(tgt_c)) if (connected and tgt_c) else None

    def button(href: str, label: str, value: str, pressed: bool = False) -> str:
        return (f'<button type="button" data-href="{href}" aria-pressed="{"true" if pressed else "false"}">'
                f'<span class="rl">{html.escape(label)}</span><span>{value}</span></button>')

    actions = "".join(button(f"embermug://set-temp/{v}", k, f"{v}°F" if use_f else f"{(v - 32) * 5 / 9:.0f}°C", pressed=(target_f == v)) for k, v in presets.items())
    actions += button("embermug://heating-off", "Heating", "off", pressed=(connected and not tgt_c))
    actions = f'<section class="actions" aria-label="Set temperature">{actions}</section>'

    chart = ""
    if latest:
        duration = max(latest["duration_s"], 60)
        temps = [(t, conv(c)) for t, c, _ in latest["app_temps"]] or [(t, conv(c)) for t, _, c in latest["mug_samples"] if c is not None]
        fine = bool(latest["app_temps"])
        targets = [(t, conv(v)) for t, v in step_series(latest["target_series"], duration)]
        which = "current drink" if latest["in_progress"] else "last drink"
        chart = f"""
    <section class="panel" aria-label="{which} temperature">
      <div class="panel-title"><span class="rl">{which}</span></div>
      <div class="charts"><figure>
        <figcaption><span class="key key-1"></span>temperature <span class="key key-2 dashed"></span>target</figcaption>
        {svg_line_chart(temps, targets, "temperature", lambda v: f"{v:.0f}{unit}", width=480, height=150, duration=duration, dots=not fine,
                        aria=f"{which}: temperature over {fmt_dur(duration)}, {len(temps)} readings, now {temp(latest['last_temp_c'])}")}
      </figure></div>
    </section>"""

    recent_rows = "".join(
        f"<tr><td class='num'>{hm(r['poured_at'], '%a %H:%M')}</td>"
        f"<td class='num'>{fmt_dur(r['duration_s'])}</td><td class='num'>{r['consumed_ml'] if r['consumed_ml'] is not None else '—'} ml</td>"
        f"<td class='num'>{fmt_dur(r['reached_target_after_s'])}</td><td class='dim'>{'in progress' if r['in_progress'] else ('abandoned' if r['abandoned'] else 'done')}</td></tr>"
        for r in [x for x in reversed(rows) if x["is_cup"]][:6]
    )
    empty = "<tr><td class='empty' colspan='5'>No cups yet. Pour a drink and it shows up here within a minute.</td></tr>"
    recent = f"""
    <section class="panel" aria-label="Recent cups">
      <div class="panel-title"><span class="rl">recent cups</span><a class="more" href="embermug://open-log">Open full log ↗</a></div>
      <div class="tablewrap"><table class="recent"><thead><tr><th>poured</th><th>lasted</th><th>drank</th><th>ready</th><th>status</th></tr></thead><tbody>{recent_rows or empty}</tbody></table></div>
    </section>"""

    yday = ""
    if yesterday and yesterday.get("cups"):
        ml = yesterday["consumed_ml"]
        vol = f"{ml} ml" if not use_f else f"{ml / 29.57:.0f} oz"
        span = ""
        if yesterday.get("first_pour"):
            span = yesterday["first_pour"][11:16]
            if yesterday.get("last_pour") and yesterday["last_pour"] != yesterday["first_pour"]:
                span += "\u2013" + yesterday["last_pour"][11:16]
        bits = [f"{yesterday['cups']} cup{'s' if yesterday['cups'] != 1 else ''}"]
        if ml:
            bits.append(vol)
        if span:
            bits.append(span)
        if yesterday.get("abandoned"):
            bits.append(f"{yesterday['abandoned']} went cold")
        yday = '<section class="panel yday" aria-label="Yesterday"><span class="rl">yesterday</span><span class="yv">' + html.escape(" · ".join(bits)) + '</span></section>'
    name = status.get("name")
    mug_line = " · ".join(str(v) for v in (name if name and str(name).strip().upper() != "EMBER" else None, status.get("model")) if v)
    meta = f'<i class="{"live" if connected else ""}" aria-hidden="true"></i>{"connected" if connected else "not connected"}' + (f' · {html.escape(mug_line)}' if mug_line else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Ember</title>
<style>{INSTRUMENT_CSS}{PANEL_CSS}</style></head>
<body><main>
  <header class="mast"><h1><b>Ember</b> · Mug</h1><div class="meta" role="status">{meta}</div></header>
  {hero}
  {tiles}
  {actions}
  {chart}
  {recent}
  {yday}
  <p class="foot">Control-click or right-click the menu bar icon for settings.</p>
</main>
<script>
document.addEventListener("click", function (e) {{
  var b = e.target.closest("button[data-href]");
  if (b) {{ e.preventDefault(); var a = document.createElement("a"); a.href = b.getAttribute("data-href"); document.body.appendChild(a); a.click(); a.remove(); }}
}});
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Ember drink log")
    parser.add_argument("--json", action="store_true", help="print drinks as JSON")
    parser.add_argument("--html", metavar="FILE", help="write the drink-log page")
    parser.add_argument("--celsius", action="store_true", help="report in °C instead of °F")
    parser.add_argument("--briefing", action="store_true", help="print yesterday's and today's one-line summaries (add --json for the data)")
    parser.add_argument("--from-capture", action="store_true", help="rebuild from the raw capture instead of drinks.jsonl")
    parser.add_argument("--capture", default=str(CAPTURE_PATH))
    args = parser.parse_args(argv)

    tracker = DrinkTracker()
    history = read_history() if args.from_capture else tracker.load_history()
    if args.from_capture:
        try:
            with open(args.capture) as f:
                cap_rows = [json.loads(l) for l in f if l.strip()]
        except OSError as e:
            print(f"Cannot read capture: {e}", file=sys.stderr)
            return 2
        rows = drinks_from_capture(cap_rows, history)
        packets = len(cap_rows)
    else:
        rows = tracker.all_rows(history)
        try:
            with open(CURRENT_PATH) as f:
                cur = json.load(f)
            if not any(r["id"] == cur["id"] for r in rows):
                rows.append(cur)
        except (OSError, json.JSONDecodeError):
            pass
        packets = 0
    daily = tracker.daily_summary(history=history, rows=rows)

    if args.briefing:
        b = tracker.briefing(use_f=not args.celsius, rows=rows)
        if args.json:
            print(json.dumps(b, indent=2, default=str))
        else:
            print(b["yesterday_line"])
            print(b["today_line"])
        return 0
    if args.html:
        mug = None
        try:
            with open(SUPPORT_DIR / "status.json") as f:
                st = json.load(f)
            mug = {k: st.get(k) for k in ("name", "model", "firmware", "serial")}
        except (OSError, json.JSONDecodeError):
            pass
        Path(args.html).write_text(render_html(rows, daily, use_f=not args.celsius, capture_packets=packets, mug=mug))
        print(f"Wrote {args.html} ({len(rows)} drinks)")
        return 0
    if args.json:
        print(json.dumps({"daily": daily, "drinks": [{k: v for k, v in r.items() if k not in ('records', 'app_temps', 'app_levels')} for r in rows]}, indent=2, default=str))
        return 0
    print(tracker.day_line(daily, "Today", use_f=not args.celsius))
    for r in rows:
        print(f"  {r['poured_at'] or r['id']:>20}  {fmt_dur(r['duration_s']):>8}  level {r['start_level']}→{r['end_level']}  {r['consumed_ml'] or '—'} ml  ready {fmt_dur(r['reached_target_after_s'])}  {'cup' if r['is_cup'] else 'rinse'}{' · in progress' if r['in_progress'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
