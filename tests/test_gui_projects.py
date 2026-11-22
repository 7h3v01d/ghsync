# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
Multi-project GUI (0.4.0), driven offscreen against several fake GitHub repos.

Covers the 0.4.0 done-conditions that need the window: projects persist across a
restart, statuses map to the right project, switching while background work is in
flight never shows one project's results under another, output from parallel work
stays out of a job's log, and destination clashes block syncing.
"""
import argparse
import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="ghsync-qtcfg-")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("GHSYNC_SRC") or os.path.dirname(HERE)
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

try:
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication, QMessageBox
    HAVE_QT = True
except ImportError:          # pragma: no cover
    HAVE_QT = False

import ghsync  # noqa: E402
from multi_fake import MultiFake  # noqa: E402


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class GuiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import ghsync_gui
        cls.gui = ghsync_gui

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ghsync-mp-")
        self._saved_env = {k: os.environ.get(k) for k in ("GHSYNC_HOME", "GITHUB_TOKEN")}
        os.environ["GHSYNC_HOME"] = os.path.join(self.tmp, "home")
        os.environ["GITHUB_TOKEN"] = "t"
        QSettings("ghsync", "gui").clear()
        self._api = ghsync.api
        self.gh = MultiFake().install(ghsync)
        self.asked = []
        self._q, self._exec = QMessageBox.question, QMessageBox.exec
        QMessageBox.question = lambda *a, **k: (self.asked.append(a[2] if len(a) > 2 else ""),
                                                QMessageBox.StandardButton.Yes)[1]
        QMessageBox.exec = lambda box: (self.asked.append(box.text()), QMessageBox.StandardButton.Yes)[1]
        self.wins = []

    def tearDown(self):
        for w in self.wins:
            w._quitting = True
            w.timer.stop()
            self.wait_idle(w)
            w.close()
        QMessageBox.question, QMessageBox.exec = self._q, self._exec
        ghsync.api = self._api
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def project(self, name, files, repo=None, sync=True):
        """A folder linked to fake repo `repo` (default: name), synced once if asked."""
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        repo = repo or name
        self.gh.repo(repo)
        self.gh.install(ghsync)
        ghsync.write_json(os.path.join(d, ".ghsync", "config.json"),
                          {"owner": "me", "repo": repo, "branch": "main"})
        for p, data in files.items():
            full = os.path.join(d, *p.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(data)
        if sync:
            with contextlib.redirect_stdout(io.StringIO()):
                ghsync.run_sync(argparse.Namespace(dir=d, message=None, dry_run=False, yes=True,
                                                   prefer=None, resolve=None, only=None), "sync")
        return d

    def open(self, folder=None):
        w = self.gui.Main(folder)
        w.timer.stop()
        w.watch_on = False
        self.wins.append(w)
        self.wait_idle(w)
        return w

    def wait_idle(self, w, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if (not w.busy and not w.watch_busy and not w._workers
                    and not getattr(w, "_queued_action", False)
                    and not getattr(w, "_status_busy", False)):
                self.app.processEvents()
                return
            time.sleep(0.01)
        self.fail("GUI work did not finish")

    def rows(self, w):
        t = w.proj_table
        return {t.item(r, 0).text(): t.item(r, 1).text() for r in range(t.rowCount())}


# ----------------------------------------- groundwork (both fail on 0.3.4)
class TestIsolation(GuiBase):
    def test_output_of_parallel_work_stays_out_of_the_job_log(self):
        a = self.project("a", {"x.txt": "x"})
        w = self.open(a)
        stop = threading.Event()

        def noise():
            while not stop.is_set():
                print("NOISE-FROM-ANOTHER-THREAD")
                time.sleep(0.005)

        def job():
            print("JOB-LINE-1")
            time.sleep(0.3)
            print("JOB-LINE-2")

        t = threading.Thread(target=noise, daemon=True)
        t.start()
        w.run_job(job, "Testing")
        self.wait_idle(w)
        stop.set()
        t.join()
        log = w.log.toPlainText()
        self.assertIn("JOB-LINE-2", log)
        self.assertNotIn("NOISE-FROM-ANOTHER-THREAD", log)

    def test_switching_project_discards_the_old_projects_scan(self):
        a = self.project("a", {"x.txt": "x"})
        b = self.project("b", {"y.txt": "y"})
        w = self.open(a)
        real = self.gui.compute_plan

        def slow(root):
            if os.path.basename(root) == "a":
                time.sleep(0.4)
            return real(root)
        self.gui.compute_plan = slow
        shown = []
        real_set = w.repo_lbl.setText
        w.repo_lbl.setText = lambda text: (shown.append((os.path.basename(w.root), text)), real_set(text))
        try:
            w.watch_refresh()                      # background scan of A in flight...
            w.load_folder(b)                       # ...when the user switches to B
            self.wait_idle(w)
            time.sleep(0.5)
            self.wait_idle(w)
        finally:
            self.gui.compute_plan = real
        wrong = [t for root, t in shown if root == "b" and "me/a" in t]
        self.assertEqual(wrong, [], "project A's scan was shown while B was selected")
        self.assertEqual(w.plan["cfg"]["repo"], "b")


# --------------------------------------------------- dashboard behaviour
class TestDashboard(GuiBase):
    def test_projects_are_listed_after_a_restart(self):
        a, b, c = (self.project(n, {"f.txt": n}) for n in ("alpha", "bravo", "charlie"))
        w = self.open(a)
        for p in (b, c):
            self.assertIsNotNone(w.add_project_path(p))
        self.wait_idle(w)
        w._quitting = True
        w.close()
        w2 = self.open()
        self.assertEqual(set(self.rows(w2)), {"alpha", "bravo", "charlie"})

    def test_status_rows_match_their_projects(self):
        clean = self.project("clean", {"f.txt": "f"})
        local = self.project("local", {"f.txt": "f"})
        remote = self.project("remote", {"f.txt": "f"})
        w = self.open(clean)
        w.add_project_path(local)
        w.add_project_path(remote)
        with open(os.path.join(local, "f.txt"), "w") as f:
            f.write("edited")
        self.gh.repo("remote").commit_files({"g.txt": b"g"}, keep=True)
        w.check_all()
        self.wait_idle(w)
        rows = self.rows(w)
        self.assertEqual(rows["clean"], "IN SYNC")
        self.assertEqual(rows["local"], "1 LOCAL CHANGE")
        self.assertEqual(rows["remote"], "GITHUB CHANGED")

    def test_background_sweep_never_touches_the_open_project(self):
        a = self.project("a", {"x.txt": "x"})
        b = self.project("b", {"y.txt": "y"})
        swept = []
        real = self.gui.gp.project_status
        self.gui.gp.project_status = lambda root: (swept.append(os.path.basename(root)), real(root))[1]
        try:
            w = self.open(a)
            w.add_project_path(b)
            self.wait_idle(w)
            w.check_all()
            self.wait_idle(w)
        finally:
            self.gui.gp.project_status = real
        self.assertNotIn("a", swept)
        self.assertIn("b", swept)
        with open(os.path.join(a, "new.txt"), "w") as f:
            f.write("n")
        w.refresh()
        self.wait_idle(w)
        self.assertEqual(self.rows(w)["a"], "1 PENDING")

    def test_selecting_a_project_opens_it(self):
        a = self.project("a", {"x.txt": "x"})
        b = self.project("b", {"y.txt": "y"})
        w = self.open(a)
        w.add_project_path(b)
        self.wait_idle(w)
        w.select_project_root(b)
        self.wait_idle(w)
        self.assertEqual(w.root, os.path.realpath(b))
        self.assertEqual(w.plan["cfg"]["repo"], "b")

    def test_switching_is_refused_while_a_sync_runs(self):
        a = self.project("a", {"x.txt": "x"})
        b = self.project("b", {"y.txt": "y"})
        w = self.open(a)
        w.add_project_path(b)
        self.wait_idle(w)
        real = ghsync.run_sync
        gate = threading.Event()
        ghsync.run_sync = lambda ns, mode: (gate.wait(5), real(ns, mode))[1]
        try:
            with open(os.path.join(a, "x.txt"), "w") as f:
                f.write("changed")
            w.refresh()
            self.wait_idle(w)
            w.do_sync("sync")
            self.app.processEvents()
            w.select_project_root(b)
            self.assertEqual(os.path.basename(w.root), "a")
            gate.set()
            self.wait_idle(w)
        finally:
            ghsync.run_sync = real
        self.assertEqual(self.gh.repo("a").files()["x.txt"], b"changed")

    def test_clashing_projects_cannot_sync(self):
        a = self.project("a", {"x.txt": "x"})
        b = self.project("b", {"y.txt": "y"})
        w = self.open(a)
        w.add_project_path(b)
        self.wait_idle(w)
        # re-linked outside the GUI (e.g. the CLI) to the same repo and branch as A
        ghsync.write_json(os.path.join(b, ".ghsync", "config.json"),
                          {"owner": "me", "repo": "a", "branch": "main"})
        with open(os.path.join(b, "y.txt"), "w") as f:
            f.write("would overwrite A's repo")
        head = self.gh.repo("a").ref
        w.select_project_root(b)
        self.wait_idle(w)
        w.do_sync("sync")
        self.wait_idle(w)
        self.assertEqual(self.gh.repo("a").ref, head)
        self.assertIn("also syncs to me/a", w.log.toPlainText())
        w.check_all()
        self.wait_idle(w)
        self.assertEqual(self.rows(w)["b"], "CLASH")

    def test_linking_to_a_destination_already_in_use_is_refused(self):
        a = self.project("a", {"x.txt": "x"})
        c = os.path.join(self.tmp, "c")
        os.makedirs(c)
        w = self.open(a)
        w.add_project_path(c)
        w.select_project_root(c)
        self.wait_idle(w)
        w.link_name.setText("me/a")
        w.do_link()
        self.wait_idle(w)
        self.assertFalse(os.path.exists(os.path.join(c, ".ghsync", "config.json")))
        self.assertIn("already syncs to me/a", w.log.toPlainText())

    def test_nested_folder_is_refused_with_a_message(self):
        a = self.project("a", {"x.txt": "x"})
        inner = os.path.join(a, "sub")
        os.makedirs(inner)
        w = self.open(a)
        self.assertIsNone(w.add_project_path(inner))
        self.assertIn("inside another project", w.log.toPlainText())
        self.assertEqual(len(self.rows(w)), 1)

    def test_remove_keeps_files_and_config(self):
        a = self.project("a", {"x.txt": "x"})
        b = self.project("b", {"y.txt": "y"})
        w = self.open(a)
        w.add_project_path(b)
        self.wait_idle(w)
        w.remove_project_root(b)
        self.wait_idle(w)
        self.assertEqual(set(self.rows(w)), {"a"})
        self.assertTrue(os.path.exists(os.path.join(b, "y.txt")))
        self.assertTrue(os.path.exists(os.path.join(b, ".ghsync", "config.json")))
        self.assertEqual(self.gh.repo("b").files(), {"y.txt": b"y"})

    def test_old_single_folder_setting_is_migrated(self):
        a = self.project("a", {"x.txt": "x"})
        QSettings("ghsync", "gui").setValue("folder", a)
        w = self.open()
        self.assertEqual(set(self.rows(w)), {"a"})
        self.assertEqual(w.root, os.path.realpath(a))

    def test_sync_still_goes_through_the_protected_engine(self):
        a = self.project("a", {"x.txt": "x"})
        self.gh.repo("a").commit_files({"x.txt": b"github edit", "junk.txt": b"j"}, keep=True)
        w = self.open(a)
        w.do_sync("sync")
        self.wait_idle(w)
        self.assertEqual(self.gh.repo("a").files(), {"x.txt": b"x"})
        with open(os.path.join(a, "x.txt"), "rb") as f:
            self.assertEqual(f.read(), b"x")


if __name__ == "__main__":
    unittest.main()


class TestStartupAdmission(GuiBase):
    """GHS-015: a folder that fails registration must not become the active project."""

    def test_nested_folder_given_at_startup_is_not_activated(self):
        sub = self.project("a/sub", {"y.txt": "y"}, repo="subrepo")   # nested, valid config...
        a = self.project("a", {"x.txt": "x"})                          # ...made before A was listed
        w0 = self.open(a)
        w0._quitting = True
        w0.close()
        w = self.open(sub)
        self.assertNotEqual(w.root, os.path.realpath(sub))
        self.assertEqual(w.root, os.path.realpath(a))
        self.assertIn("inside another project", w.log.toPlainText())
        head = self.gh.repo("subrepo").ref
        w.do_sync("sync")
        self.wait_idle(w)
        self.assertEqual(self.gh.repo("subrepo").ref, head)

    def test_rejected_folder_with_empty_list_shows_no_project(self):
        w = self.open(os.path.join(self.tmp, "does-not-exist"))
        self.assertIsNone(w.root)
        self.assertEqual(w.stack.currentIndex(), 0)
        self.assertIn("ADD", w.c_sub.text())
