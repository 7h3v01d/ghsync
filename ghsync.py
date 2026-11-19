#!/usr/bin/env python3
"""
ghsync - keep a local folder and a GitHub repo in sync, no git required.

Uses GitHub's REST "Git Data" API directly, so there is no 100-file upload limit.
Pure standard library (Python 3.8+).

Commands (run inside your project folder, or use -C <folder>):
  login                      store your GitHub token (once)
  init <name> [--public]     create a new GitHub repo, link this folder, and sync
  link <owner/repo>          link this folder to an existing repo
  status                     show what would be pushed / pulled / in conflict
  diff [paths...]            show differences (remote -> local)
  push                       send local changes to GitHub
  pull                       bring remote changes into the folder
  sync                       pull then push (conflicts are reported, not touched)

Common flags for push/pull/sync:
  -m "message"   commit message          --dry-run   show plan only
  --yes          skip delete prompts     --prefer local|remote   resolve conflicts

Ignore rules: built-in defaults plus .ghignore (or .gitignore if no .ghignore).
Per-project data lives in .ghsync/ (config + last-synced snapshot).
"""
import argparse
import base64
import difflib
import fnmatch
import getpass
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = os.environ.get("GHSYNC_API", "https://api.github.com")
STATE_DIR = ".ghsync"
MAX_FILE = 100 * 1024 * 1024  # GitHub hard limit per file
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".ghsync_token")
DEFAULT_IGNORE = [
    ".git/", ".ghsync/", "node_modules/", "__pycache__/", "*.pyc",
    ".DS_Store", "Thumbs.db", "desktop.ini", ".env", ".venv/", "venv/",
    "*.ghsync-tmp",
]


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code}: {msg}")
        self.code = code
        self.msg = msg


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- GitHub API
def get_token():
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    die("no GitHub token. Run: python ghsync.py login")


def api(method, path, body=None, raw=False):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + get_token())
        req.add_header("Accept", "application/vnd.github.raw+json" if raw else "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "ghsync")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                payload = r.read()
                if raw:
                    return payload
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            retry_after = e.headers.get("Retry-After")
            limited = e.code in (429,) or (e.code == 403 and ("rate limit" in msg.lower() or retry_after))
            if e.code in (500, 502, 503, 504) or limited:
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                last = ApiError(e.code, msg)
                time.sleep(min(wait, 60))
                continue
            raise ApiError(e.code, msg)
        except urllib.error.URLError as e:
            last = ApiError(0, str(e))
            time.sleep(2 ** attempt)
    raise last


# ------------------------------------------------------------- local helpers
def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)


def join_rel(a, b):
    return b if not a else a + "/" + b


