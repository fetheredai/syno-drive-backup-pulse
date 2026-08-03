#!/usr/bin/env python3
"""
End-to-end tests: run collector.py against the mock DSM and assert on the
data.json it produces.

    python3 -m unittest tests -v

These prove the collector's plumbing — login, log paging, field picking, day
bucketing, admin exclusion, root inference, status thresholds. They do NOT
prove the real Drive Server uses these field names; that needs --discover
against real hardware.
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import mock_dsm
import collector


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CollectorE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", cls.port), mock_dsm.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

        cls.tmp = tempfile.mkdtemp()
        cls.out = os.path.join(cls.tmp, "data.json")
        os.environ.update({
            "SYNO_NAS_NAME": "Test NAS",
            "SYNO_HOST": "127.0.0.1",
            "SYNO_PORT": str(cls.port),
            "SYNO_HTTPS": "false",
            "SYNO_USER": "svc-drivemonitor",
            "SYNO_PASS": "s3cret",
            "SYNO_DAYS": "90",
            "SYNO_OUTPUT": cls.out,
        })
        cfg = collector.config_from_env()
        ok = collector.run_once(cfg, 90, cls.out, False)
        assert ok, "collector reported failure against the mock"
        with open(cls.out) as f:
            cls.data = json.load(f)
        cls.users = {u["username"]: u for u in cls.data["nases"][0]["users"]}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    # -- shape --------------------------------------------------------------
    def test_output_shape(self):
        self.assertEqual(len(self.data["nases"]), 1)
        self.assertEqual(self.data["nases"][0]["name"], "Test NAS")
        self.assertEqual(self.data["days"], 90)
        self.assertIn("generated_at", self.data)

    def test_admins_and_guest_excluded(self):
        for name in ("admin", "awhite", "guest"):
            self.assertNotIn(name, self.users,
                             f"{name} should not appear in the dashboard")

    def test_all_regular_users_present(self):
        self.assertEqual(set(self.users),
                         set(mock_dsm.USER_PROFILE) | set(mock_dsm.EXTRA_USERS))

    def test_no_drive_client_reads_unused_not_never(self):
        """frank and gina have no Drive client at all — no connection, no log
        entries. On a real NAS these are the majority; reporting them as
        'never' buries the users whose backups actually stopped."""
        for name in mock_dsm.EXTRA_USERS:
            self.assertEqual(self.users[name]["status"], "unused",
                             f"{name} should be 'unused'")

    def test_never_is_reserved_for_users_with_a_drive_footprint(self):
        """Erin has a connected client and log entries, but has never moved a
        file. That is a real failure and must stay 'never', distinct from
        'unused'."""
        self.assertEqual(self.users["erin"]["status"], "never")

    def test_roots_never_contain_device_names_or_generic_segments(self):
        """Regression: infer_root fell back to the first path segment, so a
        path like /Backup/<device>/loose-file.txt produced roots called
        'Backup' and 'BNO-SJohn-564-....local'."""
        for u in self.users.values():
            for r in u["roots"]:
                self.assertNotEqual(r["name"].lower(), "backup")
                self.assertNotIn(".local", r["name"])
                for d in u["devices"]:
                    self.assertNotEqual(r["name"], d["name"])

    def test_user_paging_not_truncated(self):
        # SYNO.Core.User is paged; make sure we walked past page one.
        self.assertGreaterEqual(len(self.users), 5)

    # -- the core question this tool answers --------------------------------
    def test_status_thresholds(self):
        self.assertEqual(self.users["alice"]["status"], "ok")       # today
        self.assertEqual(self.users["brian"]["status"], "ok")       # 1 day
        self.assertEqual(self.users["carol"]["status"], "stale")    # 5 days
        self.assertEqual(self.users["dinesh"]["status"], "failing")  # 21 days

    def test_connected_but_never_syncing_reads_as_never(self):
        """Erin's client is online and last-seen is now, but she has never
        synced a file. The Admin Console would show her as connected; this
        dashboard must show 'never'."""
        erin = self.users["erin"]
        self.assertEqual(erin["status"], "never")
        self.assertIsNone(erin["last_success"])
        self.assertEqual(erin["daily"], {})
        self.assertTrue(erin["devices"][0]["online"],
                        "fixture should still report the client as online")

    def test_login_events_do_not_count_as_backups(self):
        self.assertEqual(self.users["erin"]["daily"], {})

    # -- aggregation --------------------------------------------------------
    def test_daily_buckets_are_dates_with_counts(self):
        daily = self.users["alice"]["daily"]
        self.assertTrue(daily)
        for day, count in daily.items():
            time.strptime(day, "%Y-%m-%d")
            self.assertIsInstance(count, int)
            self.assertGreater(count, 0)

    def test_daily_window_respects_days(self):
        for u in self.users.values():
            for day in u["daily"]:
                age = (time.time() - time.mktime(time.strptime(day, "%Y-%m-%d")))
                self.assertLessEqual(age / 86400, 91, f"{day} outside window")

    def test_roots_inferred_from_paths(self):
        self.assertEqual({r["name"] for r in self.users["alice"]["roots"]},
                         set(mock_dsm.ROOTS))

    def test_no_roots_when_user_has_no_file_events(self):
        """On real hardware File Station cannot list other users' homes (408),
        so the fallback yields nothing and roots come only from logged paths.
        A user who never synced therefore has no roots — not a bug."""
        self.assertEqual(self.users["erin"]["roots"], [])

    def test_path_comes_from_s1_not_the_target_key(self):
        """Regression: the log item has a key called 'target' holding "user".
        With it in F_PATH, every root became "user" and the real path in s1
        was ignored."""
        names = {r["name"] for r in self.users["alice"]["roots"]}
        self.assertNotIn("user", names)
        self.assertEqual(names, set(mock_dsm.ROOTS))

    def test_numeric_action_codes_count_as_file_events(self):
        """Regression: Drive Server reports action as numeric type 13, which
        never matched the verb keyword list, so every event was discarded and
        all users read as 'never'."""
        self.assertTrue(self.users["alice"]["daily"])
        self.assertEqual(self.users["alice"]["status"], "ok")

    def test_devices_attached(self):
        dev = self.users["alice"]["devices"][0]
        # client_id is the DEVICE and client_name is the USER — the opposite
        # way round from what the names suggest.
        self.assertEqual(dev["name"], "BNO-Alice-451-L379LTDXR2.local")
        self.assertTrue(dev["online"])          # from client_status == on_line
        self.assertTrue(dev["last_seen"])       # from last_auth_time

    def test_client_type_recorded(self):
        self.assertEqual(self.users["alice"]["client_types"], ["drive_backup"])

    def test_atomic_write_leaves_no_temp_file(self):
        self.assertFalse(os.path.exists(self.out + ".tmp"))


class UnitBits(unittest.TestCase):
    def test_infer_root(self):
        self.assertEqual(
            collector.infer_root("/homes/j/Drive/DESKTOP-1/Documents/tax/2025.pdf"),
            "Documents")
        self.assertEqual(
            collector.infer_root("/homes/j/Drive/DESKTOP-1/ProjectX/a.txt"),
            "ProjectX")
        self.assertIsNone(collector.infer_root(""))

    def test_status_for(self):
        now = 1_700_000_000
        self.assertEqual(collector.status_for(None, now), "never")
        self.assertEqual(collector.status_for(now - 3600, now), "ok")
        self.assertEqual(collector.status_for(now - 4 * 86400, now), "stale")
        self.assertEqual(collector.status_for(now - 30 * 86400, now), "failing")

    def test_pick_int_handles_milliseconds(self):
        self.assertEqual(collector.pick_int({"time": 1_700_000_000_000}, ["time"]),
                         1_700_000_000)
        self.assertIsNone(collector.pick_int({"time": "nope"}, ["time"]))

    def test_bad_credentials_surface_clearly(self):
        port = _free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), mock_dsm.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            ses = collector.SynoSession("bad", "127.0.0.1", port, https=False,
                                        username="svc-drivemonitor",
                                        password="wrong")
            with self.assertRaises(RuntimeError) as ctx:
                ses.connect()
            self.assertIn("wrong account or password", str(ctx.exception))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
