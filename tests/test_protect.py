# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
Protect local (0.3.0): the folder is the single source of truth.

With protection on (the default) nothing in the folder is ever replaced or deleted,
SYNC makes GitHub mirror the folder, and pulling needs an explicit one-time override
that backs up whatever it replaces.
"""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_ghsync import Base, SRC, ghsync  # noqa: E402
from fake_github import blob_sha  # noqa: E402


class ProtectBase(Base):
    def setUp(self):
        super().setUp()
        self.set_cfg(protect=None)            # key absent: the default must be "protected"

    def set_cfg(self, protect):
        cfg = {"owner": "me", "repo": "proj", "branch": "main"}
        if protect is not None:
            cfg["protect_local"] = protect
        ghsync.write_json(os.path.join(self.root, ".ghsync", "config.json"), cfg)

    def synced_unprotected(self, files):
        self.set_cfg(protect=False)
        self.synced(files)
        self.set_cfg(protect=None)

    def backups(self):
        d = os.path.join(self.root, ".ghsync", "backup")
        out = {}
        for dp, _, fs in os.walk(d):
            for f in fs:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, d).replace(os.sep, "/").split("/", 1)[1]
                with open(full, "rb") as fh:
                    out[rel] = fh.read()
        return out

    @contextlib.contextmanager
    def no_local_writes(self):
        """Fail the test if anything tries to replace or delete a project file."""
        def boom(*a, **k):
            raise AssertionError(f"local write attempted: {a[1] if len(a) > 1 else a}")
        with mock.patch.object(ghsync, "write_local", boom), \
                mock.patch.object(ghsync, "delete_local", boom):
            yield


class TestProtectedMirror(ProtectBase):
    def test_default_is_protected_and_github_edit_is_reverted(self):
        self.synced_unprotected({"a.txt": "mine", "b.txt": "b"})
        self.gh.commit_files({"a.txt": b"edited on github"}, keep=True)
        with self.no_local_writes():
            self.sync()
        self.assertEqual(self.read("a.txt"), b"mine")
        self.assertEqual(self.gh.files()["a.txt"], b"mine")

    def test_file_deleted_on_github_is_restored(self):
        self.synced_unprotected({"a.txt": "a", "keep.txt": "precious"})
        self.gh.commit_files({"keep.txt": None}, keep=True)
        with self.no_local_writes():
            self.sync()
        self.assertEqual(self.read("keep.txt"), b"precious")
        self.assertEqual(self.gh.files()["keep.txt"], b"precious")

    def test_github_only_leftovers_are_removed_from_github(self):
        self.synced_unprotected({"a.txt": "a"})
        self.gh.commit_files({"old/abandoned.py": b"x", "old/notes.md": b"y"}, keep=True)
        with self.no_local_writes():
            self.sync()
        self.assertEqual(set(self.gh.files()), {"a.txt"})
        self.assertFalse(self.exists("old"))

    def test_first_link_mirrors_folder_over_existing_repo(self):
        self.gh.commit_files({"README.md": b"old readme", "legacy/x.py": b"old", "a.txt": b"old a"})
        self.write("a.txt", "new a")
        self.write("src/app.py", "app")
        with self.no_local_writes():
            self.sync()
        self.assertEqual(self.gh.files(), {"a.txt": b"new a", "src/app.py": b"app"})
        self.assertFalse(self.exists("README.md"))

    def test_conflict_goes_to_local_without_touching_it(self):
        self.synced_unprotected({"c.txt": "base"})
        self.write("c.txt", "mine")
        self.gh.commit_files({"c.txt": b"theirs"}, keep=True)
        with self.no_local_writes():
            out = self.sync()
        self.assertNotIn("CONFLICT", out)
        self.assertEqual(self.read("c.txt"), b"mine")
        self.assertEqual(self.gh.files()["c.txt"], b"mine")

    def test_locally_deleted_file_is_still_deleted_on_github(self):
        self.synced_unprotected({"a.txt": "a", "gone.txt": "g"})
        os.remove(self.path("gone.txt"))
        self.sync()
        self.assertNotIn("gone.txt", self.gh.files())

    def test_held_files_on_github_are_left_alone(self):
        self.synced_unprotected({"a.txt": "a"})
        self.gh.commit_files({".env": b"SECRET", "CON.txt": b"x", "link": ("120000", b"/x")}, keep=True)
        self.sync()
        self.assertTrue({".env", "CON.txt"} <= set(self.gh.files()))
        self.assertIn("link", self.gh.flat())
        self.assertFalse(self.exists(".env"))

    def test_push_mode_does_not_revert_github_changes(self):
        self.synced_unprotected({"a.txt": "a", "b.txt": "b"})
        self.write("a.txt", "local edit")
        self.gh.commit_files({"b.txt": b"github edit"}, keep=True)
        with self.no_local_writes():
            self.sync(mode="push")
        self.assertEqual(self.gh.files(), {"a.txt": b"local edit", "b.txt": b"github edit"})
        self.assertEqual(self.read("b.txt"), b"b")

    def test_mirror_is_bound_to_the_confirmed_plan(self):
        self.synced_unprotected({"a.txt": "a"})
        root, cfg = ghsync.load_cfg(self.ns())
        snap = ghsync.gather(root, cfg)
        push, pull, _, _ = ghsync.plan_actions(snap, "sync", protect=True)
        digest = ghsync.plan_digest("sync", push, pull)
        self.gh.commit_files({"new-on-github.txt": b"x"}, keep=True)    # appears after review
        self.assert_fails(lambda: ghsync.run_sync(self.ns(expect=digest), "sync"))
        self.assertIn("new-on-github.txt", self.gh.files())


class TestProtectedRefusals(ProtectBase):
    def test_pull_is_refused_without_override(self):
        self.synced_unprotected({"a.txt": "mine"})
        self.gh.commit_files({"a.txt": b"github"}, keep=True)
        head = self.gh.ref
        with self.no_local_writes():
            out = self.assert_fails(lambda: ghsync.run_sync(self.ns(), "pull"))
        self.assertIn("Protect local", out)
        self.assertEqual(self.read("a.txt"), b"mine")
        self.assertEqual(self.gh.ref, head)

    def test_prefer_remote_is_refused_without_override(self):
        self.synced_unprotected({"c.txt": "base"})
        self.write("c.txt", "mine")
        self.gh.commit_files({"c.txt": b"theirs"}, keep=True)
        with self.no_local_writes():
            self.assert_fails(lambda: ghsync.run_sync(self.ns(prefer="remote"), "sync"))
            self.assert_fails(lambda: ghsync.run_sync(
                self.ns(resolve={"c.txt": "remote"}, only={"c.txt"}), "sync"))
        self.assertEqual(self.read("c.txt"), b"mine")

    def test_engine_refuses_a_pull_set_even_if_planning_were_bypassed(self):
        self.synced_unprotected({"a.txt": "mine"})
        self.gh.commit_files({"a.txt": b"github"}, keep=True)
        real = ghsync.plan_actions

        def leaky(snap, mode, *a, **k):     # simulate a future planning bug
            k["protect"] = False
            return real(snap, mode, *a, **k)
        with mock.patch.object(ghsync, "plan_actions", leaky), self.no_local_writes():
            out = self.assert_fails(lambda: ghsync.run_sync(self.ns(), "sync"))
        self.assertIn("Protect local", out)
        self.assertEqual(self.read("a.txt"), b"mine")
        # ...and the pull code refuses on its own, even if called directly.
        with self.assertRaises(ghsync.ProtectedLocal):
            ghsync.do_pull(self.root, {"owner": "me", "repo": "proj", "branch": "main"},
                           {"a.txt": blob_sha(b"github")}, {"a.txt": blob_sha(b"mine")})
        self.assertEqual(self.read("a.txt"), b"mine")


class TestOverrideAndBackups(ProtectBase):
    def test_override_pulls_once_and_backs_up(self):
        self.synced_unprotected({"a.txt": "mine", "b.txt": "b", "c.txt": "c"})
        self.gh.commit_files({"a.txt": b"github", "c.txt": None, "new.txt": b"n"}, keep=True)
        self.sync(mode="pull", allow_local_overwrite=True)
        self.assertEqual(self.read("a.txt"), b"github")
        self.assertFalse(self.exists("c.txt"))
        self.assertEqual(self.read("new.txt"), b"n")
        self.assertEqual(self.backups(), {"a.txt": b"mine", "c.txt": b"c"})
        # the override did not persist
        self.gh.commit_files({"b.txt": b"github b"}, keep=True)
        self.sync()
        self.assertEqual(self.read("b.txt"), b"b")
        self.assertEqual(self.gh.files()["b.txt"], b"b")

    def test_override_resolves_selected_conflict_to_github(self):
        self.synced_unprotected({"c.txt": "base"})
        self.write("c.txt", "mine")
        self.gh.commit_files({"c.txt": b"theirs"}, keep=True)
        self.sync(resolve={"c.txt": "remote"}, only={"c.txt"}, allow_local_overwrite=True)
        self.assertEqual(self.read("c.txt"), b"theirs")
        self.assertEqual(self.backups(), {"c.txt": b"mine"})

    def test_unprotected_pull_also_backs_up(self):
        self.set_cfg(protect=False)
        self.synced({"a.txt": "old"})
        self.gh.commit_files({"a.txt": b"new"}, keep=True)
        self.sync()
        self.assertEqual(self.read("a.txt"), b"new")
        self.assertEqual(self.backups(), {"a.txt": b"old"})

    def test_backup_failure_skips_that_file(self):
        self.synced_unprotected({"a.txt": "mine"})
        self.gh.commit_files({"a.txt": b"github"}, keep=True)
        with mock.patch.object(ghsync, "_write_backup", side_effect=OSError("disk full")):
            out = self.sync(mode="pull", allow_local_overwrite=True)
        self.assertEqual(self.read("a.txt"), b"mine")
        self.assertIn("couldn't back up a.txt", out)

    def test_backups_are_never_synced(self):
        self.set_cfg(protect=False)
        self.synced({"a.txt": "old"})
        self.gh.commit_files({"a.txt": b"new"}, keep=True)
        self.sync()
        self.sync()
        self.assertFalse(any(p.startswith(".ghsync") for p in self.gh.files()))


class TestProtectConfig(ProtectBase):
    def cfg(self):
        with open(os.path.join(self.root, ".ghsync", "config.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_save_cfg_defaults_to_protected(self):
        ghsync.save_cfg(self.root, "me", "proj", "main")
        self.assertIs(self.cfg()["protect_local"], True)

    def test_set_protect_round_trip(self):
        ghsync.set_protect(self.root, False)
        self.assertIs(self.cfg()["protect_local"], False)
        ghsync.set_protect(self.root, True)
        self.assertIs(self.cfg()["protect_local"], True)
        self.assertEqual(self.cfg()["owner"], "me")

    def test_non_boolean_setting_is_rejected(self):
        ghsync.write_json(os.path.join(self.root, ".ghsync", "config.json"),
                          {"owner": "me", "repo": "proj", "branch": "main", "protect_local": "no"})
        self.assert_fails(lambda: ghsync.run_sync(self.ns(), "sync"))

    def test_status_reports_protection(self):
        self.synced_unprotected({"a.txt": "a"})
        self.gh.commit_files({"a.txt": b"github"}, keep=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ghsync.cmd_status(argparse.Namespace(dir=self.root))
        out = buf.getvalue()
        self.assertIn("Protect local: ON", out)
        self.assertIn("revert", out.lower())


class TestProtectCli(ProtectBase):
    def setUp(self):
        super().setUp()
        ghsync.api = self._api
        self.gh._mod = None
        from fake_github import serve_http
        self.srv, self.url = serve_http(self.gh)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def cli(self, *argv):
        env = dict(os.environ, GHSYNC_API=self.url, GITHUB_TOKEN="t", PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-I", os.path.join(SRC, "ghsync.py"), "-C", self.root, *argv],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_cli_pull_needs_flag_and_protect_command_works(self):
        self.write("a.txt", "mine")
        self.assertEqual(self.cli("sync", "--yes").returncode, 0)
        self.gh.commit_files({"a.txt": b"github"}, keep=True)
        r = self.cli("pull", "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Protect local", r.stderr)
        self.assertEqual(self.read("a.txt"), b"mine")
        r = self.cli("protect")
        self.assertIn("ON", r.stdout)
        r = self.cli("pull", "--yes", "--allow-local-overwrite")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("a.txt"), b"github")
        r = self.cli("protect", "off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OFF", self.cli("protect").stdout)


if __name__ == "__main__":
    unittest.main()
