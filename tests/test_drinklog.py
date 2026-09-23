"""Tests for drinklog.py: packet framing, record decoding, drink tracking and the renderers.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import drinklog  # noqa: E402
from drinklog import DrinkTracker, Reassembler, Record, is_burst  # noqa: E402

CLOCK = 1_789_511_796  # an absolute mug timestamp (2026-09-15)


def body(kind: int, sub: int, t: int, fields: bytes = b"") -> bytes:
    return bytes([0x00, kind, sub]) + t.to_bytes(4, "big") + b"\xff" + fields


def packets(b: bytes) -> list[bytes]:
    """Frame a record body the way the mug does: one 01 packet, or 02 (16 bytes) + 04 (the rest)."""
    if len(b) <= 16:
        return [bytes([0x01, 0x00, len(b)]) + b]
    return [bytes([0x02, 0x00, 0x10]) + b[:16], bytes([0x04, 0x01, len(b) - 16]) + b[16:]]


def filling(t: int) -> bytes:
    return body(0x05, 2, t)


def target_set(t: int, c: float) -> bytes:
    return body(0x07, 5, t, round(c * 100).to_bytes(2, "big"))


def heater(t: int, c: float) -> bytes:
    return body(0x0F, 3, t, round(c * 100).to_bytes(2, "big") + b"\x7f\xff" * 4 + b"\xff\xff")


def sample(t: int, level: int, c: float | None) -> bytes:
    temp = b"\x7f\xff" if c is None else round(c * 100).to_bytes(2, "big")
    return body(0x10, 4, t, bytes([level]) + temp + b"\x7f\xff" * 4 + b"\xff\xff")


class FramingTests(unittest.TestCase):
    def test_single_packet_record(self) -> None:
        recs = Reassembler().feed(packets(target_set(1384, 57.21))[0], 100.0, False)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r.kind, r.sub, r.t), (0x07, 5, 1384))
        self.assertEqual(r.fields()["target_c"], 57.21)

    def test_fragmented_record_is_reassembled(self) -> None:
        ra = Reassembler()
        first, last = packets(sample(613, 30, 62.5))
        self.assertEqual(ra.feed(first, 1.0, False), [])
        recs = ra.feed(last, 2.0, False)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].fields(), {"level": 30, "temp_c": 62.5})
        self.assertEqual(recs[0].arrived, 1.0)  # arrival of the first fragment
        self.assertEqual(len(recs[0].raw_packets), 2)

    def test_null_sentinel_decodes_to_none(self) -> None:
        recs = Reassembler().feed(packets(sample(613, 5, None))[0], 1.0, False) or Reassembler().feed(b"", 0, False)
        ra = Reassembler()
        for p in packets(sample(613, 5, None)):
            recs = ra.feed(p, 1.0, False)
        self.assertIsNone(recs[0].fields()["temp_c"])

    def test_end_marker_and_empty(self) -> None:
        ra = Reassembler()
        self.assertEqual(ra.feed(b"\x05", 0, False), [])
        self.assertEqual(ra.feed(b"", 0, False), [])

    def test_orphan_final_fragment_is_unknown(self) -> None:
        recs = Reassembler().feed(b"\x04\x01\x02\xaa\xbb", 0, False)
        self.assertEqual(recs[0].kind, -1)

    def test_record_json_round_trip(self) -> None:
        r = Record(0x10, 4, 613, b"\x1e\x18\x6a", 5.0, True, ["01 00"], CLOCK)
        self.assertEqual(Record.from_json(json.loads(json.dumps(r.to_json()))), r)


class BurstTests(unittest.TestCase):
    def test_is_burst(self) -> None:
        flush = [100.0, 100.5, 101.0, 101.5, 102.0]
        self.assertTrue(is_burst(flush, 101.0))
        self.assertFalse(is_burst([50.0, 100.0, 200.0], 100.0))

    def test_reassemble_rows_marks_flush(self) -> None:
        rows = [{"ts": 1000.0 + i * 0.1, "hex": p.hex(" ")} for i, p in enumerate(p for b in (filling(3), heater(13, 60), target_set(20, 57)) for p in packets(b))]
        rows.append({"ts": 5000.0, "hex": packets(target_set(4000, 60))[0].hex()})
        recs = drinklog.reassemble_rows(rows)
        self.assertEqual([r.burst for r in recs], [True, True, True, False])


class TrackerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def tracker(self) -> DrinkTracker:
        return DrinkTracker(self.dir / "drinks.jsonl", self.dir / "current.json", self.dir / "daily.json", self.dir / "history.jsonl")

    def feed(self, tr: DrinkTracker, bodies: list[bytes], start: float, gap: float) -> None:
        ts = start
        for b in bodies:
            for p in packets(b):
                tr.feed_packet(p, ts)
                ts += gap


class TrackerTests(TrackerTestCase):
    def test_backlog_with_two_relative_drinks(self) -> None:
        tr = self.tracker()
        now = time.time()
        drink1 = [filling(3), heater(13, 62.77), sample(613, 30, 62.5), sample(1213, 10, 61.0)]
        drink2 = [filling(2), heater(12, 60.0), sample(612, 25, 60.0)]
        self.feed(tr, drink1 + drink2, now, 0.05)  # one fast flush
        self.assertEqual(len(tr.rows), 1, "the second pour finalises the first drink")
        first = tr.rows[0]
        self.assertEqual(first["end_reason"], "next pour")
        self.assertEqual((first["start_level"], first["end_level"]), (30, 10))
        self.assertEqual(first["consumed_ml"], round(20 / 30 * drinklog.MUG_CAPACITY_ML))
        self.assertEqual(first["first_target_c"], 62.77)
        self.assertEqual(first["anchor"], "none")  # everything was backlog: no live record to anchor on
        # persisted, and the drink in progress is written separately
        self.assertEqual(len(tr._load_rows()), 1)
        cur = json.loads((self.dir / "current.json").read_text())
        self.assertTrue(cur["in_progress"])
        self.assertEqual(cur["start_level"], 25)

    def test_live_record_anchors_pour_time(self) -> None:
        tr = self.tracker()
        tr.feed_packet(packets(target_set(7474, 60.0))[0], 20_000.0)
        tr.current.reanchor(tr.arrivals)
        self.assertEqual(tr.current.anchor_source, "live")
        self.assertEqual(tr.current.t0, 20_000.0 - 7474 - drinklog.LIVE_LATENCY_S)

    def test_clocked_records_use_mug_time(self) -> None:
        tr = self.tracker()
        self.feed(tr, [filling(CLOCK), heater(CLOCK + 10, 60.0), sample(CLOCK + 610, 30, 60.0)], 50_000.0, 0.05)
        self.feed(tr, [filling(CLOCK + 5000), sample(CLOCK + 5600, 20, 58.0)], 50_010.0, 0.05)
        self.assertEqual(len(tr.rows), 1)
        first = tr.rows[0]
        self.assertEqual(first["anchor"], "clock")
        self.assertEqual(first["poured_ts"], float(CLOCK))
        self.assertEqual(first["duration_s"], 610)
        self.assertEqual([r["t"] for r in first["records"]], [0, 10, 610])
        self.assertEqual(first["records"][2]["t_abs"], CLOCK + 610)
        tr.current.reanchor(tr.arrivals)
        self.assertEqual(tr.current.t0, float(CLOCK + 5000))

    def test_mark_pour_then_empty(self) -> None:
        tr = self.tracker()
        t0 = time.time() - 900
        tr.mark_pour(t0)
        self.feed(tr, [filling(3), sample(603, 30, 60.0)], t0 + 20, 0.05)
        self.assertEqual(tr.current.anchor_source, "app")
        tr.mark_empty(t0 + 900)
        self.assertIsNone(tr.current)
        self.assertEqual(tr.rows[0]["end_reason"], "emptied")
        self.assertEqual(tr.rows[0]["poured_ts"], t0)
        self.assertFalse((self.dir / "current.json").exists())
        daily = json.loads((self.dir / "daily.json").read_text())
        self.assertEqual(daily["date"], datetime.now().strftime("%Y-%m-%d"))

    def test_corrupt_drinks_line_is_skipped(self) -> None:
        good = {"id": "20260915-110733", "poured_ts": 1.0}
        (self.dir / "drinks.jsonl").write_text(json.dumps(good) + "\n" + '{"id": "torn' + "\n\n")
        self.assertEqual(self.tracker().rows, [good])


class HistoryTests(TrackerTestCase):
    def write(self, *rows: object, raw: str = "") -> None:
        with open(self.dir / "history.jsonl", "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
            f.write(raw)

    def test_incremental_read(self) -> None:
        tr = self.tracker()
        now = time.time()
        self.assertEqual(tr.load_history(), [])
        self.write({"ts": now - 10, "current_c": 60})
        self.assertEqual(len(tr.load_history()), 1)
        self.write({"ts": now - 5, "current_c": 61}, raw='{"ts": ')  # a line still being written
        self.assertEqual([h["current_c"] for h in tr.load_history()], [60, 61])
        self.write(raw=f'{now}, "current_c": 62}}\n')
        self.assertEqual([h["current_c"] for h in tr.load_history()], [60, 61, 62])

    def test_old_readings_dropped_and_truncation_rereads(self) -> None:
        tr = self.tracker()
        now = time.time()
        self.write({"ts": now - drinklog.HISTORY_KEEP_S - 60, "current_c": 1}, "not json", {"ts": now, "current_c": 2})
        self.assertEqual([h["current_c"] for h in tr.load_history()], [2])
        (self.dir / "history.jsonl").write_text("")
        self.write({"ts": now, "current_c": 3})
        self.assertEqual([h["current_c"] for h in tr.load_history()], [3])

    def test_app_readings_feed_the_drink(self) -> None:
        tr = self.tracker()
        t0 = time.time() - 600
        self.write(*({"ts": t0 + i * 60, "current_c": 55 + i, "target_c": 60, "state": "HEATING", "level": 30 - i} for i in range(10)))
        self.write({"ts": t0 + 590, "current_c": 60, "target_c": 60, "state": "TARGET_TEMPERATURE", "level": 20})
        tr.mark_pour(t0)
        row = tr.all_rows()[-1]
        self.assertEqual(row["reached_target_after_s"], 590)
        self.assertEqual((row["start_level"], row["end_level"]), (30, 20))
        self.assertEqual(row["last_temp_c"], 60)


class AtomicWriteTests(unittest.TestCase):
    def test_replaces_and_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "x.json"
            drinklog.write_atomic(p, "one")
            drinklog.write_atomic(p, "two")
            self.assertEqual(p.read_text(), "two")
            self.assertEqual(os.listdir(p.parent), ["x.json"])


class RenderTests(TrackerTestCase):
    def test_pages_render(self) -> None:
        tr = self.tracker()
        self.feed(tr, [filling(3), heater(13, 62.77), sample(613, 30, 62.5), target_set(700, 57.21)], time.time() - 800, 0.05)
        tr.current.records[0].burst = False
        rows = tr.all_rows()
        daily = tr.daily_summary(rows=rows)
        for use_f in (True, False):
            page = drinklog.render_html(rows, daily, use_f=use_f, mug={"name": "<Mug>", "model": "Mug 2"})
            self.assertIn("&lt;Mug&gt;", page)
            panel = drinklog.render_panel_html(rows, daily, {"connected": False}, use_f=use_f, presets_f={"Hot": 140})
            self.assertIn("embermug://set-temp/140", panel)
        self.assertIn("No drinks logged yet", drinklog.render_html([], tr.daily_summary(rows=[])))

    def test_fmt_dur(self) -> None:
        self.assertEqual(drinklog.fmt_dur(None), "—")
        self.assertEqual(drinklog.fmt_dur(125), "2 min")
        self.assertEqual(drinklog.fmt_dur(3725), "1h 02m")


if __name__ == "__main__":
    unittest.main()
