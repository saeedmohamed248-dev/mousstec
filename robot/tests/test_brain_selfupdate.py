"""«مخ موس» updates itself (robot/brain/selfupdate.py): a newer brain the
server ships comes down, goes in over the folder — never the launchers,
__init__.py last — and the one before comes back if the new one won't run."""

import io
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch
from robot import brain_package, views
from robot.brain import VERSION, config, loop, selfupdate
from robot.models import RobotDevice

NEW = "brain-99.0.0"


def _zip(version=NEW, extra=None, init=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("brain/__init__.py", init if init is not None else f'"""brain"""\nVERSION = "{version}"\n')
        zf.writestr("brain/loop.py", "NEW = True\n")
        zf.writestr("brain/models/wake.npz", b"\x00model")
        zf.writestr("brain/run_windows.bat", "@echo new launcher\n")
        zf.writestr("run_windows.bat", "@echo new launcher\n")
        zf.writestr("README.md", "readme\n")
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return buf.getvalue()


class InstallTests(unittest.TestCase):

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.folder, self.home = root / "brain", root / "home"
        self.folder.mkdir()
        (self.folder / "__init__.py").write_text('VERSION = "brain-0.9.1"\n', encoding="utf-8")
        (self.folder / "loop.py").write_text("OLD = True\n", encoding="utf-8")
        (self.folder / "run_windows.bat").write_text("@echo old launcher\n", encoding="utf-8")
        (self.folder / "brain.ini").write_text("token = secret\n", encoding="utf-8")
        self.lines = []

    def install(self, data=None, version=NEW):
        return selfupdate.install(data or _zip(), version, self.folder, self.home, "brain-0.9.1", log=self.lines.append)

    def test_it_goes_in_but_never_the_launchers_and_the_one_before_is_kept(self):
        self.assertTrue(self.install())
        self.assertEqual(selfupdate.version_in((self.folder / "__init__.py").read_text(encoding="utf-8")), NEW)
        self.assertEqual((self.folder / "loop.py").read_text(encoding="utf-8"), "NEW = True\n")
        self.assertEqual((self.folder / "models" / "wake.npz").read_bytes(), b"\x00model")
        self.assertEqual((self.folder / "run_windows.bat").read_text(encoding="utf-8"), "@echo old launcher\n")
        self.assertEqual((self.folder / "brain.ini").read_text(encoding="utf-8"), "token = secret\n")
        self.assertFalse(list(self.folder.rglob("*.new")))
        backup = self.home / selfupdate.BACKUP
        self.assertEqual((backup / "loop.py").read_text(encoding="utf-8"), "OLD = True\n")
        self.assertFalse((backup / "brain.ini").exists())                  # the key isn't copied around
        self.assertIn("run_windows.bat", "".join(self.lines))              # said: the dashboard's zip, once

    def test_init_goes_in_last(self):
        written = []
        real = selfupdate.os.replace
        with mock.patch.object(selfupdate.os, "replace",
                               side_effect=lambda a, b: (written.append(Path(b).name), real(a, b))):
            self.install()
        self.assertEqual(written[-1], "__init__.py")

    def test_a_zip_that_isnt_right_changes_nothing(self):
        bad = {
            "not a zip": b"<html>502</html>",
            "another version": _zip(init='VERSION = "brain-98.0.0"\n'),
            "outside the folder": _zip(extra={"brain/../evil.py": "x"}),
        }
        for why, data in bad.items():
            with self.subTest(why):
                self.assertFalse(self.install(data))
                self.assertEqual((self.folder / "loop.py").read_text(encoding="utf-8"), "OLD = True\n")
                self.assertFalse((self.home / selfupdate.TRIAL).exists())
        self.assertFalse((self.folder.parent / "evil.py").exists())

    def test_a_file_that_cant_be_written_puts_the_one_before_back(self):
        real = selfupdate.os.replace

        def replace(a, b):
            if Path(b).name == "wake.npz":
                raise PermissionError("in use")
            return real(a, b)

        with mock.patch.object(selfupdate.os, "replace", side_effect=replace):
            self.assertFalse(self.install())
        self.assertEqual((self.folder / "loop.py").read_text(encoding="utf-8"), "OLD = True\n")
        self.assertEqual(selfupdate.version_in((self.folder / "__init__.py").read_text(encoding="utf-8")),
                         "brain-0.9.1")

    def test_a_new_brain_that_wont_run_is_put_back_and_not_taken_again(self):
        self.install()
        for _ in range(selfupdate.TRIES):                                   # stops before it settled
            self.assertTrue(selfupdate.started(self.folder, self.home, NEW, log=self.lines.append))
        self.assertFalse(selfupdate.started(self.folder, self.home, NEW, log=self.lines.append))
        self.assertEqual((self.folder / "loop.py").read_text(encoding="utf-8"), "OLD = True\n")
        self.assertEqual(selfupdate.version_in((self.folder / "__init__.py").read_text(encoding="utf-8")),
                         "brain-0.9.1")
        self.assertIn(NEW, selfupdate.refused(self.home))
        self.assertTrue(selfupdate.started(self.folder, self.home, "brain-0.9.1"))   # the one before runs

    def test_one_that_runs_stays(self):
        self.install()
        self.assertTrue(selfupdate.started(self.folder, self.home, NEW))
        selfupdate.settled(self.home, log=self.lines.append)
        self.assertFalse((self.home / selfupdate.TRIAL).exists())
        for _ in range(selfupdate.TRIES + 2):                               # later stops: not its fault
            self.assertTrue(selfupdate.started(self.folder, self.home, NEW))
        self.assertIn(f"[UPDATE] {NEW} is running fine: it stays", self.lines)

    def test_no_trial_no_counting(self):
        self.assertTrue(selfupdate.started(self.folder, self.home, VERSION))
        (self.home).mkdir()
        (self.home / selfupdate.TRIAL).write_text(json.dumps({"version": "brain-1.0.0", "starts": 9}))
        self.assertTrue(selfupdate.started(self.folder, self.home, VERSION))  # replaced by hand since
        self.assertFalse((self.home / selfupdate.TRIAL).exists())


class BrainTakesItTests(unittest.TestCase):
    """The loop asks the server, and only where something starts it again."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.monitor = mock.Mock()
        self.monitor.status.return_value = {"brain": NEW, "services": []}
        self.monitor.brain_update.return_value = b"zip"
        self.lines = []
        self.brain = loop.Brain(config.Config(api_base="x", token="y", home=self.home), ears=mock.Mock(ready=False),
                                server=mock.Mock(), monitor=self.monitor, mic=None, speaker=mock.Mock(),
                                log=self.lines.append)
        self.brain.online = True

    def test_a_watched_robot_takes_the_newer_brain(self):
        with mock.patch.object(loop, "watched", return_value=True), \
                mock.patch.object(selfupdate, "install", return_value=True) as install:
            self.assertTrue(selfupdate.check(self.brain))
        args = install.call_args.args
        self.assertEqual((args[0], args[1], args[2], args[4]), (b"zip", NEW, Path(loop.__file__).resolve().parent,
                                                                VERSION))

    def test_not_watched_turned_off_the_same_or_put_back_before_nothing_comes_down(self):
        cases = {
            "run by hand": (False, True, NEW, set()),
            "auto_update = no": (True, False, NEW, set()),
            "nothing newer": (True, True, VERSION, set()),
            "put back before": (True, True, NEW, {NEW}),
        }
        for why, (watched, auto, shipped, refused) in cases.items():
            with self.subTest(why):
                self.brain.cfg.auto_update = auto
                self.monitor.status.return_value = {"brain": shipped}
                self.monitor.brain_update.reset_mock()
                with mock.patch.object(loop, "watched", return_value=watched), \
                        mock.patch.object(selfupdate, "refused", return_value=refused):
                    self.assertFalse(selfupdate.check(self.brain))
                self.monitor.brain_update.assert_not_called()
        self.assertEqual(sum("didn't run here before" in line for line in self.lines), 1)

    def test_offline_it_waits(self):
        self.brain.online = False
        with mock.patch.object(loop, "watched", return_value=True):
            self.assertFalse(selfupdate.check(self.brain))
        self.monitor.status.assert_not_called()

    def test_said_at_start_comes_down_instead_when_watched(self):
        self.monitor.heartbeat.return_value = 200
        self.brain.online = None
        with mock.patch.object(loop, "watched", return_value=True), \
                mock.patch.object(selfupdate, "install", return_value=True) as install:
            self.brain.beat()
        install.assert_called_once()
        self.assertFalse(any("«⬇️ تحديث المخ»" in line for line in self.lines))

    def test_auto_update_in_brain_ini(self):
        with TemporaryDirectory() as tmp:
            ini = Path(tmp) / "brain.ini"
            ini.write_text("[server]\napi_base = x\ntoken = y\n[robot]\nauto_update = no\n", encoding="utf-8")
            self.assertFalse(config.load(ini).auto_update)
            ini.write_text("[server]\napi_base = x\ntoken = y\n", encoding="utf-8")
            self.assertTrue(config.load(ini).auto_update)


class ServerHandsItOutTests(ERPTenantTestCase):

    def setUp(self):
        branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-selfupdate", branch=branch)
        self.token = self.device.issue_token()
        self.device.save()

    def test_the_brain_it_ships_with_no_key_in_it(self):
        request = APIRequestFactory().post("/api/robot/v1/brain/update/", HTTP_X_ROBOT_TOKEN=self.token)
        resp = views.brain_update(request)
        self.assertEqual((resp.status_code, resp["Content-Type"], resp["X-Brain-Version"]),
                         (200, "application/zip", VERSION))
        names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
        self.assertNotIn("brain.ini", names)
        files = selfupdate.unpack(resp.content, VERSION)                    # what the brain takes in
        self.assertIn("loop.py", files)
        self.assertIn("selfupdate.py", files)
        self.assertEqual(resp.content[:2], brain_package.build("", "")[:2])

    def test_only_to_a_robot_with_its_key(self):
        request = APIRequestFactory().post("/api/robot/v1/brain/update/", HTTP_X_ROBOT_TOKEN="wrong")
        self.assertEqual(views.brain_update(request).status_code, 401)


if __name__ == "__main__":
    unittest.main()
