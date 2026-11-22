# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
GHS-014 (0.4.1): destination ownership is enforced by the engine itself, so the CLI
(and anything else calling run_sync/cmd_link) obeys the same rule as the GUI: a folder
may not sync to a repo + branch that another registered project owns.
"""
import argparse
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_ghsync import Base, SRC, ghsync  # noqa: E402
import ghsync_projects as gp  # noqa: E402


class GuardBase(Base):
    def setUp(self):
        super().setUp()
        self.synced({"file.txt": "a"})               # project A -> me/proj @ main
        gp.add_project(self.root)
        self.b = os.path.join(self.tmp, "b")
        os.makedirs(self.b)
        with open(os.path.join(self.b, "file.txt"), "w") as f:
            f.write("b")

    def link_b(self, repo="proj", branch="main"):
        ghsync.write_json(os.path.join(self.b, ".ghsync", "config.json"),
                          {"owner": "me", "repo": repo, "branch": branch, "protect_local": False})

    def ns_b(self, **kw):
        d = dict(dir=self.b, message=None, dry_run=False, yes=True, prefer=None, resolve=None, only=None)
        d.update(kw)
        return argparse.Namespace(**d)


class TestEngineGuard(GuardBase):
    def test_registered_project_relinked_to_owned_destination_cannot_sync(self):
        gp.add_project(self.b)            # registered while unlinked...
        self.link_b()                     # ...then re-linked outside the GUI
        head = self.gh.ref
        out = self.assert_fails(lambda: ghsync.run_sync(self.ns_b(), "sync"))
        self.assertIn("also syncs to me/proj", out)
        self.assertEqual(self.gh.ref, head)
        self.assertEqual(self.gh.files(), {"file.txt": b"a"})

    def test_unregistered_folder_cannot_sync_to_an_owned_destination(self):
        self.link_b()
        head = self.gh.ref
        for mode in ("push", "pull", "sync"):
            self.assert_fails(lambda: ghsync.run_sync(self.ns_b(), mode))
        self.assertEqual(self.gh.ref, head)

    def test_owner_and_repo_case_do_not_matter(self):
        ghsync.write_json(os.path.join(self.b, ".ghsync", "config.json"),
                          {"owner": "ME", "repo": "Proj", "branch": "main"})
        out = self.assert_fails(lambda: ghsync.run_sync(self.ns_b(), "push"))
        self.assertIn("also syncs to me/proj", out)

    def test_other_branch_and_owner_itself_still_sync(self):
        self.link_b(branch="dev")
        self.gh.branch = "main"           # fake serves only main; the guard runs before any request
        try:
            ghsync.run_sync(self.ns_b(dry_run=True), "sync")
        except SystemExit:
            pass                          # fake has no 'dev' branch; what matters is no clash refusal
        except ghsync.GhsyncError as e:
            self.assertNotIn("also syncs to", str(e))
        self.write("file.txt", "a2")
        self.sync()                       # the owner, A, is unaffected
        self.assertEqual(self.gh.files()["file.txt"], b"a2")

    def test_dry_run_and_status_stay_available(self):
        self.link_b()
        ghsync.run_sync(self.ns_b(dry_run=True), "sync")    # read-only: allowed
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            ghsync.cmd_status(argparse.Namespace(dir=self.b))

    def test_damaged_registry_fails_closed_for_syncing(self):
        self.link_b(repo="other")
        with open(gp.registry_path(), "w") as f:
            f.write("{broken")
        out = self.assert_fails(lambda: ghsync.run_sync(self.ns_b(), "sync"))
        self.assertIn("project list", out)

    def test_no_registry_at_all_means_no_restriction(self):
        os.remove(gp.registry_path())
        self.write("file.txt", "a3")
        self.sync()
        self.assertEqual(self.gh.files()["file.txt"], b"a3")

    def test_cmd_link_refuses_an_owned_destination(self):
        args = argparse.Namespace(dir=self.b, repo="me/proj", branch=None)
        self.assert_fails(lambda: ghsync.cmd_link(args))
        self.assertFalse(os.path.exists(os.path.join(self.b, ".ghsync", "config.json")))


class TestCliGuard(GuardBase):
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

    def cli(self, folder, *argv):
        env = dict(os.environ, GHSYNC_API=self.url, GITHUB_TOKEN="t", PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-I", os.path.join(SRC, "ghsync.py"), "-C", folder, *argv],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_cli_sync_into_an_owned_destination_is_refused(self):
        # the reviewer's reproduction: registry knows about the clash, CLI used to ignore it
        gp.add_project(self.b)
        self.link_b()
        self.assertTrue(gp.clashes(gp.load_registry()))
        r = self.cli(self.b, "sync", "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("also syncs to me/proj", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.gh.files(), {"file.txt": b"a"})

    def test_cli_link_into_an_owned_destination_is_refused(self):
        r = self.cli(self.b, "link", "me/proj")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already syncs to me/proj", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.b, ".ghsync", "config.json")))


class TestNestedRoots(GuardBase):
    """GHS-016: the 'no nested projects' rule holds for every entry point, not just the GUI."""

    def nested(self, inside_root=True, repo="nested"):
        d = os.path.join(self.root, "nested") if inside_root else self.tmp
        os.makedirs(d, exist_ok=True)
        ghsync.write_json(os.path.join(d, ".ghsync", "config.json"),
                          {"owner": "me", "repo": repo, "branch": "main", "protect_local": False})
        return d

    def ns_for(self, d, **kw):
        a = dict(dir=d, message=None, dry_run=False, yes=True, prefer=None, resolve=None, only=None)
        a.update(kw)
        return argparse.Namespace(**a)

    def test_folder_inside_a_listed_project_cannot_sync(self):
        d = self.nested()
        with self.assertRaises(gp.RegistryError):
            gp.add_project(d)                        # the registry already refused it...
        for mode in ("push", "pull", "sync"):        # ...and now the engine does too
            out = self.assert_fails(lambda: ghsync.run_sync(self.ns_for(d), mode))
            self.assertIn("inside the listed project", out)

    def test_folder_containing_a_listed_project_cannot_sync(self):
        d = self.nested(inside_root=False)
        out = self.assert_fails(lambda: ghsync.run_sync(self.ns_for(d), "sync"))
        self.assertIn("contains the listed project", out)

    def test_nested_folder_cannot_be_linked(self):
        d = os.path.join(self.root, "sub")
        os.makedirs(d)
        out = self.assert_fails(lambda: ghsync.cmd_link(argparse.Namespace(dir=d, repo="me/other", branch=None)))
        self.assertIn("inside the listed project", out)
        self.assertFalse(os.path.exists(os.path.join(d, ".ghsync", "config.json")))

    def test_listed_project_and_unrelated_siblings_are_unaffected(self):
        sib = self.root + "2"                         # shares a name prefix, not nested
        os.makedirs(sib)
        ghsync.write_json(os.path.join(sib, ".ghsync", "config.json"),
                          {"owner": "me", "repo": "elsewhere", "branch": "main"})
        ghsync.destination_guard(sib)                 # no refusal
        self.write("file.txt", "listed project still syncs")
        self.sync()
        self.assertEqual(self.gh.files()["file.txt"], b"listed project still syncs")

    def test_read_only_commands_stay_available(self):
        d = self.nested()
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                ghsync.run_sync(self.ns_for(d, dry_run=True), "sync")
            except (SystemExit, ghsync.ApiError):
                pass                                  # fake has no 'nested' repo; only the guard matters
            except ghsync.GhsyncError as e:
                self.assertNotIn("listed project", str(e))


class TestNestedCli(GuardBase):
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

    def test_cli_sync_of_a_nested_folder_is_refused(self):
        d = os.path.join(self.root, "nested")
        os.makedirs(d)
        ghsync.write_json(os.path.join(d, ".ghsync", "config.json"),
                          {"owner": "me", "repo": "nested", "branch": "main"})
        env = dict(os.environ, GHSYNC_API=self.url, GITHUB_TOKEN="t", PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-I", os.path.join(SRC, "ghsync.py"), "-C", d, "sync", "--yes"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("inside the listed project", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class TestRegistrySchema(GuardBase):
    def write_registry(self, data):
        with open(gp.registry_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def entry(self, i, root):
        return {"id": i, "root": root, "name": os.path.basename(root)}

    def test_unknown_version_is_refused(self):
        self.write_registry({"version": 2, "projects": []})
        with self.assertRaises(gp.RegistryError) as cm:
            gp.load_registry()
        self.assertIn("version", str(cm.exception))

    def test_duplicate_ids_are_refused(self):
        self.write_registry({"version": 1, "projects": [self.entry("aaaa", self.root),
                                                        self.entry("aaaa", self.b)]})
        with self.assertRaises(gp.RegistryError):
            gp.load_registry()

    def test_hand_edited_duplicate_or_nested_roots_are_refused(self):
        inner = os.path.join(self.root, "sub")
        os.makedirs(inner)
        for second in (self.root + os.sep, inner):
            self.write_registry({"version": 1, "projects": [self.entry("a1", self.root),
                                                            self.entry("b2", second)]})
            with self.assertRaises(gp.RegistryError, msg=second):
                gp.load_registry()

    def test_relative_root_is_refused(self):
        self.write_registry({"version": 1, "projects": [self.entry("a1", "relative/path")]})
        with self.assertRaises(gp.RegistryError):
            gp.load_registry()


if __name__ == "__main__":
    unittest.main()
