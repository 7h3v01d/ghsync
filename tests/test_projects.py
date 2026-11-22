# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
Project registry (0.4.0): persistent, junction-aware project identity, overlap and
duplicate rejection, remote-destination clash detection, cheap per-project status.
Qt-free; the GUI's use of it is covered in test_gui_projects.py.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("GHSYNC_SRC") or os.path.dirname(HERE)
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)
import ghsync  # noqa: E402
import ghsync_projects as gp  # noqa: E402
from multi_fake import MultiFake  # noqa: E402

CAN_SYMLINK = hasattr(os, "symlink") and sys.platform != "win32"


class RegBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ghsync-reg-")
        self.home = os.path.join(self.tmp, "home")
        self._env = os.environ.get("GHSYNC_HOME")
        os.environ["GHSYNC_HOME"] = self.home
        self._api = ghsync.api
        self._tok = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "t"

    def tearDown(self):
        ghsync.api = self._api
        for k, v in (("GHSYNC_HOME", self._env), ("GITHUB_TOKEN", self._tok)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name, link=None, branch="main"):
        d = os.path.join(self.tmp, *name.split("/"))
        os.makedirs(d, exist_ok=True)
        if link:
            ghsync.write_json(os.path.join(d, ".ghsync", "config.json"),
                              {"owner": "me", "repo": link, "branch": branch})
        return d


class TestRegistry(RegBase):
    def test_projects_survive_a_restart(self):
        a, b, c = self.folder("a", "ra"), self.folder("b", "rb"), self.folder("c")
        ids = [gp.add_project(p)["id"] for p in (a, b, c)]
        reloaded = gp.load_registry()
        self.assertEqual([e["id"] for e in reloaded], ids)
        self.assertEqual([e["name"] for e in reloaded], ["a", "b", "c"])
        self.assertTrue(os.path.exists(gp.registry_path()))

    def test_duplicate_folder_rejected_in_any_spelling(self):
        a = self.folder("a")
        gp.add_project(a)
        for alias in (a, a + os.sep, os.path.join(a, "."), os.path.join(a, "..", "a")):
            with self.assertRaises(gp.RegistryError, msg=alias):
                gp.add_project(alias)
        self.assertEqual(len(gp.load_registry()), 1)

    @unittest.skipUnless(sys.platform == "win32", "case-insensitive paths")
    def test_duplicate_with_different_case_rejected(self):
        a = self.folder("Proj")
        gp.add_project(a)
        with self.assertRaises(gp.RegistryError):
            gp.add_project(a.upper())

    def test_parent_and_child_folders_rejected(self):
        parent = self.folder("work")
        child = self.folder("work/sub")
        gp.add_project(parent)
        with self.assertRaises(gp.RegistryError) as cm:
            gp.add_project(child)
        self.assertIn("inside", str(cm.exception))
        gp.remove_project(gp.load_registry()[0]["id"])
        gp.add_project(child)
        with self.assertRaises(gp.RegistryError) as cm:
            gp.add_project(parent)
        self.assertIn("contains", str(cm.exception))

    def test_sibling_with_common_prefix_is_not_an_overlap(self):
        gp.add_project(self.folder("app"))
        gp.add_project(self.folder("app2"))           # "app2" starts with "app" but isn't inside it
        self.assertEqual(len(gp.load_registry()), 2)

    @unittest.skipUnless(CAN_SYMLINK, "POSIX symlinks")
    def test_symlinked_alias_is_the_same_project(self):
        real = self.folder("real")
        alias = os.path.join(self.tmp, "alias")
        os.symlink(real, alias)
        gp.add_project(real)
        with self.assertRaises(gp.RegistryError):
            gp.add_project(alias)

    @unittest.skipUnless(sys.platform == "win32", "NTFS junctions")
    def test_junction_alias_is_the_same_project(self):
        real = self.folder("real")
        alias = os.path.join(self.tmp, "alias")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", alias, real], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        try:
            gp.add_project(real)
            with self.assertRaises(gp.RegistryError):
                gp.add_project(alias)
            gp.remove_project(gp.load_registry()[0]["id"])
            gp.add_project(alias)
            with self.assertRaises(gp.RegistryError):
                gp.add_project(os.path.join(real, "..", "real"))
        finally:
            os.rmdir(alias)

    def test_missing_folder_or_file_rejected(self):
        with self.assertRaises(gp.RegistryError):
            gp.add_project(os.path.join(self.tmp, "nope"))
        f = os.path.join(self.tmp, "file.txt")
        open(f, "w").close()
        with self.assertRaises(gp.RegistryError):
            gp.add_project(f)

    def test_remove_never_touches_files(self):
        a = self.folder("a", "ra")
        with open(os.path.join(a, "keep.txt"), "w") as f:
            f.write("k")
        e = gp.add_project(a)
        gp.remove_project(e["id"])
        self.assertEqual(gp.load_registry(), [])
        self.assertTrue(os.path.exists(os.path.join(a, "keep.txt")))
        self.assertTrue(os.path.exists(os.path.join(a, ".ghsync", "config.json")))

    def test_damaged_registry_fails_closed(self):
        os.makedirs(self.home)
        with open(gp.registry_path(), "w") as f:
            f.write('{"version": 1, "projects": [{"id": "x", "ro')
        with self.assertRaises(gp.RegistryError):
            gp.load_registry()
        with self.assertRaises(gp.RegistryError):
            gp.add_project(self.folder("a"))
        with open(gp.registry_path()) as f:
            self.assertIn('"ro', f.read())            # not overwritten

    def test_registry_is_written_atomically_and_versioned(self):
        gp.add_project(self.folder("a"))
        with open(gp.registry_path(), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["projects"]), 1)


