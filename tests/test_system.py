"""Adaptery systemu: plist LaunchAgenta, `launchctl` i odczyt zasilania na atrapach podprocesów."""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from types import SimpleNamespace

from perno.system import launchd, power


def done(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class PlistTests(unittest.TestCase):
    def test_plist_starts_the_daemon_at_login_and_after_crashes(self):
        data = plistlib.loads(launchd.build_plist(python="/p/.venv/bin/python", base_dir="/p", log_path="/p/logs/a.log"))
        self.assertEqual(data["Label"], "pl.perno.automat")
        self.assertEqual(data["ProgramArguments"], ["/p/.venv/bin/python", "-m", "perno.cli.automat", "run"])
        self.assertEqual((data["RunAtLoad"], data["KeepAlive"], data["WorkingDirectory"]), (True, {"SuccessfulExit": False}, "/p"))
        self.assertEqual((data["StandardOutPath"], data["StandardErrorPath"]), ("/p/logs/a.log", "/p/logs/a.log"))
        self.assertIn("/opt/homebrew/bin", data["EnvironmentVariables"]["PATH"])


class LaunchctlTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.calls = []

    def run_stub(self, results=None):
        def run(cmd, **kw):
            self.calls.append(cmd[1:])
            return (results or {}).get(cmd[1], done())
        return run

    def test_install_writes_the_file_and_reloads(self):
        path = launchd.install(self.run_stub(), home=self.home, content=b"x", uid=501)
        self.assertEqual(path.read_bytes(), b"x")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o644")
        self.assertEqual(self.calls, [["bootout", "gui/501/pl.perno.automat"], ["bootstrap", "gui/501", str(path)]])

    def test_install_reports_launchctl_failure(self):
        with self.assertRaisesRegex(launchd.LaunchdError, "bootstrap.*Input/output error"):
            launchd.install(self.run_stub({"bootstrap": done(5, "", "Input/output error")}), home=self.home, content=b"x", uid=501)

    def test_uninstall(self):
        launchd.install(self.run_stub(), home=self.home, content=b"x", uid=501)
        self.assertTrue(launchd.uninstall(self.run_stub(), home=self.home, uid=501))
        self.assertFalse(launchd.plist_path(self.home).exists())
        self.assertFalse(launchd.uninstall(self.run_stub(), home=self.home, uid=501))

    def test_status_parses_pid(self):
        launchd.install(self.run_stub(), home=self.home, content=b"x", uid=501)
        status = launchd.status(self.run_stub({"print": done(0, "x = 1\n\tpid = 4321\n")}), home=self.home, uid=501)
        self.assertEqual(status, launchd.LaunchdStatus(True, True, 4321))

    def test_status_when_not_loaded_or_launchctl_missing(self):
        self.assertEqual(launchd.status(self.run_stub({"print": done(113)}), home=self.home, uid=501),
                         launchd.LaunchdStatus(False, False))

        def missing(cmd, **kw):
            raise FileNotFoundError

        self.assertEqual(launchd.status(missing, home=self.home, uid=501), launchd.LaunchdStatus(False, False))


class PowerTests(unittest.TestCase):
    BATTERY = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t63%; discharging; 9:13 remaining present: true\n"

    def test_parse(self):
        self.assertEqual(power.parse_battery(self.BATTERY), power.PowerState(True, 63))
        self.assertEqual(power.parse_battery("Now drawing from 'AC Power'\n -InternalBattery-0\t100%; charged"),
                         power.PowerState(False, 100))
        self.assertEqual(power.parse_battery(""), power.PowerState(None, None))

    def test_read_never_raises(self):
        self.assertEqual(power.read_power_state(lambda *a, **k: done(0, self.BATTERY)), power.PowerState(True, 63))
        self.assertEqual(power.read_power_state(lambda *a, **k: done(1)), power.PowerState(None))

        def boom(*a, **k):
            raise OSError

        self.assertEqual(power.read_power_state(boom), power.PowerState(None))


class FakeProc:
    def __init__(self):
        self.alive, self.terminated = True, False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive, self.terminated = False, True


class SleepGuardTests(unittest.TestCase):
    def test_starts_once_and_releases(self):
        procs, calls = [], []

        def popen(cmd, **kw):
            calls.append(cmd)
            procs.append(FakeProc())
            return procs[-1]

        guard = power.SleepGuard(popen, pid=77)
        guard.set_needed(True)
        guard.set_needed(True)
        self.assertEqual(calls, [["/usr/bin/caffeinate", "-i", "-w", "77"]])
        self.assertTrue(guard.active)
        guard.set_needed(False)
        self.assertTrue(procs[0].terminated)
        self.assertFalse(guard.active)
        guard.set_needed(True)
        self.assertEqual(len(calls), 2)

    def test_restarts_a_dead_caffeinate_and_survives_a_missing_binary(self):
        procs = []
        guard = power.SleepGuard(lambda cmd, **kw: procs.append(FakeProc()) or procs[-1], pid=1)
        guard.set_needed(True)
        procs[0].alive = False
        guard.set_needed(True)
        self.assertEqual(len(procs), 2)

        def boom(*a, **k):
            raise OSError

        broken = power.SleepGuard(boom, pid=1)
        broken.set_needed(True)
        self.assertFalse(broken.active)
        broken.release()


if __name__ == "__main__":
    unittest.main()
