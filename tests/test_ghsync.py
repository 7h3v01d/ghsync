# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
Regression tests for the ghsync engine, run against an in-memory fake GitHub.

Run:   python -m unittest discover -s tests -v      (or: python -m pytest tests)

Set GHSYNC_SRC=<folder> to run the same suite against another copy of ghsync.py
(this is how the fixes were revert-proven against the 0.1 build).
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("GHSYNC_SRC") or os.path.dirname(HERE)
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)
import ghsync  # noqa: E402
from fake_github import FakeGitHub, blob_sha  # noqa: E402

CAN_SYMLINK = hasattr(os, "symlink") and sys.platform != "win32"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ghsync-test-")
        self.root = os.path.join(self.tmp, "proj")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.root)
        os.makedirs(self.outside)
        self._api = ghsync.api
        self.gh = FakeGitHub().install(ghsync)
        ghsync.write_json(os.path.join(self.root, ".ghsync", "config.json"),
                          {"owner": "me", "repo": "proj", "branch": "main"})

    def tearDown(self):
        ghsync.api = self._api
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def write(self, rel, data):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    def read(self, rel):
        with open(self.path(rel), "rb") as f:
            return f.read()

    def exists(self, rel):
        return os.path.lexists(self.path(rel))

    def ns(self, **kw):
        d = dict(dir=self.root, message=None, dry_run=False, yes=True, prefer=None,
                 resolve=None, only=None)
        d.update(kw)
        return argparse.Namespace(**d)

    def sync(self, mode="sync", **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            ghsync.run_sync(self.ns(**kw), mode)
        return buf.getvalue()

    def state(self):
        with open(os.path.join(self.root, ".ghsync", "state.json"), encoding="utf-8") as f:
            return json.load(f)

    def synced(self, files):
        """Local and remote both hold `files`, with a recorded base."""
        for p, d in files.items():
            self.write(p, d)
        self.sync()
        self.assertEqual({p: (d if isinstance(d, bytes) else d.encode()) for p, d in files.items()},
                         self.gh.files())

    def assert_fails(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            with self.assertRaises((SystemExit, Exception)) as cm:
                fn()
        return buf.getvalue() + "\n" + str(cm.exception)


# ======================================================================= baseline
class TestHappyPath(Base):
    def test_push_pull_delete_roundtrip(self):
        self.synced({"a.txt": "one", "dir/b.txt": "two", "gone.txt": "x"})
        self.write("a.txt", "one-local")
        os.remove(self.path("gone.txt"))
        self.gh.commit_files({"dir/b.txt": b"two-remote", "new/c.txt": b"three"}, keep=True)
        self.sync()
        self.assertEqual(self.gh.files(), {"a.txt": b"one-local", "dir/b.txt": b"two-remote",
                                           "new/c.txt": b"three"})
        self.assertEqual(self.read("dir/b.txt"), b"two-remote")
        self.assertEqual(self.read("new/c.txt"), b"three")
        self.assertFalse(self.exists("gone.txt"))
        self.assertEqual(set(self.state()["files"]), {"a.txt", "dir/b.txt", "new/c.txt"})

    def test_remote_delete_removes_local_and_empty_dirs(self):
        self.synced({"keep.txt": "k", "deep/er/x.txt": "x"})
        self.gh.commit_files({"deep/er/x.txt": None}, keep=True)
        self.sync()
        self.assertFalse(os.path.exists(self.path("deep")))
        self.assertTrue(self.exists("keep.txt"))

    def test_conflict_left_untouched_then_resolved(self):
        self.synced({"c.txt": "base"})
        self.write("c.txt", "mine")
        self.gh.commit_files({"c.txt": b"theirs"}, keep=True)
        out = self.sync()
        self.assertIn("CONFLICT", out)
        self.assertEqual(self.read("c.txt"), b"mine")
        self.assertEqual(self.gh.files()["c.txt"], b"theirs")
        self.sync(prefer="remote")
        self.assertEqual(self.read("c.txt"), b"theirs")

    def test_real_local_deletion_still_propagates(self):
        self.synced({"a.txt": "a", "b.txt": "b"})
        os.remove(self.path("b.txt"))
        self.sync()
        self.assertEqual(set(self.gh.files()), {"a.txt"})

    def test_dry_run_changes_nothing(self):
        self.synced({"a.txt": "a"})
        self.write("a.txt", "changed")
        before = self.gh.ref
        self.sync(dry_run=True)
        self.assertEqual(self.gh.ref, before)


# ============================================== GHS-001: protected/unsafe remote paths
class TestGHS001RemotePathPolicy(Base):
    def test_remote_cannot_overwrite_ghsync_config(self):
        self.synced({"normal.txt": "n"})
        evil = json.dumps({"owner": "attacker", "repo": "x", "branch": "main"}).encode()
        self.gh.commit_files({".ghsync/config.json": evil, ".ghsync/state.json": b"{}"}, keep=True)
        self.sync()
        cfg = json.load(open(os.path.join(self.root, ".ghsync", "config.json"), encoding="utf-8"))
        self.assertEqual(cfg["owner"], "me")

    def test_remote_ignored_files_are_not_pulled(self):
        self.synced({"normal.txt": "n"})
        self.gh.commit_files({".env": b"SECRET=1", ".git/hooks/pre-commit": b"#!/bin/sh\nrm -rf ~"},
                             keep=True)
        self.sync()
        self.assertFalse(self.exists(".env"))
        self.assertFalse(self.exists(".git/hooks/pre-commit"))

    def test_remote_traversal_paths_are_rejected(self):
        self.synced({"normal.txt": "n"})
        self.gh.commit_files({"../escape.txt": b"x", "a/../../escape2.txt": b"y"}, keep=True)
        self.sync()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape2.txt")))

    def test_remote_windows_unsafe_names_are_rejected(self):
        self.synced({"normal.txt": "n"})
        self.gh.commit_files({"CON.txt": b"x", "a:b.txt": b"x", "trail.": b"x", "back\\slash": b"x"},
                             keep=True)
        self.sync()
        for name in ("CON.txt", "a:b.txt", "trail.", "back\\slash"):
            self.assertFalse(os.path.lexists(os.path.join(self.root, name)), name)

    def test_remote_symlink_is_not_materialised_as_a_file(self):
        self.synced({"normal.txt": "n"})
        self.gh.commit_files({"link": ("120000", b"/etc/passwd")}, keep=True)
        self.sync()
        self.assertFalse(self.exists("link"))

    def test_conflict_resolution_cannot_target_reserved_paths(self):
        self.synced({"normal.txt": "n"})
        self.gh.commit_files({".ghsync/config.json": b'{"owner":"attacker","repo":"x","branch":"main"}'},
                             keep=True)
        self.sync(resolve={".ghsync/config.json": "remote"}, only={".ghsync/config.json"})
        cfg = json.load(open(os.path.join(self.root, ".ghsync", "config.json"), encoding="utf-8"))
        self.assertEqual(cfg["owner"], "me")

    def test_unsafe_remote_paths_never_deleted_remotely_either(self):
        self.synced({"normal.txt": "n"})
        self.gh.commit_files({"CON.txt": b"x"}, keep=True)
        self.sync()
        self.sync()
        self.assertIn("CON.txt", self.gh.files())


# ============================================ GHS-002: writes must stay inside the root
@unittest.skipUnless(CAN_SYMLINK, "needs POSIX symlinks")
class TestGHS002Containment(Base):
    def test_pull_through_symlinked_dir_does_not_escape(self):
        self.synced({"normal.txt": "n"})
        os.symlink(self.outside, self.path("linked"))
        self.gh.commit_files({"linked/outside.txt": b"pwned"}, keep=True)
        self.sync()
        self.assertFalse(os.path.exists(os.path.join(self.outside, "outside.txt")))

    def test_pull_onto_symlinked_file_does_not_write_through_it(self):
        target = os.path.join(self.outside, "victim.txt")
        with open(target, "wb") as f:
            f.write(b"original")
        self.synced({"normal.txt": "n"})
        os.symlink(target, self.path("f.txt"))
        self.gh.commit_files({"f.txt": b"pwned"}, keep=True)
        self.sync()
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"original")

    def test_nested_symlink_parent_is_refused(self):
        self.synced({"normal.txt": "n"})
        os.makedirs(self.path("real"))
        os.symlink(self.outside, self.path("real/linked"))
        self.gh.commit_files({"real/linked/deep/x.txt": b"pwned"}, keep=True)
        self.sync()
        self.assertFalse(os.path.exists(os.path.join(self.outside, "deep")))


class TestGHS002Units(Base):
    def test_reparse_points_are_treated_as_links(self):
        junction = mock.Mock(st_mode=0o040755, st_file_attributes=0x410, st_reparse_tag=0xA0000003)
        symlink = mock.Mock(st_mode=0o100644, st_file_attributes=0x420, st_reparse_tag=0xA000000C)
        unknown = mock.Mock(st_mode=0o040755, st_file_attributes=0x400)
        onedrive = mock.Mock(st_mode=0o100644, st_file_attributes=0x420, st_reparse_tag=0x9000601A)
        plain = mock.Mock(st_mode=0o040755, st_file_attributes=0x10)
        self.assertTrue(ghsync._is_link_like(junction))
        self.assertTrue(ghsync._is_link_like(symlink))
        self.assertTrue(ghsync._is_link_like(unknown))
        self.assertFalse(ghsync._is_link_like(onedrive))
        self.assertFalse(ghsync._is_link_like(plain))

    @unittest.skipUnless(CAN_SYMLINK, "needs POSIX symlinks")
    def test_delete_refuses_symlinked_parent(self):
        victim = os.path.join(self.outside, "x.txt")
        with open(victim, "wb") as f:
            f.write(b"keep me")
        os.symlink(self.outside, self.path("linked"))
        with self.assertRaises(ghsync.UnsafePath):
            ghsync.delete_local(self.root, "linked/x.txt", expect=blob_sha(b"keep me"))
        self.assertTrue(os.path.exists(victim))


# ======================================= GHS-003: excluded files are not deletions
class TestGHS003ExcludedIsNotDeleted(Base):
    def test_newly_ignored_file_is_not_deleted_from_github(self):
        self.synced({"a.txt": "a", "secret.txt": "s"})
        self.write(".ghignore", "secret.txt\n.ghignore\n")
        self.sync()
        self.assertIn("secret.txt", self.gh.files())
        self.assertTrue(self.exists("secret.txt"))

    def test_oversized_file_is_not_deleted_from_github(self):
        self.synced({"a.txt": "a", "big.bin": "small"})
        with mock.patch.object(ghsync, "MAX_FILE", 10):
            self.write("big.bin", "x" * 50)
            self.sync()
        self.assertEqual(self.gh.files().get("big.bin"), b"small")

    def test_unreadable_file_is_not_deleted_from_github(self):
        self.synced({"a.txt": "a", "locked.txt": "l"})
        locked = os.path.abspath(self.path("locked.txt"))
        real_stat, real_lstat, real_open = os.stat, os.lstat, os.open
        import builtins
        real_bopen = builtins.open

        def deny(fn):
            def wrapped(p, *a, **k):
                try:
                    if os.path.abspath(os.fsdecode(p)) == locked:
                        raise PermissionError(13, "Permission denied", p)
                except TypeError:
                    pass
                return fn(p, *a, **k)
            return wrapped
        with mock.patch("os.stat", deny(real_stat)), mock.patch("os.lstat", deny(real_lstat)), \
                mock.patch("os.open", deny(real_open)), mock.patch("builtins.open", deny(real_bopen)):
            self.sync()
        self.assertIn("locked.txt", self.gh.files())

    @unittest.skipUnless(CAN_SYMLINK, "needs POSIX symlinks")
    def test_file_replaced_by_symlink_is_not_deleted(self):
        self.synced({"a.txt": "a", "f.txt": "f"})
        os.remove(self.path("f.txt"))
        os.symlink(os.path.join(self.outside, "whatever"), self.path("f.txt"))
        self.sync()
        self.assertIn("f.txt", self.gh.files())

    def test_excluded_files_keep_their_base(self):
        self.synced({"a.txt": "a", "secret.txt": "s"})
        self.write(".ghignore", "secret.txt\n.ghignore\n")
        self.sync()
        self.assertIn("secret.txt", self.state()["files"])


# ==================================== GHS-004: local edits during a pull are protected
class TestGHS004PullPreconditions(Base):
    def _edit_after_scan(self, edits):
        real = ghsync.fetch_remote

        def wrapped(cfg):
            res = real(cfg)
            for rel, data in edits.items():
                if data is None:
                    os.remove(self.path(rel))
                else:
                    self.write(rel, data)
            return res
        return mock.patch.object(ghsync, "fetch_remote", wrapped)

    def test_edit_during_download_is_not_overwritten(self):
        self.synced({"settings.py": "v1"})
        self.gh.commit_files({"settings.py": b"v2-remote"}, keep=True)
        fired = []

        def hook(_sha):
            if not fired:
                fired.append(1)
                self.write("settings.py", "my unsaved work")
        self.gh.on_blob_download = hook
        self.sync()
        self.assertEqual(self.read("settings.py"), b"my unsaved work")
        self.assertEqual(self.gh.files()["settings.py"], b"v2-remote")

    def test_edit_after_scan_is_not_overwritten(self):
        self.synced({"settings.py": "v1"})
        self.gh.commit_files({"settings.py": b"v2-remote"}, keep=True)
        with self._edit_after_scan({"settings.py": "my edit"}):
            self.sync()
        self.assertEqual(self.read("settings.py"), b"my edit")

    def test_edit_after_scan_is_not_deleted(self):
        self.synced({"a.txt": "a", "notes.txt": "n"})
        self.gh.commit_files({"notes.txt": None}, keep=True)
        with self._edit_after_scan({"notes.txt": "important"}):
            self.sync()
        self.assertEqual(self.read("notes.txt"), b"important")

    def test_new_local_file_is_not_clobbered_by_pull(self):
        self.synced({"a.txt": "a"})
        self.gh.commit_files({"new.txt": b"remote"}, keep=True)
        with self._edit_after_scan({"new.txt": "local"}):
            self.sync()
        self.assertEqual(self.read("new.txt"), b"local")

    def test_skipped_file_is_reclassified_next_run(self):
        self.synced({"settings.py": "v1"})
        self.gh.commit_files({"settings.py": b"v2-remote"}, keep=True)
        with self._edit_after_scan({"settings.py": "my edit"}):
            self.sync()
        self.assertEqual(self.state()["files"]["settings.py"], blob_sha(b"v1"))
        out = self.sync()
        self.assertIn("CONFLICT", out)


# ======================================== GHS-005: one mutating operation per project
HOLDER = r"""
import os, sys, time
p = sys.argv[1]
os.makedirs(os.path.dirname(p), exist_ok=True)
fd = os.open(p, os.O_RDWR | os.O_CREAT)
if os.name == "nt":
    import msvcrt; msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
else:
    import fcntl; fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
sys.stdin.read()
"""


class TestGHS005Lock(Base):
    def _hold(self):
        lock = os.path.join(self.root, ".ghsync", "lock")
        proc = subprocess.Popen([sys.executable, "-I", "-c", HOLDER, lock],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.stdout.readline().strip(), "locked")
        return proc

    def test_second_process_cannot_sync_while_locked(self):
        self.synced({"a.txt": "a"})
        self.write("a.txt", "changed")
        head = self.gh.ref
        proc = self._hold()
        try:
            out = self.assert_fails(lambda: ghsync.run_sync(self.ns(), "sync"))
            self.assertEqual(self.gh.ref, head)
            self.assertIn("another ghsync", out.lower())
        finally:
            proc.stdin.close()
            proc.wait(10)

    def test_lock_is_released_when_holder_dies(self):
        self.synced({"a.txt": "a"})
        self.write("a.txt", "changed")
        proc = self._hold()
        proc.kill()
        proc.wait(10)
        self.sync()
        self.assertEqual(self.gh.files()["a.txt"], b"changed")

    def test_lock_is_reentrant_within_one_operation(self):
        with ghsync.ProjectLock(self.root):
            with ghsync.ProjectLock(self.root):
                pass
        with ghsync.ProjectLock(self.root):
            pass


# ======================================= GHS-006: atomic state with crash recovery
class TestGHS006AtomicState(Base):
    def test_crash_during_state_write_keeps_previous_state(self):
        self.synced({"a.txt": "a"})
        state_path = os.path.join(self.root, ".ghsync", "state.json")
        before = self.state()
        real_dump = json.dump

        def crash(obj, f, **kw):
            f.write('{"commit": "abc", "fi')
            f.flush()
            raise OSError("disk went away")
        with mock.patch("json.dump", crash):
            with self.assertRaises(OSError):
                ghsync.write_json(state_path, {"commit": "x", "files": {}})
        json.dump = real_dump
        self.assertEqual(self.state(), before)
        leftovers = [n for n in os.listdir(os.path.dirname(state_path)) if n.startswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_corrupt_state_is_not_silently_reset(self):
        self.synced({"a.txt": "a", "b.txt": "b"})
        with open(os.path.join(self.root, ".ghsync", "state.json"), "w") as f:
            f.write('{"commit": "abc", "fi')
        os.remove(self.path("b.txt"))
        head = self.gh.ref
        self.assert_fails(lambda: ghsync.run_sync(self.ns(), "sync"))
        self.assertEqual(self.gh.ref, head)

    def test_corrupt_cache_just_rehashes(self):
        self.synced({"a.txt": "a"})
        with open(os.path.join(self.root, ".ghsync", "cache.json"), "w") as f:
            f.write("garbage")
        self.write("a.txt", "changed")
        self.sync()
        self.assertEqual(self.gh.files()["a.txt"], b"changed")

    def test_interrupted_pull_recovers_on_next_run(self):
        self.synced({"a.txt": "a"})
        self.gh.commit_files({"x/1.txt": b"1", "x/2.txt": b"2", "x/3.txt": b"3"}, keep=True)
        bad = blob_sha(b"2")

        def hook(sha):
            if sha == bad:
                raise ConnectionError("network dropped")
        self.gh.on_blob_download = hook
        try:
            self.sync()
        except BaseException:   # noqa: BLE001 - 0.1 aborts the whole pull here
            pass
        self.gh.on_blob_download = None
        self.sync()
        self.assertEqual(self.read("x/2.txt"), b"2")
        self.assertEqual(set(self.state()["files"]), {"a.txt", "x/1.txt", "x/2.txt", "x/3.txt"})
        tmp = [f for d, _, fs in os.walk(self.root) for f in fs if f.endswith(".ghsync-tmp")]
        self.assertEqual(tmp, [])

    def test_leftover_temp_files_are_cleaned(self):
        self.synced({"a.txt": "a"})
        self.write("sub/.a.txt.1234.ghsync-tmp", "half")
        self.write("old.txt.ghsync-tmp", "half")
        self.sync()
        tmp = [f for d, _, fs in os.walk(self.root) for f in fs if f.endswith(".ghsync-tmp")]
        self.assertEqual(tmp, [])


# ================================ GHS-007: approval is bound to the executed plan
class TestGHS007PlanBinding(Base):
    def _digest(self, mode="sync"):
        root, cfg = ghsync.load_cfg(self.ns())
        snap = ghsync.gather(root, cfg)
        push, pull, conflict, _ = ghsync.plan_actions(snap, mode)
        return ghsync.plan_digest(mode, push, pull)

    def test_new_deletion_after_confirmation_aborts(self):
        self.synced({"a.txt": "a", "b.txt": "b"})
        self.write("a.txt", "changed")
        digest = self._digest()           # user confirmed: push a.txt, no deletions
        os.remove(self.path("b.txt"))     # ...then the folder changed
        self.assert_fails(lambda: ghsync.run_sync(self.ns(expect=digest), "sync"))
        self.assertIn("b.txt", self.gh.files())
        self.assertEqual(self.gh.files()["a.txt"], b"a")

    def test_remote_change_after_confirmation_aborts(self):
        self.synced({"a.txt": "a", "b.txt": "b"})
        digest = self._digest()
        self.gh.commit_files({"b.txt": None}, keep=True)
        self.assert_fails(lambda: ghsync.run_sync(self.ns(expect=digest), "sync"))
        self.assertTrue(self.exists("b.txt"))

    def test_matching_digest_executes(self):
        self.synced({"a.txt": "a", "b.txt": "b"})
        os.remove(self.path("b.txt"))
        digest = self._digest()
        self.sync(expect=digest)
        self.assertNotIn("b.txt", self.gh.files())


# ================================== extra findings from verification (not in review)
class TestWindowsLinks(Base):
    def _fake_junction(self, rel):
        target = os.path.abspath(self.path(rel))
        real = os.lstat
        import types

        def lstat(p, *a, **k):
            st = real(p, *a, **k)
            if os.path.abspath(os.fsdecode(p)) == target:
                d = {n: getattr(st, n) for n in dir(st) if n.startswith("st_")}
                d.update(st_file_attributes=0x410, st_reparse_tag=0xA0000003)
                return types.SimpleNamespace(**d)
            return st
        return mock.patch("os.lstat", lstat)

    def test_junction_is_not_scanned_or_pushed(self):
        # On Windows before Python 3.12, os.walk walks into junctions: 0.1 pushed whatever
        # the junction pointed at. Simulated here by making a real folder report as one.
        self.synced({"a.txt": "a"})
        self.write("jdir/inner.txt", "from outside the project")
        with self._fake_junction("jdir"):
            out = self.sync()
        self.assertNotIn("jdir/inner.txt", self.gh.files())
        self.assertIn("symlink or junction", out)

    def test_pull_refuses_to_write_through_junction(self):
        self.synced({"a.txt": "a"})
        os.makedirs(self.path("jdir"))
        self.gh.commit_files({"jdir/x.txt": b"pwned"}, keep=True)
        with self._fake_junction("jdir"):
            self.sync()
        self.assertFalse(self.exists("jdir/x.txt"))

    def test_case_collisions_on_github_are_held_on_windows(self):
        self.synced({"a.txt": "a"})
        self.gh.commit_files({"Readme.md": b"1", "README.md": b"2"}, keep=True)
        with mock.patch.object(ghsync, "CASE_INSENSITIVE_FS", True):
            self.sync()
        self.assertFalse(self.exists("Readme.md"))
        self.assertFalse(self.exists("README.md"))

    def test_status_reports_unsafe_remote_entries(self):
        self.synced({"a.txt": "a"})
        self.gh.commit_files({".ghsync/config.json": b"{}", "link": ("120000", b"/etc")}, keep=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ghsync.cmd_status(argparse.Namespace(dir=self.root))
        out = buf.getvalue()
        self.assertIn("HELD", out)
        self.assertIn(".ghsync/config.json", out)
        self.assertIn("symlink on GitHub", out)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_pull_keeps_executable_bit(self):
        self.synced({"run.sh": "echo 1"})
        os.chmod(self.path("run.sh"), 0o755)
        self.gh.commit_files({"run.sh": b"echo 2"}, keep=True)
        self.sync()
        self.assertEqual(self.read("run.sh"), b"echo 2")
        self.assertTrue(os.stat(self.path("run.sh")).st_mode & 0o100)


class TestCliOverHttp(Base):
    """Drives the real CLI and the real api() over HTTP against the fake."""

    def setUp(self):
        super().setUp()
        ghsync.api = self._api
        self.gh._mod = None
        from fake_github import serve_http
        self.srv, self.url = serve_http(self.gh)

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def cli(self, *argv):
        env = dict(os.environ, GHSYNC_API=self.url, GITHUB_TOKEN="test-token", PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-I", os.path.join(SRC, "ghsync.py"), "-C", self.root, *argv],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_cli_status_sync_and_refusals(self):
        self.write("a.txt", "hello")
        self.write("sub/b.txt", "world")
        r = self.cli("sync", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.gh.files(), {"a.txt": b"hello", "sub/b.txt": b"world"})
        self.gh.commit_files({"sub/b.txt": b"remote", ".ghsync/config.json": b"{}"}, keep=True)
        r = self.cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PULL", r.stdout)
        self.assertIn("HELD", r.stdout)
        r = self.cli("pull", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("sub/b.txt"), b"remote")
        r = self.cli("diff", "a.txt")
        self.assertEqual(r.returncode, 0, r.stderr)


# ====================================================== misc robustness
class TestMisc(Base):
    def test_ambiguous_ref_update_is_treated_as_success(self):
        self.synced({"a.txt": "a"})
        self.write("a.txt", "changed")
        real = self.gh.api

        def flaky(method, path, body=None, raw=False):
            if method == "PATCH":
                real(method, path, body, raw)     # applied on GitHub...
                raise ghsync.ApiError(422, "Update is not a fast forward")   # ...but we saw an error
            return real(method, path, body, raw)
        ghsync.api = flaky
        self.sync()
        ghsync.api = real
        self.assertEqual(self.gh.files()["a.txt"], b"changed")
        self.assertEqual(self.state()["commit"], self.gh.ref)

    def test_config_with_bad_owner_is_rejected(self):
        ghsync.write_json(os.path.join(self.root, ".ghsync", "config.json"),
                          {"owner": "../../x", "repo": "proj", "branch": "main"})
        self.assert_fails(lambda: ghsync.run_sync(self.ns(), "sync"))

    def test_path_policy(self):
        ok = ["a.txt", "dir/b.txt", ".github/workflows/ci.yml", "a b/c d.txt", "unicodé.txt"]
        bad = ["", "/abs", "a//b", "a/./b", "../x", "a/..", ".ghsync/config.json", ".GHSYNC/x",
               ".git/config", "CON", "con.txt", "LPT1.log", "a:b", "x.", "x ", "a\\b", "a\0b",
               "a<b", "tab\tname", "x.ghsync-tmp"]
        for p in ok:
            self.assertIsNone(ghsync.check_repo_path(p), p)
        for p in bad:
            self.assertIsNotNone(ghsync.check_repo_path(p), p)


if __name__ == "__main__":
    unittest.main()
