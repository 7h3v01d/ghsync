# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
Offscreen GUI tests (skipped when PyQt6 isn't installed).

Drives the real Main window against the fake GitHub to check that what the user
confirms is what runs (GHS-007).
"""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_cfg_home = tempfile.mkdtemp(prefix="ghsync-qtcfg-")
os.environ["XDG_CONFIG_HOME"] = _cfg_home          # keep QSettings out of the real profile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("GHSYNC_SRC") or os.path.dirname(HERE)
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox
    HAVE_QT = True
except ImportError:          # pragma: no cover
    HAVE_QT = False

from test_ghsync import Base  # noqa: E402
import ghsync  # noqa: E402,F401 - imported from SRC first so the GUI binds to this copy


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestGuiPlanBinding(Base):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import ghsync_gui
        cls.gui = ghsync_gui

    def setUp(self):
        super().setUp()
        os.environ["GITHUB_TOKEN"] = "test-token"
        os.environ["GHSYNC_HOME"] = os.path.join(self.tmp, "ghsync-home")   # per-test project list
        self.asked = []
        self._q, self._exec = QMessageBox.question, QMessageBox.exec
        self.answer = QMessageBox.StandardButton.Yes
        QMessageBox.question = lambda *a, **k: (self.asked.append(a[2] if len(a) > 2 else ""),
                                                self.answer)[1]
        QMessageBox.exec = lambda box: (self.asked.append(box.text()), self.answer)[1]

    def tearDown(self):
        QMessageBox.question, QMessageBox.exec = self._q, self._exec
        if getattr(self, "win", None):
            self.win._quitting = True
            self.win.timer.stop()
            self.win.close()
        super().tearDown()

    def wait_idle(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if (not self.win.busy and not self.win.watch_busy and not self.win._workers
                    and not getattr(self.win, "_queued_action", False)
                    and not getattr(self.win, "_status_busy", False)):
                self.app.processEvents()
                return
            time.sleep(0.01)
        self.fail("GUI job did not finish")

    def open(self):
        self.win = self.gui.Main(self.root)
        self.win.timer.stop()                # no background polling during the test
        self.win.watch_on = False
        self.wait_idle()
        self.assertIsNotNone(self.win.plan)

    def test_deletion_appearing_after_review_is_not_executed(self):
        self.synced({"a.txt": "a", "b.txt": "b"})
        self.write("a.txt", "changed")
        self.open()                           # user sees: push a.txt, nothing to delete
        os.remove(self.path("b.txt"))         # ...then b.txt disappears before they click SYNC
        self.win.do_sync("sync")
        self.wait_idle()
        self.assertIn("b.txt", self.gh.files(), "a deletion the user never saw was executed")
        self.assertEqual(self.asked, [])
        # The GUI refreshed after refusing, so the deletion is now visible for review.
        self.assertIn("b.txt", self.win.plan["push"])

    def test_confirmed_deletion_runs(self):
        self.synced({"a.txt": "a", "b.txt": "b"})
        os.remove(self.path("b.txt"))
        self.open()
        self.win.do_sync("sync")
        self.wait_idle()
        self.assertEqual(len(self.asked), 1)
        self.assertIn("b.txt", self.asked[0])
        self.assertNotIn("b.txt", self.gh.files())

    def test_keep_github_resolution_is_bound(self):
        self.synced({"c.txt": "base"})
        self.write("c.txt", "mine")
        self.gh.commit_files({"c.txt": b"theirs"}, keep=True)
        self.open()
        self.assertIn("c.txt", self.win.plan["conflict"])
        self.win.table.selectAll()
        self.win.resolve("remote")
        self.wait_idle()
        self.assertEqual(self.read("c.txt"), b"theirs")

    def test_held_files_are_logged(self):
        self.synced({"a.txt": "a"})
        self.gh.commit_files({".ghsync/config.json": b"{}"}, keep=True)
        self.open()
        log = self.win.log.toPlainText()
        self.assertIn(".ghsync/config.json", log)
        self.assertIn("held", self.win.status.text().lower())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestGuiProtectLocal(TestGuiPlanBinding):
    """Protect local in the real window (config without the key = protected)."""
    test_deletion_appearing_after_review_is_not_executed = None
    test_confirmed_deletion_runs = None
    test_keep_github_resolution_is_bound = None
    test_held_files_are_logged = None

    def protect(self, value):
        cfg = {"owner": "me", "repo": "proj", "branch": "main"}
        if value is not None:
            cfg["protect_local"] = value
        ghsync.write_json(os.path.join(self.root, ".ghsync", "config.json"), cfg)

    def start(self, files, github_changes):
        self.protect(False)
        self.synced(files)
        self.protect(None)
        self.gh.commit_files(github_changes, keep=True)
        self.open()

    def kinds(self):
        return {self.win.table.item(r, 2).text(): self.win.table.item(r, 0).data(256)
                for r in range(self.win.table.rowCount())}

    def select(self, path):
        for r in range(self.win.table.rowCount()):
            if self.win.table.item(r, 2).text() == path:
                self.win.table.selectRow(r)

    def test_sync_mirrors_and_never_touches_folder(self):
        self.start({"a.txt": "mine", "b.txt": "b"}, {"a.txt": b"web edit", "junk.txt": b"x"})
        self.assertTrue(self.win.protect_chk.isChecked())
        self.assertEqual(self.kinds(), {"a.txt": "mirror", "junk.txt": "mirror"})
        self.win.do_sync("sync")
        self.wait_idle()
        self.assertEqual(self.gh.files(), {"a.txt": b"mine", "b.txt": b"b"})
        self.assertEqual(self.read("a.txt"), b"mine")
        self.assertTrue(any("GitHub: junk.txt" in t for t in self.asked))

    def test_pull_override_declined_changes_nothing(self):
        self.start({"a.txt": "mine"}, {"a.txt": b"github"})
        self.assertTrue(self.win.b_pull.isEnabled())
        self.answer = QMessageBox.StandardButton.No
        self.win.do_sync("pull")
        self.wait_idle()
        self.assertEqual(self.read("a.txt"), b"mine")
        self.assertIn("replace: a.txt", self.asked[-1])

    def test_pull_override_accepted_backs_up_and_stays_protected(self):
        self.start({"a.txt": "mine"}, {"a.txt": b"github"})
        self.win.do_sync("pull")
        self.wait_idle()
        self.assertEqual(self.read("a.txt"), b"github")
        bdir = os.path.join(self.root, ".ghsync", "backup")
        self.assertEqual(len(os.listdir(bdir)), 1)
        self.assertTrue(self.win.protect_chk.isChecked())
        self.assertTrue(ghsync.protect_enabled(self.config()))

    def test_keep_github_on_a_mirror_row(self):
        self.start({"a.txt": "mine", "b.txt": "b"}, {"a.txt": b"github a", "b.txt": b"github b"})
        self.select("a.txt")
        self.assertTrue(self.win.b_keep_remote.isEnabled())
        self.win.resolve("remote")
        self.wait_idle()
        self.assertEqual(self.read("a.txt"), b"github a")
        self.assertEqual(self.read("b.txt"), b"b")             # only the selected file

    def test_turning_protection_off_needs_confirmation(self):
        self.start({"a.txt": "a"}, {})
        self.answer = QMessageBox.StandardButton.No
        self.win.protect_chk.setChecked(False)
        self.assertTrue(self.win.protect_chk.isChecked())
        self.assertTrue(ghsync.protect_enabled(self.config()))
        self.answer = QMessageBox.StandardButton.Yes
        self.win.protect_chk.setChecked(False)
        self.wait_idle()
        self.assertFalse(ghsync.protect_enabled(self.config()))
        self.assertFalse(self.win.plan["protect"])

    def test_push_button_sends_conflicts_shown_in_the_list(self):
        self.protect(None)
        self.gh.commit_files({"a.py": b"github a", "b.py": b"github b"})
        self.write("a.py", "my a")
        self.write("b.py", "my b")
        self.open()
        self.assertEqual(set(self.kinds()), {"a.py", "b.py"})
        self.win.do_sync("push")
        self.wait_idle()
        self.assertEqual(self.gh.files(), {"a.py": b"my a", "b.py": b"my b"})

    def test_confirmed_sync_survives_a_refresh_started_by_closing_the_dialog(self):
        # Closing the dialog re-activates the window, whose focus-refresh used to start a
        # scan first; the confirmed sync then found the GUI busy and was silently dropped.
        self.start({"a.txt": "a"}, {"old/home.js": b"x", "old/scan.js": b"y"})

        def answer_and_refocus(*a, **k):
            self.asked.append(a[2] if len(a) > 2 else "")
            self.win.last_refresh = 0
            self.win.refresh()                      # what window re-activation triggers
            return QMessageBox.StandardButton.Yes
        QMessageBox.question = answer_and_refocus
        self.win.do_sync("sync")
        self.wait_idle()
        self.assertEqual(set(self.gh.files()), {"a.txt"})
        self.assertIn("> SYNCING", self.win.log.toPlainText())

    def test_dialog_closing_does_not_trigger_focus_refresh(self):
        self.start({"a.txt": "a"}, {"junk.txt": b"x"})
        self.win.last_refresh = 0
        scans = []
        real = self.win.refresh
        self.win.refresh = lambda: (scans.append(1), real())
        from PyQt6.QtCore import QEvent

        def answer(*a, **k):
            self.win.changeEvent(QEvent(QEvent.Type.ActivationChange))
            return QMessageBox.StandardButton.Yes
        QMessageBox.question = answer
        with unittest.mock.patch.object(type(self.win), "isActiveWindow", lambda _: True):
            self.win.do_sync("sync")
            refreshes_during_dialog = len(scans)
        self.wait_idle()
        self.assertEqual(refreshes_during_dialog, 0)
        self.assertNotIn("junk.txt", self.gh.files())
