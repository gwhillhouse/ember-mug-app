"""Tests for the GUI-free parts of ember_mug_app.py: config, the CLI command queue, history, CLI validation."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ember_mug_app as app  # noqa: E402


class AppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        paths = {
            "SUPPORT_DIR": self.dir,
            "CONFIG_PATH": self.dir / "config.json",
            "COMMAND_PATH": self.dir / "command.json",
            "COMMAND_DIR": self.dir / "commands",
            "HISTORY_PATH": self.dir / "history.jsonl",
            "STATUS_PATH": self.dir / "status.json",
        }
        for name, value in paths.items():
            patcher = mock.patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class ConfigTests(AppTestCase):
    def test_round_trip(self) -> None:
        config = app.load_config()
        config["mug_address"] = "AA-BB"
        config["presets_f"] = {"Hot": 140}
        app.save_config(config)
        again = app.load_config()
        self.assertEqual((again["mug_address"], again["presets_f"]), ("AA-BB", {"Hot": 140}))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["config.json"])  # no temp files left behind

    def test_broken_config_is_kept_aside(self) -> None:
        for bad in ('{"mug_address": "AA-', "[1, 2]"):
            (self.dir / "config.json").write_text(bad)
            config = app.load_config()
            self.assertEqual(config["mug_address"], "")
            self.assertEqual((self.dir / "config.json.bad").read_text(), bad)
            self.assertFalse((self.dir / "config.json").exists())


class CommandQueueTests(AppTestCase):
    def test_several_commands_all_arrive_in_order(self) -> None:
        app.write_command({"cmd": "set_temp", "temp_c": 60})
        app.write_command({"cmd": "led", "rgb": [255, 0, 0]})
        app.write_command({"cmd": "refresh"})
        self.assertEqual([c["cmd"] for c in app.take_commands()], ["set_temp", "led", "refresh"])
        self.assertEqual(app.take_commands(), [])
        self.assertEqual(list((self.dir / "commands").iterdir()), [])

    def test_stale_and_malformed_commands_are_dropped(self) -> None:
        app.write_command({"cmd": "set_temp", "temp_c": 60})
        (self.dir / "commands" / "0-bad.json").write_text("[1]")
        (self.dir / "commands" / "1-torn.json").write_text('{"cmd": ')
        with mock.patch.object(app.time, "time", return_value=time.time() + app.COMMAND_MAX_AGE_S + 5):
            self.assertEqual(app.take_commands(), [])
        self.assertEqual(list((self.dir / "commands").iterdir()), [])

    def test_legacy_single_file_is_still_read(self) -> None:
        (self.dir / "command.json").write_text(json.dumps({"cmd": "refresh"}))
        self.assertEqual(app.take_commands(), [{"cmd": "refresh"}])
        self.assertFalse((self.dir / "command.json").exists())


class HistoryTests(AppTestCase):
    def test_load_compacts_and_skips_bad_rows(self) -> None:
        now = time.time()
        rows = [
            {"ts": now - (app.HISTORY_KEEP_DAYS + 1) * 86400, "current_c": 50},
            {"ts": now - 86400, "current_c": 55},  # kept on disk, outside the 12 h window
            {"ts": now - 60, "current_c": 60, "target_c": 60, "state": "HEATING", "battery": 80, "on_base": True},
            {"ts": "yesterday", "current_c": 1},
            {"current_c": 2},
        ]
        (self.dir / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
        h = app.TemperatureHistory(12)
        self.assertEqual([s[1] for s in h.snapshot()], [60])
        self.assertEqual(len(h.battery), 1)
        kept = [json.loads(line)["current_c"] for line in (self.dir / "history.jsonl").read_text().splitlines()]
        self.assertEqual(kept, [55, 60])

    def test_add_and_battery_trend(self) -> None:
        h = app.TemperatureHistory(12)
        h.add(55.0, 60.0, "HEATING", battery=50, on_base=True)
        h.add(56.0, 60.0, "HEATING", battery=51, on_base=True)
        self.assertEqual(len(h.snapshot()), 2)
        self.assertTrue(h.battery_trend()["on_base"])
        self.assertEqual(len((self.dir / "history.jsonl").read_text().splitlines()), 2)


class CliValidationTests(AppTestCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        (self.dir / "status.json").write_text(json.dumps({"connected": True, "updated_ts": time.time()}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = app.cli(list(argv))
        return rc, err.getvalue()

    def queued(self) -> list[dict]:
        return app.take_commands()

    def test_set_temp_accepted(self) -> None:
        self.assertEqual(self.run_cli("--set-temp", "60C")[0], 0)
        self.assertEqual(self.queued()[0]["temp_c"], 60.0)
        self.assertEqual(self.run_cli("--set-temp", "140F")[0], 0)
        self.assertAlmostEqual(self.queued()[0]["temp_c"], 60.0)

    def test_set_temp_rejected(self) -> None:
        for value in ("0", "32F", "55F", "nan", "inf", "hot", "70C"):
            rc, err = self.run_cli("--set-temp", value)
            self.assertEqual(rc, 2, value)
            self.assertTrue(err)
        self.assertEqual(self.queued(), [])

    def test_handoff_bounds(self) -> None:
        self.assertEqual(self.run_cli("--handoff", "inf")[0], 2)
        self.assertEqual(self.run_cli("--handoff", "0")[0], 2)
        self.assertEqual(self.run_cli("--handoff", "15")[0], 0)
        self.assertEqual(self.queued()[0]["seconds"], 900)


if __name__ == "__main__":
    unittest.main()