class TestDestinations(RegBase):
    def test_same_repo_and_branch_rejected_on_add(self):
        gp.add_project(self.folder("a", "shared"))
        with self.assertRaises(gp.RegistryError) as cm:
            gp.add_project(self.folder("b", "shared"))
        self.assertIn("me/shared", str(cm.exception))

    def test_destination_match_ignores_owner_and_repo_case(self):
        gp.add_project(self.folder("a", "Shared"))
        d = self.folder("b")
        ghsync.write_json(os.path.join(d, ".ghsync", "config.json"),
                          {"owner": "ME", "repo": "shared", "branch": "main"})
        with self.assertRaises(gp.RegistryError):
            gp.add_project(d)

    def test_same_repo_different_branch_is_allowed(self):
        gp.add_project(self.folder("a", "shared", "main"))
        gp.add_project(self.folder("b", "shared", "dev"))
        self.assertEqual(gp.clashes(gp.load_registry()), {})

    def test_clash_created_later_is_detected(self):
        a = gp.add_project(self.folder("a", "ra"))
        b = gp.add_project(self.folder("b", "rb"))
        ghsync.write_json(os.path.join(b["root"], ".ghsync", "config.json"),
                          {"owner": "me", "repo": "ra", "branch": "main"})   # re-linked with the CLI
        c = gp.clashes(gp.load_registry())
        self.assertEqual(c, {a["id"]: [b["id"]], b["id"]: [a["id"]]})
        self.assertIsNotNone(gp.clash_message(b["root"]))

    def test_check_destination_before_linking(self):
        gp.add_project(self.folder("a", "ra"))
        b = gp.add_project(self.folder("b"))
        with self.assertRaises(gp.RegistryError):
            gp.check_destination(b["root"], "me", "ra", "main")
        gp.check_destination(b["root"], "me", "rb", "main")


class TestStatus(RegBase):
    def setUp(self):
        super().setUp()
        self.gh = MultiFake().install(ghsync)

    def synced_project(self, name, files):
        d = self.folder(name, name)
        cfgd = {"owner": "me", "repo": name, "branch": "main", "protect_local": False}
        ghsync.write_json(os.path.join(d, ".ghsync", "config.json"), cfgd)
        self.gh.repo(name)
        self.gh.install(ghsync)
        for p, data in files.items():
            full = os.path.join(d, *p.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(data)
        import argparse
        with contextlib.redirect_stdout(io.StringIO()):
            ghsync.run_sync(argparse.Namespace(dir=d, message=None, dry_run=False, yes=True,
                                               prefer=None, resolve=None, only=None), "sync")
        return d

    def test_states(self):
        clean = self.synced_project("clean", {"a.txt": "a"})
        local = self.synced_project("local", {"a.txt": "a"})
        with open(os.path.join(local, "a.txt"), "w") as f:
            f.write("changed")
        remote = self.synced_project("remote", {"a.txt": "a"})
        self.gh.repo("remote").commit_files({"b.txt": b"b"}, keep=True)
        both = self.synced_project("both", {"a.txt": "a"})
        with open(os.path.join(both, "new.txt"), "w") as f:
            f.write("n")
        self.gh.repo("both").commit_files({"b.txt": b"b"}, keep=True)
        unlinked = self.folder("unlinked")
        got = {n: gp.project_status(p)["state"] for n, p in
               (("clean", clean), ("local", local), ("remote", remote), ("both", both), ("unlinked", unlinked))}
        self.assertEqual(got, {"clean": "clean", "local": "local", "remote": "remote",
                               "both": "both", "unlinked": "unlinked"})
        st = gp.project_status(local)
        self.assertEqual(st["local_changes"], 1)
        self.assertEqual(st["repo"], "me/local · main")
        self.assertEqual(st["files"], 1)

    def test_ignored_paths_are_not_local_changes(self):
        d = self.synced_project("p", {"a.txt": "a", "secret.txt": "s"})
        with open(os.path.join(d, ".ghignore"), "w") as f:
            f.write("secret.txt\n.ghignore\n")
        self.assertEqual(gp.project_status(d)["state"], "clean")

    def test_errors_are_reported_not_raised_and_nothing_is_printed(self):
        d = self.synced_project("p", {"a.txt": "a"})
        with open(os.path.join(d, ".ghsync", "state.json"), "w") as f:
            f.write("{broken")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            st = gp.project_status(d)
            st2 = gp.project_status(self.folder("gone-repo", "missing-on-github"))
        self.assertEqual(st["state"], "error")
        self.assertIn("damaged", st["error"])
        self.assertEqual(st2["state"], "error")
        self.assertEqual(buf.getvalue(), "")

    def test_status_is_one_api_call(self):
        d = self.synced_project("p", {"a.txt": "a"})
        n = len(self.gh.calls)
        gp.project_status(d)
        self.assertEqual(len(self.gh.calls) - n, 1)


if __name__ == "__main__":
    unittest.main()
