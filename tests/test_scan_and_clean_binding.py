# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
0.5.1: GHS-017 (a failed secret inspection must never count as clean) and GHS-018
(a clean-up confirmation is bound to the GitHub content of every target, not just names).
"""
import argparse
import contextlib
import io
import os
import secrets
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_ghsync import Base, ghsync  # noqa: E402
from fake_github import blob_sha  # noqa: E402


def fresh_token():
    """A GitHub-token-shaped value unique to this call, so no earlier scan is cached for it."""
    return "gh" + "p_" + secrets.token_hex(18)          # 36 chars after the prefix


@contextlib.contextmanager
def flaky_read(rel, failures=1):
    """read_local fails `failures` times for `rel` (a file open in an editor, AV lock...), then works."""
    real, left = ghsync.read_local, [failures]

    def read_local(root, r):
        if r == rel and left[0] > 0:
            left[0] -= 1
            raise PermissionError(13, "The process cannot access the file")
        return real(root, r)
    ghsync.read_local = read_local
    try:
        yield
    finally:
        ghsync.read_local = real


# ================================================================ GHS-017
class TestGHS017UncheckedIsNeverClean(Base):
    def test_read_error_is_reported_as_unchecked_not_clean(self):
        tok = fresh_token()
        self.write("settings.py", f"TOKEN = '{tok}'")
        items = {"settings.py": blob_sha(f"TOKEN = '{tok}'".encode())}
        with flaky_read("settings.py"):
            first = ghsync.find_secrets(self.root, items)
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0].get("unchecked"))
        self.assertIn("could not be checked", first[0]["kind"])

    def test_transient_read_error_is_not_cached(self):
        # The reviewer's reproduction: after recovery, the next scan must see the token.
        tok = fresh_token()
        self.write("settings.py", f"TOKEN = '{tok}'")
        items = {"settings.py": blob_sha(f"TOKEN = '{tok}'".encode())}
        with flaky_read("settings.py"):
            ghsync.find_secrets(self.root, items)
        second = ghsync.find_secrets(self.root, items)
        self.assertEqual([f["kind"] for f in second], ["GitHub token"])
        self.assertEqual(second[0]["line"], 1)

    def test_content_that_does_not_match_the_plan_is_unchecked(self):
        self.write("notes.txt", "now")
        found = ghsync.find_secrets(self.root, {"notes.txt": blob_sha(b"what the plan saw")})
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].get("unchecked"))

    def test_read_error_during_scan_blocks_the_push(self):
        # Scan fails, upload would succeed: the secret must not slip through.
        self.synced({"a.txt": "a"})
        tok = fresh_token()
        self.write("settings.py", f"TOKEN = '{tok}'")
        head = self.gh.ref
        with flaky_read("settings.py"):
            out = self.assert_fails(lambda: ghsync.run_sync(self.ns(), "sync"))
        self.assertEqual(self.gh.ref, head)
        self.assertNotIn("settings.py", self.gh.files())
        self.assertIn("could not be checked", out)
        self.assertNotIn(tok, out)

    def test_no_approval_covers_an_unchecked_file(self):
        self.synced({"a.txt": "a"})
        tok = fresh_token()
        body = f"TOKEN = '{tok}'"
        self.write("settings.py", body)
        head = self.gh.ref
        for allow in (True, {("settings.py", blob_sha(body.encode()))}):
            with flaky_read("settings.py"):
                with self.assertRaises(ghsync.SecretsUnchecked):
                    with contextlib.redirect_stdout(io.StringIO()):
                        ghsync.run_sync(self.ns(allow_secrets=allow), "sync")
            self.assertEqual(self.gh.ref, head)
        self.assertNotIn("settings.py", self.gh.files())

    def test_retry_after_recovery_works_normally(self):
        self.synced({"a.txt": "a"})
        self.write("b.txt", "plain")
        with flaky_read("b.txt"):
            self.assert_fails(lambda: ghsync.run_sync(self.ns(), "sync"))
        self.sync()                                       # file readable again: pushes
        self.assertIn("b.txt", self.gh.files())

    def test_unchecked_is_still_a_secrets_refusal(self):
        # Callers that catch SecretsFound (GUI, smoke test) keep catching this.
        self.assertTrue(issubclass(ghsync.SecretsUnchecked, ghsync.SecretsFound))


# ================================================================ GHS-018
class TestGHS018CleanBoundToContent(Base):
    def cns(self, **kw):
        d = dict(dir=self.root, dry_run=False, yes=True, message=None)
        d.update(kw)
        return argparse.Namespace(**d)

    def setup_repo(self):
        self.synced({"a.txt": "a"})
        self.gh.commit_files({"ignored.db": b"version 1"}, keep=True)
        self.write(".ghignore", "*.db\n.ghignore\n")
        self.write("ignored.db", "local copy")

    def reviewed_digest(self):
        root, cfg = ghsync.load_cfg(self.ns())
        snap = ghsync.gather(root, cfg)
        return ghsync.clean_digest(cfg, snap["remote"]["files"], ghsync.clean_targets(snap))

    def test_target_replaced_on_github_after_review_is_refused(self):
        self.setup_repo()
        digest = self.reviewed_digest()
        self.gh.commit_files({"ignored.db": b"version 2 - newer, never reviewed"}, keep=True)
        head = self.gh.ref
        with self.assertRaises(ghsync.PlanChanged):
            with contextlib.redirect_stdout(io.StringIO()):
                ghsync.run_clean(self.cns(expect=digest))
        self.assertEqual(self.gh.ref, head)
        self.assertEqual(self.gh.files()["ignored.db"], b"version 2 - newer, never reviewed")

    def test_unchanged_targets_still_clean(self):
        self.setup_repo()
        digest = self.reviewed_digest()
        self.gh.commit_files({"unrelated.txt": b"someone else's edit"}, keep=True)
        with contextlib.redirect_stdout(io.StringIO()):
            res = ghsync.run_clean(self.cns(expect=digest))
        self.assertEqual(res["removed"], ["ignored.db"])
        self.assertNotIn("ignored.db", self.gh.files())
        self.assertEqual(self.read("ignored.db"), b"local copy")

    def test_digest_is_bound_to_destination(self):
        cfg = {"owner": "me", "repo": "proj", "branch": "main"}
        files, targets = {"x.db": "a" * 40}, ["x.db"]
        d = ghsync.clean_digest(cfg, files, targets)
        self.assertEqual(d, ghsync.clean_digest(dict(cfg, owner="ME", repo="Proj"), files, targets))
        self.assertNotEqual(d, ghsync.clean_digest(dict(cfg, branch="dev"), files, targets))
        self.assertNotEqual(d, ghsync.clean_digest(dict(cfg, repo="other"), files, targets))
        self.assertNotEqual(d, ghsync.clean_digest(cfg, {"x.db": "b" * 40}, targets))


if __name__ == "__main__":
    unittest.main()
