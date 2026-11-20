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
        self.asked = []
        self._q = QMessageBox.question
        QMessageBox.question = lambda *a, **k: (self.asked.append(a[2] if len(a) > 2 else ""),
                                                QMessageBox.StandardButton.Yes)[1]

    def tearDown(self):
        QMessageBox.question = self._q
        if getattr(self, "win", None):
            self.win._quitting = True
            self.win.timer.stop()
            self.win.close()
        super().tearDown()

    def wait_idle(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if not self.win.busy and not self.win.watch_busy and not self.win._workers:
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