def blob_sha_bytes(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def blob_sha_file(path, size):
    h = hashlib.sha1()
    h.update(b"blob %d\0" % size)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_ignore(root):
    pats = list(DEFAULT_IGNORE)
    for name in (".ghignore", ".gitignore"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("!"):
                        pats.append(line)
            break
    return pats


def ignored(rel, is_dir, pats):
    parts = rel.split("/")
    for pat in pats:
        dir_only = pat.endswith("/")
        p = pat.rstrip("/")
        anchored = p.startswith("/") or "/" in p
        p = p.lstrip("/")
        if not p:
            continue
        if anchored:
            for i in range(1, len(parts) + 1):
                last = i == len(parts)
                if dir_only and last and not is_dir:
                    continue
                if fnmatch.fnmatch("/".join(parts[:i]), p):
                    return True
        else:
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                if dir_only and last and not is_dir:
                    continue
                if fnmatch.fnmatch(part, p):
                    return True
    return False


def scan_local(root):
    cache_path = os.path.join(root, STATE_DIR, "cache.json")
    cache = read_json(cache_path, {})
    new_cache, files, skipped = {}, {}, []
    pats = load_ignore(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [
            d for d in dirnames
            if not ignored(join_rel(rel_dir, d), True, pats)
            and not os.path.islink(os.path.join(dirpath, d))
        ]
        for fn in filenames:
            rel = join_rel(rel_dir, fn)
            full = os.path.join(dirpath, fn)
            if ignored(rel, False, pats) or os.path.islink(full):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size > MAX_FILE:
                skipped.append((rel, st.st_size))
                continue
            c = cache.get(rel)
            if c and c[0] == st.st_size and c[1] == st.st_mtime_ns:
                sha = c[2]
            else:
                sha = blob_sha_file(full, st.st_size)
            new_cache[rel] = [st.st_size, st.st_mtime_ns, sha]
            files[rel] = sha
    write_json(cache_path, new_cache)
    return files, skipped


def local_path(root, rel):
    return os.path.join(root, *rel.split("/"))


def write_local(root, rel, data):
    full = local_path(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".ghsync-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, full)


def delete_local(root, rel):
    full = local_path(root, rel)
    try:
        os.remove(full)
    except FileNotFoundError:
        return
    d = os.path.dirname(full)
    while os.path.abspath(d) != os.path.abspath(root):
        try:
            os.rmdir(d)  # only succeeds if empty
        except OSError:
            break
        d = os.path.dirname(d)


# ------------------------------------------------------------ remote helpers
def fetch_remote(cfg):
    o, r, br = cfg["owner"], cfg["repo"], cfg["branch"]
    try:
        ref = api("GET", f"/repos/{o}/{r}/git/ref/heads/{br}")
    except ApiError as e:
        if e.code in (404, 409):
            die(f"branch '{br}' not found or repo is empty. Create the repo with a README first "
                f"(ghsync init does this).")
        raise
    commit_sha = ref["object"]["sha"]
    commit = api("GET", f"/repos/{o}/{r}/git/commits/{commit_sha}")
    tree_sha = commit["tree"]["sha"]
    tree = api("GET", f"/repos/{o}/{r}/git/trees/{tree_sha}?recursive=1")
    if tree.get("truncated"):
        die("remote tree is too large for a single API response (truncated); not safe to sync.")
    files, modes = {}, {}
    for e in tree["tree"]:
        if e["type"] == "blob":
            files[e["path"]] = e["sha"]
            modes[e["path"]] = e.get("mode", "100644")
    return {"commit": commit_sha, "tree": tree_sha, "files": files, "modes": modes}


def classify(base, local, remote):
    push, pull, conflict, same = {}, {}, {}, []
    for p in set(base) | set(local) | set(remote):
        b, l, r = base.get(p), local.get(p), remote.get(p)
        if l == r:
            if l is not None:
                same.append(p)
        elif l == b:
            pull[p] = r      # only remote changed (None = remote deleted)
        elif r == b:
            push[p] = l      # only local changed (None = local deleted)
        else:
            conflict[p] = (l, r)
    return push, pull, conflict, same


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def do_push(root, cfg, items, remote, message):
    """items: {path: local_sha or None}. Returns new commit sha."""
    o, r, br = cfg["owner"], cfg["repo"], cfg["branch"]
    have = set(remote["files"].values())
    need = sorted({sha: p for p, sha in items.items() if sha and sha not in have}.items())
    uploads = {}  # sha -> path (one representative file per unique content)
    for sha, p in need:
        uploads[sha] = p

    def up(item):
        sha, p = item
        with open(local_path(root, p), "rb") as f:
            data = f.read()
        got = blob_sha_bytes(data)
        if got != sha:
            raise RuntimeError(f"{p} changed while syncing; run again")
        res = api("POST", f"/repos/{o}/{r}/git/blobs",
                  {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        if res["sha"] != sha:
            raise RuntimeError(f"hash mismatch uploading {p}")
        return sha

    if uploads:
        print(f"  uploading {len(uploads)} file(s)...")
        done = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            for _ in ex.map(up, list(uploads.items())):
                done += 1
                if done % 25 == 0 or done == len(uploads):
                    print(f"    {done}/{len(uploads)}")

    entries = []
    for p, sha in sorted(items.items()):
        mode = remote["modes"].get(p, "100644")
        entries.append({"path": p, "mode": mode, "type": "blob", "sha": sha})
    tree_sha = remote["tree"]
    for part in chunks(entries, 500):
        tree_sha = api("POST", f"/repos/{o}/{r}/git/trees",
                       {"base_tree": tree_sha, "tree": part})["sha"]
    commit = api("POST", f"/repos/{o}/{r}/git/commits",
                 {"message": message, "tree": tree_sha, "parents": [remote["commit"]]})
    try:
        api("PATCH", f"/repos/{o}/{r}/git/refs/heads/{br}", {"sha": commit["sha"]})
    except ApiError as e:
        if e.code == 422:
            die("remote changed while pushing (not a fast-forward). Run the command again.")
        raise
    return commit["sha"]


def do_pull(root, cfg, items):
    o, r = cfg["owner"], cfg["repo"]
    fetches = [(p, sha) for p, sha in items.items() if sha]

    def down(item):
        p, sha = item
        data = api("GET", f"/repos/{o}/{r}/git/blobs/{sha}", raw=True)
        if blob_sha_bytes(data) != sha:
            raise RuntimeError(f"download of {p} failed hash check")
        write_local(root, p, data)

    if fetches:
        print(f"  downloading {len(fetches)} file(s)...")
        done = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            for _ in ex.map(down, fetches):
                done += 1
                if done % 25 == 0 or done == len(fetches):
                    print(f"    {done}/{len(fetches)}")
    for p, sha in items.items():
        if sha is None:
            delete_local(root, p)


# ------------------------------------------------------------------ commands
def load_cfg(args):
    root = os.path.abspath(args.dir or os.getcwd())
    cfg = read_json(os.path.join(root, STATE_DIR, "config.json"), None)
    if not cfg:
        die("this folder is not linked. Use 'init <name>' or 'link owner/repo' first.")
    return root, cfg


def gather(root, cfg):
    state = read_json(os.path.join(root, STATE_DIR, "state.json"), {"files": {}})
    local, skipped = scan_local(root)
    remote = fetch_remote(cfg)
    return state.get("files", {}), local, remote, skipped


def label(p, base, local, remote):
    b, l, r = base.get(p), local.get(p), remote["files"].get(p)
    return "A" if (l is not None and b is None and r is None) else \
        "D" if l is None else "M"


def show_plan(base, local, remote, push, pull, conflict, skipped):
    def block(title, lines):
        if not lines:
            return
        print(f"\n{title} ({len(lines)})")
        for ln in lines[:60]:
            print("  " + ln)
        if len(lines) > 60:
            print(f"  ... and {len(lines) - 60} more")

    def tag(p, new_val, old_val):
        return "A" if old_val is None else ("D" if new_val is None else "M")

    block("PUSH  (local -> GitHub)",
          [f"{tag(p, v, remote['files'].get(p))} {p}" for p, v in sorted(push.items())])
    block("PULL  (GitHub -> local)",
          [f"{tag(p, v, local.get(p))} {p}" for p, v in sorted(pull.items())])
    block("CONFLICT  (changed on both sides)", sorted(conflict))
    if skipped:
        print(f"\nSkipped {len(skipped)} file(s) over 100 MB (GitHub limit):")
        for p, s in skipped[:10]:
            print(f"  {p} ({s / 1048576:.0f} MB)")
    if not (push or pull or conflict):
        print("\nIn sync. Nothing to do.")


def cmd_status(args):
    root, cfg = load_cfg(args)
    base, local, remote, skipped = gather(root, cfg)
    push, pull, conflict, same = classify(base, local, remote["files"])
    print(f"{cfg['owner']}/{cfg['repo']} @ {cfg['branch']}  (remote head {remote['commit'][:7]})")
    show_plan(base, local, remote, push, pull, conflict, skipped)
    if conflict:
        print("\nResolve with: diff <path>, then sync --prefer local|remote")


def cmd_diff(args):
    root, cfg = load_cfg(args)
    base, local, remote, _ = gather(root, cfg)
    push, pull, conflict, _ = classify(base, local, remote["files"])
    targets = [p.replace("\\", "/") for p in args.paths] or sorted(list(push) + list(pull) + list(conflict))
    if not targets:
        print("No differences.")
        return
    o, r = cfg["owner"], cfg["repo"]
    for p in targets:
        lsha, rsha = local.get(p), remote["files"].get(p)
        if lsha == rsha:
            continue
        a = b = b""
        if rsha:
            a = api("GET", f"/repos/{o}/{r}/git/blobs/{rsha}", raw=True)
        if lsha:
            with open(local_path(root, p), "rb") as f:
                b = f.read()
        print(f"\n=== {p} ===")
        try:
            at, bt = a.decode("utf-8"), b.decode("utf-8")
        except UnicodeDecodeError:
            print(f"binary file differs (remote {len(a)} bytes, local {len(b)} bytes)")
            continue
        lines = list(difflib.unified_diff(
            at.splitlines(), bt.splitlines(),
            fromfile=f"remote/{p}" if rsha else "/dev/null",
            tofile=f"local/{p}" if lsha else "/dev/null", lineterm=""))
        for ln in lines[:400]:
            print(ln)
        if len(lines) > 400:
            print(f"... {len(lines) - 400} more diff lines")


def run_sync(args, mode):
    root, cfg = load_cfg(args)
    base, local, remote, skipped = gather(root, cfg)
    push, pull, conflict, same = classify(base, local, remote["files"])

    only = getattr(args, "only", None)        # optional set of paths to act on (GUI)
    if only is not None:
        push = {p: v for p, v in push.items() if p in only}
        pull = {p: v for p, v in pull.items() if p in only}
        conflict = {p: v for p, v in conflict.items() if p in only}
    resolve = getattr(args, "resolve", None) or {}   # optional {path: 'local'|'remote'} (GUI)
    for p, (l, r) in list(conflict.items()):
        side = resolve.get(p) or args.prefer
        if side == "local":
            push[p] = l
            del conflict[p]
        elif side == "remote":
            pull[p] = r
            del conflict[p]
    if mode == "push":
        pull = {}
    elif mode == "pull":
        push = {}

    print(f"{cfg['owner']}/{cfg['repo']} @ {cfg['branch']}")
    show_plan(base, local, remote, push, pull, conflict, skipped)
    if args.dry_run or not (push or pull):
        return

    deletes_local = [p for p, v in pull.items() if v is None]
    deletes_remote = [p for p, v in push.items() if v is None]
    if (deletes_local or deletes_remote) and not args.yes:
        print(f"\nThis will delete {len(deletes_local)} local and {len(deletes_remote)} remote file(s).")
        if input("Continue? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return

    new_commit = remote["commit"]
    if pull:
        print("\nPulling...")
        do_pull(root, cfg, pull)
    if push:
        print("\nPushing...")
        msg = args.message or f"ghsync: {len(push)} file(s) changed ({time.strftime('%Y-%m-%d %H:%M')})"
        new_commit = do_push(root, cfg, push, remote, msg)

    # Snapshot of what both sides agree on now. Unresolved conflicts keep their old base.
    final = {}
    for p in set(base) | set(local) | set(remote["files"]):
        if p in push:
            v = push[p]
        elif p in pull:
            v = pull[p]
        elif p in conflict:
            v = base.get(p)
        else:
            v = local.get(p) if local.get(p) == remote["files"].get(p) else base.get(p)
        if v is not None:
            final[p] = v
    write_json(os.path.join(root, STATE_DIR, "state.json"), {"commit": new_commit, "files": final})
    print(f"\nDone. Remote head {new_commit[:7]}." + (f"  {len(conflict)} conflict(s) left untouched." if conflict else ""))


def cmd_login(args):
    tok = getpass.getpass("Paste GitHub token (classic token with 'repo' scope): ").strip()
    if not tok:
        die("empty token")
    os.environ["GITHUB_TOKEN"] = tok
    me = api("GET", "/user")
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    print(f"Logged in as {me['login']}. Token saved to {TOKEN_FILE}")


def save_cfg(root, owner, repo, branch):
    write_json(os.path.join(root, STATE_DIR, "config.json"),
               {"owner": owner, "repo": repo, "branch": branch})


def cmd_link(args):
    root = os.path.abspath(args.dir or os.getcwd())
    if "/" not in args.repo:
        die("use owner/repo")
    owner, repo = args.repo.split("/", 1)
    info = api("GET", f"/repos/{owner}/{repo}")
    branch = args.branch or info["default_branch"]
    save_cfg(root, owner, repo, branch)
    print(f"Linked {root} -> {owner}/{repo} ({branch}).")
    print("Run 'status' to compare. Files that exist on both sides with different content show as CONFLICT.")


def cmd_init(args):
    root = os.path.abspath(args.dir or os.getcwd())
    body = {"name": args.name, "private": not args.public, "auto_init": True,
            "description": args.description or ""}
    path = f"/orgs/{args.org}/repos" if args.org else "/user/repos"
    try:
        info = api("POST", path, body)
    except ApiError as e:
        die(f"could not create repo: {e}")
    save_cfg(root, info["owner"]["login"], info["name"], info["default_branch"])
    print(f"Created {info['full_name']} ({'public' if args.public else 'private'}).")
    args.message, args.dry_run, args.yes, args.prefer = args.message or "Initial upload via ghsync", False, True, None
    run_sync(args, "sync")


def main():
    ap = argparse.ArgumentParser(description="Sync a folder with GitHub without git.")
    ap.add_argument("-C", dest="dir", help="project folder (default: current folder)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login")
    p = sub.add_parser("init")
    p.add_argument("name")
    p.add_argument("--public", action="store_true")
    p.add_argument("--org")
    p.add_argument("--description")
    p.add_argument("-m", "--message")
    p = sub.add_parser("link")
    p.add_argument("repo")
    p.add_argument("--branch")
    sub.add_parser("status")
    p = sub.add_parser("diff")
    p.add_argument("paths", nargs="*")
    for name in ("push", "pull", "sync"):
        p = sub.add_parser(name)
        p.add_argument("-m", "--message")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--yes", action="store_true")
        p.add_argument("--prefer", choices=["local", "remote"])

    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        if args.cmd == "login":
            cmd_login(args)
        elif args.cmd == "init":
            cmd_init(args)
        elif args.cmd == "link":
            cmd_link(args)
        elif args.cmd == "status":
            cmd_status(args)
        elif args.cmd == "diff":
            cmd_diff(args)
        else:
            run_sync(args, args.cmd)
    except ApiError as e:
        die(str(e))
    except KeyboardInterrupt:
        die("interrupted")


if __name__ == "__main__":
    main()
