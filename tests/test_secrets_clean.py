# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
0.5.0: secrets check before every push, and clean-up of ignored files still on GitHub.

Fake credentials are assembled at runtime so this file never contains anything that
looks like a real token (to GitHub's push protection, or to ghsync's own check).
"""
import argparse
import contextlib
import io
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_ghsync import Base, SRC, ghsync  # noqa: E402
from fake_github import blob_sha  # noqa: E402

GH_TOKEN = "gh" + "p_" + "aB3" * 12                      # GitHub classic PAT shape
OPENAI_KEY = "s" + "k-proj-" + "Ab12Cd34" * 5            # OpenAI project key shape
AWS_KEY = "AK" + "IA" + "QWERTYUI23456789"               # AWS access key id shape
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\n-----END " + "RSA PRIVATE KEY-----\n"


def items(root, *rels):
    out = {}
    for r in rels:
        with open(os.path.join(root, *r.split("/")), "rb") as f:
            out[r] = blob_sha(f.read())
    return out


# ============================================================ the scanner
class TestSecretScan(Base):
    def scan(self, files):
        for p, d in files.items():
            self.write(p, d)
        return ghsync.find_secrets(self.root, items(self.root, *files))

    def test_tokens_are_found_with_line_and_masked_preview(self):
        f = self.scan({"config.py": f"DEBUG = True\nTOKEN = '{GH_TOKEN}'\n",
                       "ai.py": f"key = '{OPENAI_KEY}'", "aws.cfg": f"id={AWS_KEY}",
                       "deploy/server.txt": PEM})
        kinds = {x["path"]: x["kind"] for x in f}
        self.assertEqual(kinds, {"config.py": "GitHub token", "ai.py": "OpenAI/Anthropic API key",
                                 "aws.cfg": "AWS access key", "deploy/server.txt": "private key"})
        gh = next(x for x in f if x["path"] == "config.py")
        self.assertEqual(gh["line"], 2)
        self.assertNotIn(GH_TOKEN, gh["preview"])
        self.assertTrue(gh["preview"].startswith("ghp_"))

    def test_secret_looking_file_names(self):
        names = ["server.pem", "deploy.key", "id_rsa", "id_ed25519", ".env", ".env.local",
                 "key.txt", "credentials.json", ".pypirc", "cert.pfx"]
        f = self.scan({n: "nothing secret inside" for n in names})
        self.assertEqual({x["path"] for x in f}, set(names))
        self.assertTrue(all(x["kind"] == "file name looks like a secret" for x in f))

    def test_harmless_look_alikes_are_not_flagged(self):
        f = self.scan({".env.example": "TOKEN=put-yours-here", ".env.template": "X=",
                       "id_rsa.pub": "ssh-rsa AAAA user@host",
                       "style.css": ".sk-loading-spinner-container-wide-version { }",
                       "notes.md": "set GITHUB_TOKEN=ghp_xxx in your shell",
                       "keys.md": "Use the deploy.key file from the vault",
                       "readme.txt": "-----BEGIN PUBLIC KEY-----",
                       "secret.txt": "notes about the surprise party"})
        self.assertEqual(f, [])

    def test_binary_and_huge_files_skip_the_content_scan(self):
        self.write("blob.bin", "\x00\x01" + GH_TOKEN)
        with mock_scan_limit(10):
            self.write("big.txt", "x" * 50 + GH_TOKEN)
            f = ghsync.find_secrets(self.root, items(self.root, "blob.bin", "big.txt"))
        self.assertEqual(f, [])

    def test_ghsync_own_source_is_clean(self):
        tree = {}
        for d, _, fs in os.walk(SRC):
            if any(part in (".ghsync", "__pycache__", ".git") for part in d.split(os.sep)):
                continue
            for name in fs:
                full = os.path.join(d, name)
                with open(full, "rb") as fh:
                    tree[os.path.relpath(full, SRC).replace(os.sep, "/")] = blob_sha(fh.read())
        self.assertEqual(ghsync.find_secrets(SRC, tree), [])


@contextlib.contextmanager
def mock_scan_limit(n):
    old = ghsync.SECRET_SCAN_MAX
    ghsync.SECRET_SCAN_MAX = n
    try:
        yield
    finally:
        ghsync.SECRET_SCAN_MAX = old


# ======================================================= the push guard
class TestSecretGuard(Base):
    def test_push_with_a_secret_is_refused_and_nothing_uploads(self):
        self.synced({"a.txt": "a"})
        self.write("settings.py", f"TOKEN = '{GH_TOKEN}'")
        self.write("b.txt", "fine")
        head = self.gh.ref
        out = self.assert_fails(lambda: ghsync.run_sync(self.ns(), "sync"))
        self.assertEqual(self.gh.ref, head)
        self.assertIn("settings.py:1", out)
        self.assertIn("GitHub token", out)
        self.assertNotIn(GH_TOKEN, out)

    def test_allow_all_flag_pushes(self):
        self.synced({"a.txt": "a"})
        self.write("settings.py", f"TOKEN = '{GH_TOKEN}'")
        self.sync(allow_secrets=True)
        self.assertIn("settings.py", self.gh.files())

    def test_approval_covers_exactly_the_content_seen(self):
        self.synced({"a.txt": "a"})
        self.write("settings.py", f"TOKEN = '{GH_TOKEN}'")
        approved = {("settings.py", blob_sha(f"TOKEN = '{GH_TOKEN}'".encode()))}
        self.write("settings.py", f"TOKEN = '{GH_TOKEN}'\nOTHER = '{OPENAI_KEY}'")   # changed after review
        head = self.gh.ref
        self.assert_fails(lambda: ghsync.run_sync(self.ns(allow_secrets=approved), "sync"))
        self.assertEqual(self.gh.ref, head)
        self.write("settings.py", f"TOKEN = '{GH_TOKEN}'")
        self.sync(allow_secrets=approved)
        self.assertIn("settings.py", self.gh.files())

    def test_mirror_restore_is_checked_too(self):
        self.write("a.txt", "a")
        self.write("deploy.key", "k")
        self.sync(allow_secrets=True)                 # once knowingly allowed...
        self.gh.commit_files({"deploy.key": None}, keep=True)    # ...then deleted on GitHub
        ghsync.write_json(os.path.join(self.root, ".ghsync", "config.json"),
                          {"owner": "me", "repo": "proj", "branch": "main"})   # Protect local on
        head = self.gh.ref
        out = self.assert_fails(lambda: ghsync.run_sync(self.ns(), "sync"))   # restore = upload: checked
        self.assertIn("deploy.key", out)
        self.assertEqual(self.gh.ref, head)

    def test_deleting_a_secret_file_is_not_blocked(self):
        self.synced({"a.txt": "a"})
        self.gh.commit_files({"old.pem": b"x"}, keep=True)
        self.write("old.pem", "x")
        self.sync(allow_secrets=True)
        os.remove(self.path("old.pem"))
        self.sync()
        self.assertNotIn("old.pem", self.gh.files())

    def test_dry_run_and_status_warn_without_blocking(self):
        self.synced({"a.txt": "a"})
        self.write("id_rsa", "x")
        out = self.sync(dry_run=True)
        self.assertIn("POSSIBLE SECRETS", out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ghsync.cmd_status(argparse.Namespace(dir=self.root))
        self.assertIn("POSSIBLE SECRETS", buf.getvalue())
        self.assertIn("id_rsa", buf.getvalue())


class TestSecretCli(Base):
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

    def test_cli_push_refused_then_allowed_once(self):
        self.write("a.txt", "a")
        self.write("secrets.json", '{"k": 1}')
        r = self.cli("push", "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("secrets.json", r.stderr)
        self.assertIn("--allow-secrets", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.gh.files(), {})
        r = self.cli("push", "--yes", "--allow-secrets")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("secrets.json", self.gh.files())


# ======================================================= clean-up on GitHub
class TestClean(Base):
    def cns(self, **kw):
        d = dict(dir=self.root, dry_run=False, yes=True, message=None)
        d.update(kw)
        return argparse.Namespace(**d)

    def clean(self, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            res = ghsync.run_clean(self.cns(**kw))
        return res, buf.getvalue()

    def setup_repo(self):
        self.synced({"a.txt": "a", "src/app.py": "code"})
        self.gh.commit_files({"src/pkg.egg-info/PKG-INFO": b"m", ".pytest_cache/x": b"c",
                              "oce.sqlite3": b"db", "CON.txt": b"x", "link": ("120000", b"/x")},
                             keep=True)
        self.write(".ghignore", "*.sqlite3\n.ghignore\n")
        self.write("oce.sqlite3", "local database")

    def test_ignored_files_on_github_are_removed_and_local_copies_kept(self):
        self.setup_repo()
        res, out = self.clean()
        self.assertEqual(sorted(res["removed"]), [".pytest_cache/x", "oce.sqlite3", "src/pkg.egg-info/PKG-INFO"])
        files = self.gh.files()
        self.assertEqual(set(files) - {"CON.txt", "link"}, {"a.txt", "src/app.py"})
        self.assertIn("CON.txt", files)                         # unsafe name: not "ignored", left alone
        self.assertIn("link", self.gh.flat())                   # symlink: left alone
        self.assertEqual(self.read("oce.sqlite3"), b"local database")
        self.assertEqual(self.state()["commit"], self.gh.ref)
        self.assertNotIn("oce.sqlite3", self.state()["files"])

    def test_dry_run_only_lists(self):
        self.setup_repo()
        head = self.gh.ref
        res, out = self.clean(dry_run=True)
        self.assertEqual(self.gh.ref, head)
        self.assertIn("oce.sqlite3", out)

    def test_bound_to_the_confirmed_list(self):
        self.setup_repo()
        root, cfg = ghsync.load_cfg(self.ns())
        snap = ghsync.gather(root, cfg)
        digest = ghsync.clean_digest(cfg, snap["remote"]["files"], ghsync.clean_targets(snap))
        self.gh.commit_files({"more.sqlite3": b"appeared after review"}, keep=True)
        head = self.gh.ref
        self.assert_fails(lambda: ghsync.run_clean(self.cns(expect=digest)))
        self.assertEqual(self.gh.ref, head)

    def test_nothing_to_clean(self):
        self.synced({"a.txt": "a"})
        head = self.gh.ref
        res, out = self.clean()
        self.assertEqual(res["removed"], [])
        self.assertEqual(self.gh.ref, head)
        self.assertIn("Nothing to clean", out)

    def test_clean_obeys_the_project_guards(self):
        self.setup_repo()
        import ghsync_projects as gp
        gp.add_project(self.root)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        ghsync.write_json(os.path.join(other, ".ghsync", "config.json"),
                          {"owner": "me", "repo": "proj", "branch": "main"})
        head = self.gh.ref
        out = self.assert_fails(lambda: ghsync.run_clean(self.cns(dir=other)))
        self.assertIn("also syncs to", out)
        self.assertEqual(self.gh.ref, head)

    def test_next_status_is_clean_after_cleaning(self):
        self.setup_repo()
        self.clean()
        out = self.sync(dry_run=True)
        self.assertIn("In sync", out)


if __name__ == "__main__":
    unittest.main()
