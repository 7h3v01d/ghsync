# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
Smoke test against the REAL GitHub API, using a disposable repository.

  set GITHUB_TOKEN=...                       (or run `python ghsync.py login` first)
  python tests\\smoke_real_github.py owner/throwaway-repo --i-understand owner/throwaway-repo

The repository must already exist with at least one commit (create it on github.com
with "Add a README"). The test adds, changes and deletes files under smoke/ in that
repo and leaves a few commits behind. Never point it at a repo you care about.

Exit code 0 = every step passed (20 steps).
"""
import argparse
import base64
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Use a throwaway project list so the real one (%APPDATA%\ghsync\projects.json) is never touched.
_SMOKE_HOME = tempfile.mkdtemp(prefix="ghsync-smoke-home-")
os.environ["GHSYNC_HOME"] = _SMOKE_HOME
import ghsync  # noqa: E402
import ghsync_projects  # noqa: E402

RESULTS = []


def step(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n  {detail}" if detail and not ok else ""))


def quiet(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return fn(*a), buf.getvalue()


def settle(cfg, sha, timeout=45):
    """Wait until GitHub reports `sha` as the branch head (reads can lag just after a move)."""
    end = time.time() + timeout
    while time.time() < end:
        if ghsync.remote_head(cfg) == sha:
            return True
        time.sleep(1)
    print(f"  (warning: GitHub still not reporting {sha[:7]} as head after {timeout}s)")
    return False


_LAST = {"sync": None, "head": None}    # newest head we caused: by ghsync or behind its back


def remote_commit(cfg, files, message):
    """Change the repo behind ghsync's back, like an edit on github.com would."""
    head = ghsync.remote_head(cfg)
    tree = ghsync.api("GET", ghsync.repo_api(cfg, f"/git/commits/{head}"))["tree"]["sha"]
    entries = []
    for p, data in files.items():
        if data is None:
            entries.append({"path": p, "mode": "100644", "type": "blob", "sha": None})
        else:
            sha = ghsync.api("POST", ghsync.repo_api(cfg, "/git/blobs"),
                             {"content": base64.b64encode(data).decode(), "encoding": "base64"})["sha"]
            entries.append({"path": p, "mode": "100644", "type": "blob", "sha": sha})
    t = ghsync.api("POST", ghsync.repo_api(cfg, "/git/trees"), {"base_tree": tree, "tree": entries})["sha"]
    c = ghsync.api("POST", ghsync.repo_api(cfg, "/git/commits"), {"message": message, "tree": t, "parents": [head]})
    ghsync.api("PATCH", ghsync.ref_api(cfg, single=False), {"sha": c["sha"]})
    _LAST["head"] = c["sha"]
    settle(cfg, c["sha"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--i-understand", required=True, metavar="OWNER/REPO",
                    help="repeat the repo name to confirm it may be modified")
    a = ap.parse_args()
    if a.repo != a.i_understand:
        sys.exit("repo names don't match; refusing")
    owner, repo = a.repo.split("/", 1)
    info = ghsync.api("GET", ghsync.repo_api({"owner": owner, "repo": repo}))
    cfg = {"owner": owner, "repo": repo, "branch": info["default_branch"]}

    tmp = tempfile.mkdtemp(prefix="ghsync-smoke-")
    root = os.path.join(tmp, "proj")
    os.makedirs(root)
    ghsync.save_cfg(root, **cfg, protect_local=False)     # steps 1-8: two-way sync
    ns = argparse.Namespace(dir=root, message=None, dry_run=False, yes=True, prefer=None,
                            resolve=None, only=None)
    stamp = time.strftime("%H%M%S")

    def path(rel):
        return os.path.join(root, *rel.split("/"))

    def write(rel, data):
        os.makedirs(os.path.dirname(path(rel)), exist_ok=True)
        with open(path(rel), "wb") as f:
            f.write(data)

    def synced_head():
        st = ghsync.read_json(os.path.join(root, ".ghsync", "state.json"), {}) or {}
        return st.get("commit")

    def remote_files():
        # Wait for the newest head we caused. That is ghsync's last sync, unless this test has
        # committed behind ghsync's back since - then ghsync's commit is no longer the head
        # and waiting for it would only time out (the 45 s warning in 0.5.0's smoke run).
        sh = synced_head()
        if sh != _LAST["sync"]:
            _LAST["sync"] = _LAST["head"] = sh
        if _LAST["head"]:
            settle(cfg, _LAST["head"])
        return ghsync.fetch_remote(cfg)["files"]

    try:
        # 1. adopt the repo (pull whatever is there), then push new files
        quiet(ghsync.run_sync, ns, "sync")
        write("smoke/a.txt", f"a {stamp}".encode())
        write("smoke/sub/b.txt", f"b {stamp}".encode())
        write("smoke/big-but-ok.bin", os.urandom(300_000))
        quiet(ghsync.run_sync, ns, "sync")
        rf = remote_files()
        step("push adds files", all(p in rf for p in ("smoke/a.txt", "smoke/sub/b.txt", "smoke/big-but-ok.bin")))

        # 2. remote edit + remote delete -> pull
        remote_commit(cfg, {"smoke/a.txt": b"edited on github", "smoke/sub/b.txt": None}, "smoke: remote edit")
        quiet(ghsync.run_sync, ns, "sync")
        step("pull applies remote edit", open(path("smoke/a.txt"), "rb").read() == b"edited on github")
        step("pull applies remote delete", not os.path.exists(path("smoke/sub/b.txt")))

        # 3. local delete -> push
        os.remove(path("smoke/big-but-ok.bin"))
        quiet(ghsync.run_sync, ns, "sync")
        step("push deletes remotely", "smoke/big-but-ok.bin" not in remote_files())

        # 4. conflict is left alone, then resolved
        write("smoke/a.txt", b"local side")
        remote_commit(cfg, {"smoke/a.txt": b"remote side"}, "smoke: conflict")
        _, out = quiet(ghsync.run_sync, ns, "sync")
        step("conflict reported and untouched",
             "CONFLICT" in out and open(path("smoke/a.txt"), "rb").read() == b"local side")
        quiet(ghsync.run_sync, argparse.Namespace(**{**vars(ns), "prefer": "local"}), "sync")
        rf = remote_files()
        blob = ghsync.api("GET", ghsync.repo_api(cfg, f"/git/blobs/{rf['smoke/a.txt']}"), raw=True)
        step("conflict resolved with --prefer local", blob == b"local side")

        # 5. reserved / ignored remote paths are never written locally
        remote_commit(cfg, {"smoke/.env": b"SECRET=1", ".ghsync/config.json": b'{"owner":"x"}'},
                      "smoke: hostile paths")
        quiet(ghsync.run_sync, ns, "sync")
        cfg_now = ghsync.read_json(os.path.join(root, ".ghsync", "config.json"), {})
        step("remote .ghsync/ ignored", cfg_now.get("owner") == owner)
        step("remote ignored file not pulled", not os.path.exists(path("smoke/.env")))

        # 6. newly ignored file is not deleted from GitHub
        write("smoke/keep.txt", b"keep")
        quiet(ghsync.run_sync, ns, "sync")
        with open(path(".ghignore"), "w") as f:
            f.write("smoke/keep.txt\n.ghignore\n")
        quiet(ghsync.run_sync, ns, "sync")
        step("newly ignored file kept on GitHub", "smoke/keep.txt" in remote_files())
        os.remove(path(".ghignore"))

        # 7. lock: a second operation is refused while one holds the folder
        with ghsync.ProjectLock(root):
            import subprocess
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(ghsync.__file__), "ghsync.py"),
                                "-C", root, "sync", "--yes"], capture_output=True, text=True, timeout=120)
        step("concurrent run refused", r.returncode != 0 and "another ghsync" in r.stderr, r.stderr.strip())

        # 8. restart after a fresh state file: nothing gets deleted
        os.remove(os.path.join(root, ".ghsync", "state.json"))
        before = remote_files()
        quiet(ghsync.run_sync, ns, "sync")
        step("re-baseline deletes nothing", set(before) <= set(remote_files()))

        # 9. Protect local (0.3): GitHub mirrors the folder, the folder is never touched
        ghsync.set_protect(root, True)
        remote_commit(cfg, {"smoke/a.txt": b"edited on github again", "smoke/junk.txt": b"leftover"},
                      "smoke: drift on github")
        _, out = quiet(ghsync.run_sync, ns, "sync")
        rf = remote_files()
        blob = ghsync.api("GET", ghsync.repo_api(cfg, f"/git/blobs/{rf['smoke/a.txt']}"), raw=True)
        step("protect: folder untouched", open(path("smoke/a.txt"), "rb").read() == b"local side")
        ok = blob == b"local side" and "smoke/junk.txt" not in rf
        step("protect: GitHub edit reverted and GitHub-only file removed", ok,
             f"GitHub a.txt={blob[:40]!r}, junk.txt on GitHub={'smoke/junk.txt' in rf}, "
             f"head={ghsync.remote_head(cfg)[:7]}, last synced={(synced_head() or '?')[:7]}\n"
             "  ghsync said:\n    " + "\n    ".join(out.strip().splitlines()[-15:]))
        remote_commit(cfg, {"smoke/a.txt": b"one more github edit"}, "smoke: drift again")
        try:
            quiet(ghsync.run_sync, ns, "pull")
            refused = False
        except ghsync.ProtectedLocal:
            refused = True
        step("protect: pull refused without override",
             refused and open(path("smoke/a.txt"), "rb").read() == b"local side")

        # 10. Secrets check (0.5): a fake token is refused before anything is uploaded
        fake_token = "gh" + "p_" + "sM0k3" * 7 + "x"       # assembled at runtime, never a real token
        write("smoke/creds.py", f"TOKEN = '{fake_token}'\n".encode())
        head = ghsync.remote_head(cfg)
        try:
            quiet(ghsync.run_sync, ns, "sync")
            blocked = False
        except ghsync.SecretsFound as e:
            blocked = fake_token not in str(e)
        step("secrets: push with a token refused, nothing uploaded",
             blocked and ghsync.remote_head(cfg) == head and "smoke/creds.py" not in remote_files())
        # 10b. GHS-017 (0.5.1): a file that can't be read during the scan is refused, not
        #      treated as clean - even with --allow-secrets - and nothing is uploaded
        write("smoke/creds2.py", f"TOKEN = '{fake_token}'\n".encode())
        real_read, fails = ghsync.read_local, [True]

        def flaky_read(r, rel):
            if rel == "smoke/creds2.py" and fails[0]:
                fails[0] = False
                raise PermissionError(13, "in use (simulated)")
            return real_read(r, rel)
        ghsync.read_local = flaky_read
        try:
            quiet(ghsync.run_sync, argparse.Namespace(**{**vars(ns), "allow_secrets": True}), "sync")
            unchecked = False
        except ghsync.SecretsUnchecked:
            unchecked = True
        finally:
            ghsync.read_local = real_read
        step("secrets: unreadable file refused even with --allow-secrets",
             unchecked and ghsync.remote_head(cfg) == head and "smoke/creds2.py" not in remote_files())
        os.remove(path("smoke/creds.py"))
        os.remove(path("smoke/creds2.py"))

        # 11. Clean-up (0.5): an ignored file left on GitHub is removed; the local copy stays
        remote_commit(cfg, {"smoke/pkg.egg-info/PKG-INFO": b"stale metadata"}, "smoke: stale egg-info")
        write("smoke/pkg.egg-info/PKG-INFO", b"local metadata")
        # 11a. GHS-018 (0.5.1): approval is bound to GitHub's content, not just the name
        for _ in range(30):                     # until GitHub's reads show the stale file
            snap = ghsync.gather(root, cfg)
            if "smoke/pkg.egg-info/PKG-INFO" in ghsync.clean_targets(snap):
                break
            time.sleep(1.5)
        digest = ghsync.clean_digest(cfg, snap["remote"]["files"], ghsync.clean_targets(snap))
        remote_commit(cfg, {"smoke/pkg.egg-info/PKG-INFO": b"newer, never reviewed"}, "smoke: egg-info v2")
        try:
            quiet(ghsync.run_clean, argparse.Namespace(dir=root, dry_run=False, yes=True, message=None,
                                                       expect=digest))
            refused = False
        except ghsync.PlanChanged:
            refused = True
        step("clean: target changed on GitHub after review is refused",
             refused and "smoke/pkg.egg-info/PKG-INFO" in remote_files())
        res, _ = quiet(ghsync.run_clean, argparse.Namespace(dir=root, dry_run=False, yes=True, message=None))
        step("clean: ignored file removed from GitHub, local copy kept",
             "smoke/pkg.egg-info/PKG-INFO" in res.get("removed", [])
             and "smoke/pkg.egg-info/PKG-INFO" not in remote_files()
             and open(path("smoke/pkg.egg-info/PKG-INFO"), "rb").read() == b"local metadata")

        # 12. Multi-project (0.4.1): a second folder can't take over a registered project's branch
        ghsync_projects.add_project(root)
        other = os.path.join(tmp, "second-folder")
        os.makedirs(other)
        with open(os.path.join(other, "intruder.txt"), "w") as f:
            f.write("must never reach GitHub")
        ghsync.write_json(os.path.join(other, ".ghsync", "config.json"),
                          {"owner": owner, "repo": repo, "branch": cfg["branch"], "protect_local": False})
        remote_files()                         # wait until GitHub reports what we last pushed
        head = synced_head()
        ns2 = argparse.Namespace(**{**vars(ns), "dir": other})
        try:
            quiet(ghsync.run_sync, ns2, "sync")
            refused = False
        except ghsync.DestinationClash:
            refused = True
        step("multi: second folder on the same branch refused (engine)",
             refused and ghsync.remote_head(cfg) == head)
        import subprocess
        r = subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(ghsync.__file__), "ghsync.py"),
                            "-C", other, "sync", "--yes"], capture_output=True, text=True, timeout=120,
                           env=dict(os.environ))
        step("multi: ...and refused through the CLI",
             r.returncode != 0 and "also syncs to" in r.stderr and ghsync.remote_head(cfg) == head
             and "intruder.txt" not in ghsync.fetch_remote(cfg)["files"], r.stderr.strip()[-300:])

        # clean up what the smoke test added
        remote_commit(cfg, {p: None for p in ghsync.fetch_remote(cfg)["files"]
                            if p.startswith(("smoke/", ".ghsync/"))},
                      "smoke: cleanup")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(_SMOKE_HOME, ignore_errors=True)

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} steps passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
