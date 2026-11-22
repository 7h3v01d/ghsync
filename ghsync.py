#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
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
  sync                       Protect local ON (default): make GitHub mirror this folder
                             Protect local OFF: pull then push (conflicts reported)
  protect [on|off]           show or change Protect local for this folder
  clean [--dry-run]          remove files your ignore rules exclude from GitHub (local copies kept)

Common flags for push/pull/sync:
  -m "message"   commit message          --dry-run   show plan only
  --yes          skip delete prompts     --prefer local|remote   resolve conflicts
  --allow-secrets            push files flagged as possible secrets, this once
  --allow-local-overwrite    one-time permission to replace/delete files in the folder
                             while Protect local is on (they are backed up first)

Ignore rules: built-in defaults plus every one of .ghignore, .gitignore and .packagerignore
found in the project root. A path matched by any of them is not synced.
Per-project data lives in .ghsync/ (config + last-synced snapshot).

Protect local (0.3, on by default): the folder is the single source of truth. Nothing in
it is ever replaced or deleted; sync makes GitHub match it (reverting edits made on GitHub,
restoring files deleted there, removing files that exist only there). Pulling needs a
one-time --allow-local-overwrite, and every local file it replaces is first copied to
.ghsync/backup/<time>/.

Safety model (0.2):
  * One path policy for both sides. Paths GitHub sends are checked before anything is
    written: no '..', no absolute paths, nothing under .ghsync/ or .git/, no names that
    Windows can't hold. Ignore rules apply to GitHub's files too.
  * Writes and deletes stay inside the project: every parent folder is checked, and a
    symlink, junction or other redirecting reparse point anywhere on the way is refused.
  * Only a file that is confirmed absent counts as deleted. Files that are ignored, too
    big, unreadable or behind a link are "held": never pushed, pulled or deleted.
  * Right before a pull replaces or deletes a file, its current content is compared with
    what the scan saw. If you edited it in the meantime, that file is skipped.
  * One mutating operation per project at a time (OS-level lock, released if the
    process dies). State files are written atomically.
"""
import argparse
import base64
import contextlib
import difflib
import fnmatch
import getpass
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

__version__ = "0.5.1"

API = os.environ.get("GHSYNC_API", "https://api.github.com")
STATE_DIR = ".ghsync"
MAX_FILE = 100 * 1024 * 1024  # GitHub hard limit per file
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".ghsync_token")
TMP_SUFFIX = ".ghsync-tmp"
DEFAULT_IGNORE = [
    ".git/", ".ghsync/", "node_modules/", "__pycache__/", "*.pyc",
    ".DS_Store", "Thumbs.db", "desktop.ini", ".env", ".venv/", "venv/",
    # tool caches and generated metadata: always rebuilt, never source (0.4.3)
    ".pytest_cache/", "*.egg-info/", ".mypy_cache/", ".ruff_cache/", ".tox/", ".nox/",
    ".hypothesis/", ".ipynb_checkpoints/",
    "*" + TMP_SUFFIX,
]
# build/ and dist/ are deliberately NOT built in: some projects commit them, and built-in
# rules can't be overridden. Add them to .gitignore/.ghignore/.packagerignore if wanted.
CASE_INSENSITIVE_FS = sys.platform in ("win32", "darwin")

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_REPARSE_POINT = 0x400            # FILE_ATTRIBUTE_REPARSE_POINT
_NAME_SURROGATE = 0x20000000      # reparse tags that redirect a path (symlinks, junctions, mounts)
_TEMP_REASON = "ghsync temporary file"
# Temp files ghsync creates next to project files. The run id is written to
# .ghsync/journal.json *before* any such file exists, and cleanup only deletes exact
# matches whose run id is in the journal. A name alone never proves ownership.
_RUN_TEMP_RE = re.compile(r"^\.ghsync-([0-9a-f]{16})-([0-9]{1,9})\.ghsync-tmp$")
# Temp files for ghsync's own JSON files, only ever inside the reserved .ghsync/ folder.
_STATE_TEMP_RE = re.compile(r"^\.tmp-[0-9a-f]{12}\.ghsync-tmp$")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code}: {msg}")
        self.code = code
        self.msg = msg


class GhsyncError(RuntimeError):
    """A refusal with a message meant for the user."""


class UnsafePath(GhsyncError):
    pass


class LocalChanged(GhsyncError):
    pass


class LockBusy(GhsyncError):
    pass


class StateError(GhsyncError):
    pass


class PlanChanged(GhsyncError):
    pass


class ProtectedLocal(GhsyncError):
    pass


class DestinationClash(GhsyncError):
    pass


class SecretsFound(GhsyncError):
    def __init__(self, findings):
        self.findings = findings
        super().__init__(
            f"{len(findings)} possible secret(s) in files about to be pushed:\n"
            + format_secret_findings(findings)
            + "\nNothing was pushed. Remove them (or add the files to .gitignore) and try again. "
              "To push them anyway this once: --allow-secrets on the command line, or confirm "
              "in the window.")


class SecretsUnchecked(SecretsFound):
    """Files about to be pushed could not be inspected. Never approvable: nothing was seen."""

    def __init__(self, findings):
        self.findings = findings
        GhsyncError.__init__(
            self,
            f"{len(findings)} file(s) about to be pushed could not be checked for secrets:\n"
            + format_secret_findings(findings)
            + "\nNothing was pushed. This is usually a file that was open or still being written. "
              "Close it and try again. (Approving secrets doesn't cover this, because nothing "
              "was actually inspected.)")


def _projects():
    """The project registry module (ghsync_projects.py, shipped alongside), or None.

    When this file runs as a script it is __main__; alias it as 'ghsync' first so the
    registry module imports *this* module rather than a second copy of it.
    """
    sys.modules.setdefault("ghsync", sys.modules[__name__])
    mod = sys.modules.get("ghsync_projects")
    if mod is not None:
        return mod
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ghsync_projects.py")
    if not os.path.exists(path):
        return None              # engine used on its own (0.3.x layout): no project registry
    # Load by explicit path: works however Python was started (e.g. `python -I`, which
    # leaves the script's folder off sys.path). A present but broken module fails closed.
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("ghsync_projects", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ghsync_projects"] = mod
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        sys.modules.pop("ghsync_projects", None)
        raise DestinationClash(f"the project registry module couldn't be loaded ({e}); "
                               "refusing to sync until it can be checked") from None
    return mod


def nesting_guard(root):
    """GHS-016: refuse a folder nested inside, or containing, a listed project."""
    gp = _projects()
    if gp is None:
        return
    msg = gp.overlap_message(root)
    if msg:
        raise DestinationClash(msg)


def destination_guard(root, owner=None, repo=None, branch=None):
    """GHS-014: refuse if another registered project owns this repo + branch.
    GHS-016: refuse if this folder is nested inside, or contains, a listed project.

    Enforced here, at the operation boundary, so the CLI obeys the same rule as the GUI.
    With owner/repo/branch it checks a destination about to be linked; without, the
    folder's current config. Reads the registry snapshot (written atomically) without
    taking the registry lock, so no lock ordering exists between the two kinds of lock.
    A damaged registry fails closed. No registry module or file: no restriction.
    """
    gp = _projects()
    if gp is None:
        return
    nesting_guard(root)
    if owner is not None:
        try:
            gp.check_destination(root, owner, repo, branch)
        except gp.RegistryError as e:
            raise DestinationClash(str(e)) from None
        return
    msg = gp.clash_message(root)
    if msg:
        raise DestinationClash(msg)


_PROTECT_HELP = ("Protect local is on for this folder, so ghsync won't replace or delete files in it. "
                 "To do it once, use --allow-local-overwrite (the files are backed up to "
                 ".ghsync/backup/ first), or turn protection off with: ghsync protect off")


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
        req.add_header("User-Agent", "ghsync/" + __version__)
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


def repo_api(cfg, suffix=""):
    q = urllib.parse.quote
    return f"/repos/{q(cfg['owner'], safe='')}/{q(cfg['repo'], safe='')}{suffix}"


def ref_api(cfg, single=True):
    return repo_api(cfg, f"/git/{'ref' if single else 'refs'}/heads/{urllib.parse.quote(cfg['branch'], safe='/')}")


def remote_head(cfg):
    return api("GET", ref_api(cfg))["object"]["sha"]


# ------------------------------------------------------------- path policy
_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                 | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})
_BAD_CHARS = set('<>:"|?*\\') | {chr(i) for i in range(32)}
_RESERVED_DIRS = {".ghsync", ".git"}


def check_repo_path(rel):
    """Return None if `rel` is a path ghsync may read or write, else the reason it may not.

    Applied to every path from either side before it is classified, so a repository can
    never address the tool's own state, escape the project, or create names Windows
    cannot represent faithfully.
    """
    if not isinstance(rel, str) or not rel:
        return "empty path"
    if rel.startswith("/"):
        return "absolute path"
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return "empty, '.' or '..' path segment"
        if len(part) > 255:
            return "name longer than 255 characters"
        bad = next((c for c in part if c in _BAD_CHARS), None)
        if bad is not None:
            return f"character {bad!r} is not allowed in Windows file names"
        if part[-1] in ". ":
            return "name ends with a dot or space (Windows would rename it)"
        if part.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED:
            return "reserved Windows device name"
        if part.casefold() in _RESERVED_DIRS:
            return f"'{part}/' is reserved"
        if part.endswith(TMP_SUFFIX):
            return "the .ghsync-tmp suffix is reserved"
    return None


def _nfc(s):
    return unicodedata.normalize("NFC", s)


# ------------------------------------------------------------- secrets check (0.5)
SECRET_SCAN_MAX = 2 * 1024 * 1024      # content of larger files isn't scanned (names still are)
_SECRET_PATTERNS = [
    ("GitHub token", re.compile(
        r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})(?![A-Za-z0-9_])")),
    ("OpenAI/Anthropic API key", re.compile(
        r"(?<![A-Za-z0-9_-])sk-(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}")),
    ("AWS access key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    ("Google API key", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])")),
    ("Slack token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("Stripe live key", re.compile(r"(?<![A-Za-z0-9])(?:sk|rk)_live_[0-9A-Za-z]{24,}")),
    # assembled so this source line can't match itself
    ("private key", re.compile("-----BEGIN (?:[A-Z0-9]+ )*PRIV" + "ATE KEY(?: BLOCK)?-----")),
]
_SECRET_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".pypirc", ".git-credentials",
                 ".htpasswd", "credentials.json", "key.txt", "keys.txt", "token.txt", "tokens.txt",
                 "secrets.json", "secrets.yaml", "secrets.yml", "secrets.toml"}
# Deliberately not secret.txt / secrets.txt: as often notes as credentials (too noisy).
_SECRET_GLOBS = ("*.pem", "*.key", "*.p12", "*.pfx", "*.ppk", "*.keystore", "*.jks", "*.kdbx")
_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults"}
_SECRET_CACHE = {}


def _secret_name(name):
    n = name.lower()
    if n in _SECRET_NAMES:
        return True
    if n == ".env" or (n.startswith(".env.") and n not in _ENV_TEMPLATES):
        return True
    return any(fnmatch.fnmatch(n, g) for g in _SECRET_GLOBS)


def _mask(text):
    return f"{text[:4]}…({len(text)} chars)"


UNCHECKED = "could not be checked"


def _scan_one(root, rel, sha):
    """(findings, verified). verified=False means the content was NOT inspected: never cache
    that as clean, and never let it through (GHS-017)."""
    found = []
    if _secret_name(rel.rsplit("/", 1)[-1]):
        found.append({"kind": "file name looks like a secret", "line": 0, "preview": ""})
    try:
        data = read_local(root, rel)
    except (OSError, GhsyncError) as e:
        why = getattr(e, "strerror", None) or type(e).__name__
        found.append({"kind": f"{UNCHECKED} (read failed: {why})", "line": 0, "preview": "",
                      "unchecked": True})
        return found, False
    if blob_sha_bytes(data) != sha:
        found.append({"kind": f"{UNCHECKED} (file changed since it was scanned)", "line": 0,
                      "preview": "", "unchecked": True})
        return found, False
    if len(data) > SECRET_SCAN_MAX or b"\0" in data[:8192]:
        return found, True          # inspected; content deliberately not pattern-scanned
    text = data.decode("utf-8", "replace")
    for kind, rx in _SECRET_PATTERNS:
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            preview = "-----BEGIN …" if kind == "private key" else _mask(m.group())
            found.append({"kind": kind, "line": line, "preview": preview})
            break                                   # one hit per kind per file is enough
    return found, True


def find_secrets(root, items):
    """Possible secrets in the files of `items` ({path: blob sha}; None = deletion, skipped).

    High-confidence only: credential-shaped file names, and token / private-key patterns
    in text files up to SECRET_SCAN_MAX. The matched secret itself is never returned,
    only a masked preview. Results of a successful inspection are cached by content, so
    rescans are free. A file that could not be inspected gets an "unchecked" finding instead,
    which is never cached and never approvable.
    """
    if len(_SECRET_CACHE) > 50000:
        _SECRET_CACHE.clear()
    out = []
    for rel, sha in sorted(items.items()):
        if not sha:
            continue
        key = (sha, rel.rsplit("/", 1)[-1].lower(), SECRET_SCAN_MAX)
        found = _SECRET_CACHE.get(key)
        if found is None:
            found, verified = _scan_one(root, rel, sha)
            if verified:
                _SECRET_CACHE[key] = found
        out.extend(dict(f, path=rel, sha=sha) for f in found)
    return out


def format_secret_findings(findings):
    lines = []
    for f in findings[:60]:
        where = f"{f['path']}:{f['line']}" if f["line"] else f["path"]
        lines.append(f"  {where}  -  {f['kind']}" + (f"  ({f['preview']})" if f["preview"] else ""))
    if len(findings) > 60:
        lines.append(f"  ... and {len(findings) - 60} more")
    return "\n".join(lines)


def print_secret_findings(findings):
    if findings:
        print(f"\nPOSSIBLE SECRETS ({len(findings)}) - these would be published on GitHub:")
        print(format_secret_findings(findings))


# ------------------------------------------------------------- local helpers
def read_json(path, default, strict=False):
    """Missing file -> default. Damaged file -> default, or StateError when strict."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        if strict:
            raise StateError(f"{path} could not be read: {e}") from None
        return default


def _replace(src, dst):
    for attempt in range(10):           # Windows: AV scanners / indexers briefly hold files open
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9 or os.name != "nt":
                raise
            time.sleep(0.05 * (attempt + 1))


def write_json(path, obj):
    """Atomic: the old file stays intact until the new one is completely on disk."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".tmp-{secrets.token_hex(6)}{TMP_SUFFIX}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def join_rel(a, b):
    return b if not a else a + "/" + b


def blob_sha_bytes(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _is_link_like(st):
    """True for symlinks and for Windows reparse points that redirect a path (junctions etc.).

    Cloud placeholders (OneDrive) and dedup files are reparse points too, but they are
    ordinary files to the user, so only name-surrogate tags count as links.
    """
    if stat.S_ISLNK(st.st_mode):
        return True
    if (getattr(st, "st_file_attributes", 0) or 0) & _REPARSE_POINT:
        tag = getattr(st, "st_reparse_tag", None)
        if not isinstance(tag, int) or not tag:
            return True                 # can't tell: be conservative
        return bool(tag & _NAME_SURROGATE)
    return False


def _open_regular(path):
    """Open a regular file for reading without following a final symlink."""
    fd = os.open(path, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePath(f"{path} is not a regular file")
        return os.fdopen(fd, "rb"), st
    except BaseException:
        os.close(fd)
        raise


def _hash_open(f, size):
    h = hashlib.sha1()
    h.update(b"blob %d\0" % size)
    n = 0
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
        n += len(chunk)
    if n != size:
        raise LocalChanged("file changed while it was being read")
    return h.hexdigest()


def blob_sha_file(path, size=None):
    f, st = _open_regular(path)
    with f:
        return _hash_open(f, st.st_size if size is None else size)


# Every one of these that exists in the project root is honoured; a path matched by any
# of them is not synced. Each file is read on its own and none is ever written by ghsync.
IGNORE_FILES = (".ghignore", ".gitignore", ".packagerignore")


def ignore_files(root):
    """The ignore files present in the project root, in IGNORE_FILES order."""
    return [n for n in IGNORE_FILES if os.path.lexists(os.path.join(root, n))]


def _read_ignore_file(root, name):
    """Patterns from one ignore file. Fails closed: a file that exists but can't be read
    means exclusions were asked for and would be lost, so the sync must not go ahead."""
    p = os.path.join(root, name)
    try:
        f, _ = _open_regular(p)
        with f:
            text = f.read().decode("utf-8-sig")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, GhsyncError) as e:
        raise GhsyncError(f"{name} exists but can't be read ({e}). Nothing was synced, because "
                          f"the files it excludes could otherwise be uploaded. Fix or remove {name} "
                          "and try again.") from None
    pats = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            # Force-include (a .packagerignore / .gitignore feature). ghsync never
            # un-ignores: ignoring more is safe (ignored files are held, never deleted),
            # un-ignoring could upload something another tool was told to keep private.
            continue
        if line.startswith("\\!") or line.startswith("\\#"):
            line = line[1:]
        pats.append(line)
    return pats


def load_ignore(root):
    pats = list(DEFAULT_IGNORE)
    for name in IGNORE_FILES:
        pats.extend(_read_ignore_file(root, name))
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


def local_path(root, rel):
    return os.path.join(root, *rel.split("/"))


# ------------------------------------------------------------- project lock
_tls = threading.local()


def _os_lock(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class ProjectLock:
    """Exclusive, interprocess lock on one project folder.

    Uses an OS file lock on .ghsync/lock, so the lock disappears with the process that
    held it: a crash or kill never leaves a stale lock behind. Re-entrant within a
    thread. Owner details go to .ghsync/lock.owner for the "busy" message only.
    """

    def __init__(self, root):
        self.root = root
        self.key = os.path.normcase(os.path.abspath(root))

    def __enter__(self):
        held = _tls.__dict__.setdefault("held", {})
        if self.key in held:
            held[self.key][1] += 1
            return self
        d = os.path.join(self.root, STATE_DIR)
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, "lock"), os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
        try:
            _os_lock(fd)
        except OSError:
            os.close(fd)
            owner = read_json(os.path.join(d, "lock.owner"), {}) or {}
            who = ""
            if isinstance(owner, dict) and owner.get("pid"):
                who = f" (pid {owner.get('pid')} on {owner.get('host', '?')}, since {owner.get('started', '?')})"
            raise LockBusy(f"another ghsync operation is running on this folder{who}. "
                           "Wait for it to finish and try again.") from None
        held[self.key] = [fd, 1]
        with contextlib.suppress(OSError):
            write_json(os.path.join(d, "lock.owner"),
                       {"pid": os.getpid(), "host": socket.gethostname(),
                        "started": time.strftime("%Y-%m-%d %H:%M:%S"), "version": __version__})
        return self

    def __exit__(self, *exc):
        held = _tls.__dict__.get("held", {})
        ent = held.get(self.key)
        if not ent:
            return
        ent[1] -= 1
        if ent[1] == 0:
            del held[self.key]
            try:
                _os_unlock(ent[0])
            finally:
                os.close(ent[0])


@contextlib.contextmanager
def lock_if_free(root):
    """Yield True while holding the project lock, or False if someone else has it."""
    lk = ProjectLock(root)
    try:
        lk.__enter__()
    except (LockBusy, OSError):
        yield False
        return
    try:
        yield True
    finally:
        lk.__exit__(None, None, None)


# ------------------------------------------------------------- local scan
def scan_local(root):
    """Return (files, excluded).

    files:    {rel: blob_sha} for every regular file ghsync can sync.
    excluded: {rel: reason} for things it saw but will not touch (links, oversized,
              unreadable, unsupported names, its own temp files). Ignored paths are
              simply not listed.
    """
    cache_path = os.path.join(root, STATE_DIR, "cache.json")
    cache = read_json(cache_path, {})
    if not isinstance(cache, dict):
        cache = {}
    new_cache, files, excluded = {}, {}, {}
    pats = load_ignore(root)
    fresh = time.time_ns() - 2_000_000_000     # don't cache files modified in the last 2 s
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        full_dir = local_path(root, rel_dir) if rel_dir else root
        try:
            with os.scandir(full_dir) as it:
                names = [e.name for e in it]
        except OSError as e:
            excluded[rel_dir or "."] = f"folder can't be read ({e.strerror or e})"
            continue
        for name in names:
            rel = join_rel(rel_dir, name)
            full = os.path.join(full_dir, name)
            try:
                st = os.lstat(full)
            except OSError as e:
                excluded[rel] = f"can't be read ({e.strerror or e})"
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if _RUN_TEMP_RE.match(name) and stat.S_ISREG(st.st_mode) and not _is_link_like(st):
                excluded[rel] = _TEMP_REASON     # a candidate only; see cleanup_temps()
                continue
            if ignored(rel, is_dir, pats):
                continue
            if _is_link_like(st):
                excluded[rel] = "symlink or junction (not followed)"
                continue
            why = check_repo_path(rel)
            if why:
                excluded[rel] = f"unsupported name: {why}"
                continue
            if is_dir:
                stack.append(rel)
                continue
            if not stat.S_ISREG(st.st_mode):
                excluded[rel] = "not a regular file"
                continue
            if st.st_size > MAX_FILE:
                excluded[rel] = f"over 100 MB ({st.st_size / 1048576:.0f} MB, GitHub limit)"
                continue
            c = cache.get(rel)
            if (isinstance(c, list) and len(c) == 3 and c[0] == st.st_size
                    and c[1] == st.st_mtime_ns and isinstance(c[2], str)):
                sha = c[2]
            else:
                try:
                    sha = blob_sha_file(full)
                except (OSError, GhsyncError) as e:
                    excluded[rel] = f"can't be read ({getattr(e, 'strerror', None) or e})"
                    continue
            if st.st_mtime_ns < fresh:
                new_cache[rel] = [st.st_size, st.st_mtime_ns, sha]
            files[rel] = sha
    with lock_if_free(root) as mine:
        if mine:
            with contextlib.suppress(OSError):
                write_json(cache_path, new_cache)
    return files, excluded


# ------------------------------------------------- contained filesystem access
def _check_parents(root, rel, create):
    """Walk rel's parent folders below root. Each must be a real folder, never a link.

    Returns the absolute parent path. Raises FileNotFoundError if a parent is missing
    and create is False, UnsafePath if a parent is a link, junction or file.
    """
    parts = rel.split("/")[:-1]
    cur = root
    for i, name in enumerate(parts):
        cur = os.path.join(cur, name)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if not create:
                raise
            with contextlib.suppress(FileExistsError):
                os.mkdir(cur)
            st = os.lstat(cur)
        where = "/".join(parts[:i + 1])
        if _is_link_like(st):
            raise UnsafePath(f"{rel}: '{where}' is a symlink or junction; refusing to go through it")
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafePath(f"{rel}: '{where}' is a file, not a folder")
    return cur


def _check_expected(target, rel, expect):
    """The local file must still be what the scan saw: blob sha `expect`, or absent if None."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        if expect is None:
            return
        raise LocalChanged(f"{rel} was removed locally during the sync; skipped") from None
    if expect is None:
        raise LocalChanged(f"{rel} appeared locally during the sync; skipped")
    if _is_link_like(st) or not stat.S_ISREG(st.st_mode):
        raise UnsafePath(f"{rel} is no longer a regular file; skipped")
    if blob_sha_file(target) != expect:
        raise LocalChanged(f"{rel} changed locally during the sync; skipped (your edit is kept)")


def write_local(root, rel, data, expect, temp_name=None):
    """Replace root/rel with data, only if the current file still matches `expect`.

    Never writes outside root: parents are verified before the temp file is created and
    again immediately before the rename, and the rename replaces a final symlink rather
    than following it. `temp_name` comes from a journaled RunTemps; without one the temp
    gets a fresh run id that is not journaled, so a hard crash would leave it in place
    (held, never deleted) rather than risk deleting anything ghsync can't prove it made.
    """
    why = check_repo_path(rel)
    if why:
        raise UnsafePath(f"{rel}: {why}")
    try:
        parent = _check_parents(root, rel, create=False)
        _check_expected(os.path.join(parent, rel.rsplit("/", 1)[-1]), rel, expect)
    except FileNotFoundError:
        if expect is not None:
            raise LocalChanged(f"{rel} was removed locally during the sync; skipped") from None
    parent = _check_parents(root, rel, create=True)
    target = os.path.join(parent, rel.rsplit("/", 1)[-1])
    tmp = os.path.join(parent, temp_name or RunTemps(secrets.token_hex(8)).next_name())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW, 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(OSError):
            old = os.lstat(target)
            if stat.S_ISREG(old.st_mode):
                os.chmod(tmp, stat.S_IMODE(old.st_mode))
        _check_expected(target, rel, expect)
        _check_parents(root, rel, create=False)
        _replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def delete_local(root, rel, expect):
    """Delete root/rel only if it still has content `expect`; prune emptied folders."""
    why = check_repo_path(rel)
    if why:
        raise UnsafePath(f"{rel}: {why}")
    try:
        parent = _check_parents(root, rel, create=False)
    except FileNotFoundError:
        return                                   # already gone
    target = os.path.join(parent, rel.rsplit("/", 1)[-1])
    if not os.path.lexists(target):
        return
    _check_expected(target, rel, expect)
    _check_parents(root, rel, create=False)
    os.remove(target)
    stop = os.path.normcase(os.path.abspath(root))
    d = parent
    while os.path.normcase(os.path.abspath(d)) != stop:
        try:
            os.rmdir(d)  # only succeeds if empty
        except OSError:
            break
        d = os.path.dirname(d)


def read_local(root, rel):
    """Read root/rel without following links. Used for uploads and diffs."""
    why = check_repo_path(rel)
    if why:
        raise UnsafePath(f"{rel}: {why}")
    parent = _check_parents(root, rel, create=False)
    f, _ = _open_regular(os.path.join(parent, rel.rsplit("/", 1)[-1]))
    with f:
        return f.read()


class RunTemps:
    """Names for the temp files of one pull: .ghsync-<run id>-<n>.ghsync-tmp"""

    def __init__(self, run_id):
        self.run_id = run_id
        self._n = 0
        self._mu = threading.Lock()

    def next_name(self):
        with self._mu:
            self._n += 1
            return f".ghsync-{self.run_id}-{self._n}{TMP_SUFFIX}"


def _journal_path(root):
    return os.path.join(root, STATE_DIR, "journal.json")


def journal_runs(root):
    """Run ids that may have left temp files. A damaged journal reads as empty: when in
    doubt ghsync leaves files alone rather than delete something it can't prove it made."""
    j = read_json(_journal_path(root), {})
    runs = j.get("pending_runs") if isinstance(j, dict) else None
    if not isinstance(runs, list):
        return []
    return [r for r in runs if isinstance(r, str) and re.fullmatch(r"[0-9a-f]{16}", r)]


def _journal_set(root, runs):
    write_json(_journal_path(root), {"pending_runs": sorted(set(runs))})


def _remove_owned(root, rel, pattern):
    """Delete root/rel only if it is a regular file whose name matches `pattern` exactly."""
    try:
        if not pattern.match(rel.rsplit("/", 1)[-1]):
            return True
        parent = _check_parents(root, rel, create=False)
        target = os.path.join(parent, rel.rsplit("/", 1)[-1])
        st = os.lstat(target)
        if stat.S_ISREG(st.st_mode) and not _is_link_like(st):
            os.remove(target)
        return True
    except FileNotFoundError:
        return True
    except (OSError, GhsyncError):
        return False


def cleanup_temps(root, temps):
    """Remove temp files left by a run that was interrupted. Caller must hold the lock.

    Deletes a file only if its name is exactly .ghsync-<run id>-<n>.ghsync-tmp and that
    run id is in .ghsync/journal.json, i.e. ghsync recorded the run before creating it.
    Any other *.ghsync-tmp file is user data and is left alone. Inside the reserved
    .ghsync/ folder, only ghsync's own .tmp-<12 hex>.ghsync-tmp names are removed.
    """
    runs = set(journal_runs(root))
    if runs:
        failed = set()
        for rel in temps:
            m = _RUN_TEMP_RE.match(rel.rsplit("/", 1)[-1])
            if m and m.group(1) in runs and not _remove_owned(root, rel, _RUN_TEMP_RE):
                failed.add(m.group(1))
        with contextlib.suppress(OSError):
            _journal_set(root, failed)      # runs fully cleaned are forgotten
    sd = os.path.join(root, STATE_DIR)
    with contextlib.suppress(OSError):
        for name in os.listdir(sd):
            if _STATE_TEMP_RE.match(name):
                _remove_owned(root, STATE_DIR + "/" + name, _STATE_TEMP_RE)


# ------------------------------------------------------------ remote helpers
def fetch_remote(cfg):
    try:
        commit_sha = remote_head(cfg)
    except ApiError as e:
        if e.code in (404, 409):
            die(f"branch '{cfg['branch']}' not found or repo is empty. Create the repo with a "
                f"README first (ghsync init does this).")
        raise
    commit = api("GET", repo_api(cfg, f"/git/commits/{commit_sha}"))
    tree_sha = commit["tree"]["sha"]
    tree = api("GET", repo_api(cfg, f"/git/trees/{tree_sha}?recursive=1"))
    if tree.get("truncated"):
        die("remote tree is too large for a single API response (truncated); not safe to sync.")
    files, modes, unsupported = {}, {}, {}
    for e in tree["tree"]:
        p, kind, mode = e.get("path"), e.get("type"), e.get("mode", "100644")
        if not isinstance(p, str) or kind == "tree":
            continue
        if kind == "blob" and mode in ("100644", "100755"):
            files[p] = e["sha"]
            modes[p] = mode
        elif kind == "blob" and mode == "120000":
            unsupported[p] = "symlink on GitHub (not supported)"
        elif kind == "commit":
            unsupported[p] = "submodule on GitHub (not supported)"
        else:
            unsupported[p] = f"unsupported GitHub entry ({kind} {mode})"
    return {"commit": commit_sha, "tree": tree_sha, "files": files, "modes": modes,
            "unsupported": unsupported}


# --------------------------------------------------------------- planning
def _probe_absent(root, rel, dirs, listings):
    """Is rel confirmed absent locally? Returns (True, None) or (False, reason).

    Absent means: every parent below root is a real folder (or missing), and nothing
    with exactly this name exists. A name that only exists in a different letter case
    counts as absent, so case-only renames still sync.
    """
    def exact(parent_full, name):
        if parent_full not in listings:
            try:
                listings[parent_full] = {_nfc(n) for n in os.listdir(parent_full)}
            except OSError:
                listings[parent_full] = None
        names = listings[parent_full]
        return names is None or _nfc(name) in names

    parts = rel.split("/")
    prefix, parent_full = "", root
    for name in parts[:-1]:
        prefix = join_rel(prefix, name)
        if prefix not in dirs:
            full = os.path.join(parent_full, name)
            try:
                st = os.lstat(full)
                status = ("link" if _is_link_like(st) else "dir" if stat.S_ISDIR(st.st_mode) else "file")
                if status == "dir" and not exact(parent_full, name):
                    status = "missing"
            except FileNotFoundError:
                status = "missing"
            except OSError:
                status = "error"
            dirs[prefix] = status
        status = dirs[prefix]
        if status == "missing":
            return True, None
        if status != "dir":
            return False, {"link": f"inside '{prefix}', which is a symlink or junction",
                           "file": f"'{prefix}' is a file here, not a folder",
                           "error": f"folder '{prefix}' can't be read"}[status]
        parent_full = os.path.join(parent_full, name)
    full = os.path.join(parent_full, parts[-1])
    try:
        os.lstat(full)
    except FileNotFoundError:
        return True, None
    except OSError as e:
        return False, f"can't be read ({e.strerror or e})"
    if not exact(parent_full, parts[-1]):
        return True, None
    return False, "present on disk but not scanned"


def compute_held(root, base, local, excluded, remote):
    """Paths that must not be pushed, pulled or deleted this run, with the reason."""
    held = dict(remote.get("unsupported", {}))
    rfiles = remote["files"]
    for p in rfiles:
        why = check_repo_path(p)
        if why:
            held[p] = f"unsafe path on GitHub: {why}"
    if CASE_INSENSITIVE_FS:
        groups = {}
        for p in rfiles:
            groups.setdefault(_nfc(p).casefold(), []).append(p)
        for same in groups.values():
            if len(same) > 1:
                for p in same:
                    held.setdefault(p, "differs only in letter case from another GitHub file")
    pats = load_ignore(root)
    dirs, listings = {}, {}
    for p in set(base) | set(local) | set(rfiles):
        if p in held:
            continue
        why = check_repo_path(p)
        if why:
            held[p] = f"unsupported path: {why}"
        elif ignored(p, False, pats):
            held[p] = "ignored"
        elif p in excluded:
            held[p] = excluded[p]
        elif p not in local:
            ok, why = _probe_absent(root, p, dirs, listings)
            if not ok:
                held[p] = why
    return held


def load_state(root):
    path = os.path.join(root, STATE_DIR, "state.json")
    try:
        state = read_json(path, {"files": {}}, strict=True)
    except StateError as e:
        raise StateError(_damaged(path, e)) from None
    files = state.get("files") if isinstance(state, dict) else None
    if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v)
            for k, v in files.items()):
        raise StateError(_damaged(path, "unexpected content"))
    return state


def _damaged(path, why):
    return (f"the sync state file {path} is damaged ({why}). Nothing was changed. "
            "To rebuild it, delete that file and run 'status': without a previous state ghsync "
            "never deletes anything, and files that differ on the two sides show as conflicts "
            "for you to resolve.")


def _compare_status(cfg, base_sha, head_sha):
    """GitHub's view of head relative to base: ahead / behind / diverged / identical,
    or None when GitHub couldn't answer (which callers must treat as unknown, not safe)."""
    try:
        status = api("GET", repo_api(cfg, f"/compare/{base_sha}...{head_sha}")).get("status")
    except ApiError:
        return None
    return status if status in ("ahead", "behind", "diverged", "identical") else None


# A head GitHub confirms is the last-synced commit, a descendant of it, or a rewritten
# history (force-push) is acted on. "behind" or no answer never is (GHS-011: fail closed).
_SAFE_RELATIONS = ("ahead", "identical", "diverged")


def fetch_remote_checked(cfg, last_commit, attempts=6):
    """fetch_remote(), but only act on a branch head whose relation to the last sync is known.

    Right after the branch moves, GitHub can briefly answer with the previous commit.
    Acting on that would look like "GitHub went back to the old version": a two-way
    sync would pull old content over the folder. So when the head differs from the
    commit recorded at the last sync, GitHub's compare API must confirm it is ahead
    (normal progress) or diverged (a force-push). If it is behind, or GitHub can't
    answer the comparison, ghsync waits and re-reads, and refuses if that persists.
    """
    status = None
    for attempt in range(attempts):
        remote = fetch_remote(cfg)
        if not last_commit or remote["commit"] == last_commit:
            return remote
        status = _compare_status(cfg, last_commit, remote["commit"])
        if status in _SAFE_RELATIONS:
            return remote
        if attempt < attempts - 1:
            time.sleep(min(1 + attempt, 4))
    if status == "behind":
        why = (f"GitHub is showing an older version of the branch ({remote['commit'][:7]}) than "
               f"this folder last synced to ({last_commit[:7]}). This is usually GitHub catching "
               "up after a recent push: try again in a minute.")
    else:
        why = (f"ghsync couldn't confirm how GitHub's branch ({remote['commit'][:7]}) relates to "
               f"the commit this folder last synced to ({last_commit[:7]}): GitHub's compare "
               "service didn't answer, or the two commits share no history. Try again in a minute.")
    raise GhsyncError(
        why + " Nothing was changed. If the branch was rolled back or replaced on GitHub on "
        "purpose (or this folder was linked to a different repo), delete .ghsync/state.json and "
        "sync again: with no previous state nothing is deleted, and differing files are listed "
        "for you to decide.")


def gather(root, cfg):
    """Snapshot both sides. Returns a dict: base, local, remote, held, temps."""
    state = load_state(root)
    base = state.get("files", {})
    local, excluded = scan_local(root)
    last = state.get("commit") if isinstance(state.get("commit"), str) else None
    remote = fetch_remote_checked(cfg, last)
    held = compute_held(root, base, local, excluded, remote)
    temps = [p for p, why in excluded.items() if why == _TEMP_REASON]
    return {"base": base, "local": local, "remote": remote, "held": held, "temps": temps,
            "excluded": excluded}


def classify(base, local, remote, held=()):
    push, pull, conflict, same = {}, {}, {}, []
    for p in (set(base) | set(local) | set(remote)) - set(held):
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


def plan_actions(snap, mode, only=None, resolve=None, prefer=None, protect=False):
    """The exact push/pull sets a run in `mode` would execute for this snapshot.

    protect=True (Protect local): the pull set is always empty and every conflict is
    pushed as the local version (in push and sync mode). In sync mode GitHub is also made
    to mirror the folder: everything GitHub changed since the last sync is pushed as the
    local version (or deleted on GitHub if it isn't local).
    Held paths are never in any set, so ignored or unsafe GitHub files are left alone.
    """
    push, pull, conflict, same = classify(snap["base"], snap["local"], snap["remote"]["files"],
                                          snap["held"])
    if only is not None:
        push = {p: v for p, v in push.items() if p in only}
        pull = {p: v for p, v in pull.items() if p in only}
        conflict = {p: v for p, v in conflict.items() if p in only}
    resolve = resolve or {}
    for p, (l, r) in list(conflict.items()):
        side = resolve.get(p) or prefer
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
    if protect:
        local = snap["local"]
        # The folder is the source of truth, so a conflict is resolved to the local
        # version by PUSH and SYNC alike. Only SYNC also reverts files that changed on
        # GitHub alone and removes files that exist only there.
        mirrored = list(pull) + list(conflict) if mode == "sync" else list(conflict)
        for p in mirrored:
            push[p] = local.get(p)
        conflict = {}
        pull = {}
    return push, pull, conflict, same


def drift(snap):
    """Paths GitHub changed since the last sync (what a two-way sync would pull or flag)."""
    _, pull, conflict, _ = classify(snap["base"], snap["local"], snap["remote"]["files"], snap["held"])
    return set(pull) | set(conflict)


def plan_digest(mode, push, pull):
    """Fingerprint of exactly what will be executed. The GUI confirms against this."""
    payload = json.dumps({"mode": mode, "push": sorted(push.items()), "pull": sorted(pull.items())},
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def next_base(snap, done_push, done_pull):
    """New 'last agreed' state. Anything held, skipped or unresolved keeps its old base."""
    base, local, rfiles, held = snap["base"], snap["local"], snap["remote"]["files"], snap["held"]
    final = {}
    for p in set(base) | set(local) | set(rfiles):
        if p in held:
            v = base.get(p)
        elif p in done_push:
            v = done_push[p]
        elif p in done_pull:
            v = done_pull[p]
        elif local.get(p) == rfiles.get(p):
            v = local.get(p)
        else:
            v = base.get(p)
        if v is not None:
            final[p] = v
    return final


# ---------------------------------------------------------------- transfers
def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def do_push(root, cfg, items, remote, message):
    """items: {path: local_sha or None}. One commit, all or nothing. Returns new commit sha."""
    have = set(remote["files"].values())
    uploads = {sha: p for p, sha in sorted(items.items()) if sha and sha not in have}

    def up(item):
        sha, p = item
        data = read_local(root, p)
        if blob_sha_bytes(data) != sha:
            raise LocalChanged(f"{p} changed while syncing; nothing was pushed. Run again.")
        res = api("POST", repo_api(cfg, "/git/blobs"),
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

    entries = [{"path": p, "mode": remote["modes"].get(p, "100644"), "type": "blob", "sha": sha}
               for p, sha in sorted(items.items())]
    tree_sha = remote["tree"]
    for part in chunks(entries, 500):
        tree_sha = api("POST", repo_api(cfg, "/git/trees"), {"base_tree": tree_sha, "tree": part})["sha"]
    commit = api("POST", repo_api(cfg, "/git/commits"),
                 {"message": message, "tree": tree_sha, "parents": [remote["commit"]]})
    try:
        api("PATCH", ref_api(cfg, single=False), {"sha": commit["sha"]})
    except ApiError as e:
        if e.code == 422:
            # Ambiguous: a retried PATCH may have already landed. Check before giving up.
            with contextlib.suppress(ApiError):
                if remote_head(cfg) == commit["sha"]:
                    return commit["sha"]
            raise GhsyncError("GitHub changed while pushing (not a fast-forward). Nothing was "
                              "overwritten. Run the command again.") from None
        raise
    return commit["sha"]


def _write_backup(bdir, rel, data):
    target = os.path.join(bdir, *rel.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _backup_existing(root, bdir, rel, expect, counter):
    """Copy the local file about to be replaced/deleted into the backup folder.

    Raises (so the file is skipped, not overwritten) if it can't be read, no longer has
    the content the plan saw, or the backup can't be written.
    """
    if expect is None:
        return                                  # nothing local to lose
    try:
        data = read_local(root, rel)
    except FileNotFoundError:
        return
    if blob_sha_bytes(data) != expect:
        raise LocalChanged(f"{rel} changed locally during the sync; skipped (your edit is kept)")
    try:
        _write_backup(bdir, rel, data)
    except OSError as e:
        raise GhsyncError(f"couldn't back up {rel} ({e}); skipped, nothing was overwritten") from None
    counter.append(rel)


def do_pull(root, cfg, items, local, temps=None, allow_overwrite=False):
    """Apply remote changes one file at a time.

    Returns (done, failed): done = {path: sha or None} actually applied, failed =
    {path: reason}. A file that changed locally since the scan is skipped, never
    overwritten. Every existing local file is backed up to .ghsync/backup/<time>/
    before it is replaced or deleted. Deletions run first so case-only renames work
    on Windows. Refuses outright while Protect local is on unless allow_overwrite.
    """
    if protect_enabled(cfg) and not allow_overwrite:
        raise ProtectedLocal(_PROTECT_HELP)
    done, failed = {}, {}
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    bdir = os.path.join(root, STATE_DIR, "backup", stamp)
    backed = []
    for p in sorted(p for p, v in items.items() if v is None):
        try:
            _backup_existing(root, bdir, p, local.get(p), backed)
            delete_local(root, p, expect=local.get(p))
            done[p] = None
        except (GhsyncError, OSError) as e:
            failed[p] = str(e)

    fetches = sorted((p, sha) for p, sha in items.items() if sha)

    def down(item):
        p, sha = item
        try:
            data = api("GET", repo_api(cfg, f"/git/blobs/{sha}"), raw=True)
            if blob_sha_bytes(data) != sha:
                raise GhsyncError(f"download of {p} failed hash check")
            _backup_existing(root, bdir, p, local.get(p), backed)
            write_local(root, p, data, expect=local.get(p),
                        temp_name=temps.next_name() if temps else None)
            return p, sha, None
        except Exception as e:  # noqa: BLE001 - one bad file must not abort the rest
            return p, sha, (str(e) if isinstance(e, (GhsyncError, OSError))
                            else f"{type(e).__name__}: {e}")

    if fetches:
        print(f"  downloading {len(fetches)} file(s)...")
        n = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            for p, sha, err in ex.map(down, fetches):
                n += 1
                if err:
                    failed[p] = err
                else:
                    done[p] = sha
                if n % 25 == 0 or n == len(fetches):
                    print(f"    {n}/{len(fetches)}")
    if backed:
        print(f"  backed up {len(backed)} replaced/deleted local file(s) to "
              f"{STATE_DIR}/backup/{stamp}/")
    return done, failed


# ------------------------------------------------------------------ commands
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/+@-]{1,250}$")


def validate_cfg(cfg):
    if not isinstance(cfg, dict):
        raise StateError("config.json is not a JSON object")
    owner, repo, branch = cfg.get("owner"), cfg.get("repo"), cfg.get("branch")
    for label, v in (("owner", owner), ("repo", repo)):
        if not isinstance(v, str) or not _OWNER_RE.match(v) or v in (".", ".."):
            raise StateError(f"config.json has an invalid {label}: {v!r}")
    if (not isinstance(branch, str) or not _BRANCH_RE.match(branch) or ".." in branch
            or "//" in branch or branch.startswith("/") or branch.endswith(("/", ".lock"))):
        raise StateError(f"config.json has an invalid branch: {branch!r}")
    if "protect_local" in cfg and not isinstance(cfg["protect_local"], bool):
        raise StateError(f"config.json has an invalid protect_local: {cfg['protect_local']!r} "
                         "(must be true or false)")
    return cfg


def protect_enabled(cfg):
    """Protect local is on unless the config explicitly says false."""
    return cfg.get("protect_local", True) is not False


def load_cfg(args):
    root = os.path.abspath(args.dir or os.getcwd())
    cfg = read_json(os.path.join(root, STATE_DIR, "config.json"), None, strict=True)
    if not cfg:
        die("this folder is not linked. Use 'init <name>' or 'link owner/repo' first.")
    return root, validate_cfg(cfg)


def show_plan(snap, push, pull, conflict, failed=None, mirror=()):
    local, remote = snap["local"], snap["remote"]
    mirror = set(mirror)

    def block(title, lines):
        if not lines:
            return
        print(f"\n{title} ({len(lines)})")
        for ln in lines[:60]:
            print("  " + ln)
        if len(lines) > 60:
            print(f"  ... and {len(lines) - 60} more")

    def tag(new_val, old_val):
        return "A" if old_val is None else ("D" if new_val is None else "M")

    block("PUSH  (local -> GitHub)",
          [f"{tag(v, remote['files'].get(p))} {p}" + ("   (reverts GitHub-side change)" if p in mirror else "")
           for p, v in sorted(push.items())])
    block("PULL  (GitHub -> local)",
          [f"{tag(v, local.get(p))} {p}" for p, v in sorted(pull.items())])
    block("CONFLICT  (changed on both sides)", sorted(conflict))
    shown = held_to_show(snap)
    plain = [p for p, why in shown.items() if why == "ignored"]
    block("HELD  (not synced, never deleted)",
          [f"{p}  -  {why}" for p, why in sorted(shown.items()) if why != "ignored"])
    if plain:
        print(f"\n{len(plain)} ignored file(s) differ between the folder and GitHub and are left alone.")
    if failed:
        block("SKIPPED  (left as they were; will be retried)", [f"{p}  -  {why}" for p, why in sorted(failed.items())])
    if not (push or pull or conflict):
        print("\nIn sync. Nothing to do.")


def held_to_show(snap):
    """Held paths whose two sides actually differ (identical ones aren't worth mentioning)."""
    local, rfiles = snap["local"], snap["remote"]["files"]
    out = {p: why for p, why in snap["held"].items()
           if why != _TEMP_REASON and (local.get(p) != rfiles.get(p) or p in snap["remote"].get("unsupported", {}))}
    for p, why in snap.get("excluded", {}).items():     # local-only things that won't be uploaded
        if why != _TEMP_REASON and p not in snap["held"]:
            out[p] = why
    return out


def cmd_status(args):
    root, cfg = load_cfg(args)
    snap = gather(root, cfg)
    protect = protect_enabled(cfg)
    push, pull, conflict, _ = plan_actions(snap, "sync", protect=protect)
    print(f"{cfg['owner']}/{cfg['repo']} @ {cfg['branch']}  (remote head {snap['remote']['commit'][:7]})")
    print("Ignore rules: " + (", ".join(ignore_files(root)) or "built-in defaults only"))
    print("Protect local: " + ("ON - sync makes GitHub mirror this folder; nothing in the folder is "
                               "replaced or deleted" if protect else "OFF - sync is two-way"))
    show_plan(snap, push, pull, conflict, mirror=drift(snap) if protect else ())
    print_secret_findings(find_secrets(root, {p: v for p, v in push.items() if v}))
    if conflict:
        print("\nResolve with: diff <path>, then sync --prefer local|remote")


def cmd_diff(args):
    root, cfg = load_cfg(args)
    snap = gather(root, cfg)
    local, remote = snap["local"], snap["remote"]
    push, pull, conflict, _ = plan_actions(snap, "sync")
    targets = [p.replace("\\", "/") for p in args.paths] or sorted(list(push) + list(pull) + list(conflict))
    if not targets:
        print("No differences.")
        return
    for p in targets:
        if p in snap["held"] or check_repo_path(p):
            print(f"\n=== {p} ===\nnot synced: {snap['held'].get(p) or check_repo_path(p)}")
            continue
        lsha, rsha = local.get(p), remote["files"].get(p)
        if lsha == rsha:
            continue
        a = b = b""
        if rsha:
            a = api("GET", repo_api(cfg, f"/git/blobs/{rsha}"), raw=True)
        if lsha:
            b = read_local(root, p)
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
    with ProjectLock(root):
        return _run_sync_locked(args, mode, root, cfg)


def _run_sync_locked(args, mode, root, cfg):
    state_path = os.path.join(root, STATE_DIR, "state.json")
    if not args.dry_run:
        destination_guard(root)        # under the project lock, before any GitHub request
    allow = bool(getattr(args, "allow_local_overwrite", False))
    protect = protect_enabled(cfg) and not allow
    resolve = getattr(args, "resolve", None) or {}
    if protect and (mode == "pull" or args.prefer == "remote" or "remote" in resolve.values()):
        raise ProtectedLocal(_PROTECT_HELP)
    snap = gather(root, cfg)
    cleanup_temps(root, snap["temps"])
    remote = snap["remote"]
    push, pull, conflict, _ = plan_actions(snap, mode, getattr(args, "only", None),
                                           resolve, args.prefer, protect=protect)
    if protect and pull:          # second, independent guard: planning must never get here
        raise ProtectedLocal("internal error: a local overwrite was planned while Protect local "
                             "is on. Nothing was done.")

    print(f"{cfg['owner']}/{cfg['repo']} @ {cfg['branch']}"
          + ("   [Protect local: GitHub mirrors this folder]" if protect else ""))
    show_plan(snap, push, pull, conflict, mirror=drift(snap) if protect else ())

    expect = getattr(args, "expect", None)
    if expect is not None and plan_digest(mode, push, pull) != expect:
        raise PlanChanged("the folder or GitHub changed after you confirmed, so the plan is no "
                          "longer the one you approved. Nothing was done. Review the refreshed "
                          "list and try again.")
    findings = find_secrets(root, {p: v for p, v in push.items() if v})
    print_secret_findings(findings)
    if args.dry_run:
        return
    unchecked = [f for f in findings if f.get("unchecked")]
    if unchecked:                 # nothing was inspected, so no approval can cover it
        raise SecretsUnchecked(unchecked)
    allow_secrets = getattr(args, "allow_secrets", None)
    if findings and allow_secrets is not True:
        approved = set(allow_secrets or ())
        pending = [f for f in findings if (f["path"], f["sha"]) not in approved]
        if pending:               # checked before anything happens, pull included
            raise SecretsFound(pending)
    if not (push or pull):
        final = next_base(snap, {}, {})
        if final != snap["base"]:
            write_json(state_path, {"commit": remote["commit"], "files": final})
        return

    deletes_local = [p for p, v in pull.items() if v is None]
    deletes_remote = [p for p, v in push.items() if v is None]
    if (deletes_local or deletes_remote) and not args.yes:
        print(f"\nThis will delete {len(deletes_local)} local and {len(deletes_remote)} remote file(s).")
        if input("Continue? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return

    new_commit = remote["commit"]
    done_pull, failed = {}, {}
    if pull:
        print("\nPulling...")
        temps = RunTemps(secrets.token_hex(8))
        _journal_set(root, journal_runs(root) + [temps.run_id])   # before any temp exists
        done_pull, failed = do_pull(root, cfg, pull, snap["local"], temps, allow_overwrite=not protect)
        # Every temp of this run was renamed into place or removed by write_local.
        _journal_set(root, [r for r in journal_runs(root) if r != temps.run_id])
        # Record what the pull achieved before pushing, so an interrupted push loses nothing.
        write_json(state_path, {"commit": remote["commit"], "files": next_base(snap, {}, done_pull)})
    done_push = {}
    if push:
        print("\nPushing...")
        msg = args.message or f"ghsync: {len(push)} file(s) changed ({time.strftime('%Y-%m-%d %H:%M')})"
        new_commit = do_push(root, cfg, push, remote, msg)
        done_push = push

    write_json(state_path, {"commit": new_commit, "files": next_base(snap, done_push, done_pull)})
    if failed:
        print(f"\nSKIPPED ({len(failed)}) - left as they were; they'll be compared again next time:")
        for p, why in sorted(failed.items())[:60]:
            print(f"  {p}  -  {why}")
    print(f"\nDone. Remote head {new_commit[:7]}."
          + (f"  {len(conflict)} conflict(s) left untouched." if conflict else "")
          + (f"  {len(failed)} file(s) skipped." if failed else ""))
    return {"pushed": done_push, "pulled": done_pull, "skipped": failed, "conflicts": conflict}


def clean_targets(snap):
    """Files on GitHub that this folder's ignore rules exclude (they'd otherwise stay forever)."""
    rfiles = snap["remote"]["files"]
    return sorted(p for p, why in snap["held"].items() if why == "ignored" and p in rfiles)


def clean_digest(cfg, remote_files, targets):
    """Fingerprint of a clean-up: destination, plus each target AND the GitHub content it
    has now. A target replaced on GitHub after review changes the digest (GHS-018)."""
    payload = json.dumps({"mode": "clean",
                          "dest": [cfg["owner"].lower(), cfg["repo"].lower(), cfg["branch"]],
                          "targets": sorted((p, remote_files.get(p)) for p in targets)},
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def run_clean(args):
    """Remove ignored files from GitHub in one commit. Local copies are never touched."""
    root, cfg = load_cfg(args)
    with ProjectLock(root):
        if not args.dry_run:
            destination_guard(root)
        snap = gather(root, cfg)
        targets = clean_targets(snap)
        print(f"{cfg['owner']}/{cfg['repo']} @ {cfg['branch']}")
        if not targets:
            print("\nNothing to clean: no ignored files are on GitHub.")
            return {"removed": []}
        print(f"\nIGNORED BUT STILL ON GITHUB ({len(targets)}) - would be removed from GitHub; "
              "copies in this folder are kept:")
        for p in targets[:200]:
            print(f"  {p}")
        if len(targets) > 200:
            print(f"  ... and {len(targets) - 200} more")
        expect = getattr(args, "expect", None)
        if expect is not None and clean_digest(cfg, snap["remote"]["files"], targets) != expect:
            raise PlanChanged("the ignored files on GitHub (or their contents) changed after you "
                              "confirmed. "
                              "Nothing was removed. Review the refreshed list and try again.")
        if args.dry_run:
            return {"removed": [], "would_remove": targets}
        if not args.yes:
            if input(f"Remove these {len(targets)} file(s) from GitHub? [y/N] ").strip().lower() != "y":
                print("Cancelled.")
                return {"removed": []}
        msg = getattr(args, "message", None) or f"ghsync: remove {len(targets)} ignored file(s) from GitHub"
        print("\nRemoving...")
        new_commit = do_push(root, cfg, {p: None for p in targets}, snap["remote"], msg)
        state = load_state(root)
        files = {k: v for k, v in state.get("files", {}).items() if k not in set(targets)}
        write_json(os.path.join(root, STATE_DIR, "state.json"), {"commit": new_commit, "files": files})
        print(f"\nDone. Removed {len(targets)} file(s) from GitHub (still in its history). "
              f"Remote head {new_commit[:7]}.")
        return {"removed": targets, "commit": new_commit}


def _save_token(tok):
    d = os.path.dirname(TOKEN_FILE)
    tmp = os.path.join(d, f".ghsync_token.{secrets.token_hex(4)}{TMP_SUFFIX}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
        _replace(tmp, TOKEN_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    with contextlib.suppress(OSError):
        os.chmod(TOKEN_FILE, 0o600)


def cmd_login(args):
    tok = getpass.getpass("Paste GitHub token (fine-grained: Contents read/write): ").strip()
    if not tok:
        die("empty token")
    os.environ["GITHUB_TOKEN"] = tok
    me = api("GET", "/user")
    _save_token(tok)
    print(f"Logged in as {me['login']}. Token saved to {TOKEN_FILE}")


def save_cfg(root, owner, repo, branch, protect_local=True):
    cfg = validate_cfg({"owner": owner, "repo": repo, "branch": branch, "protect_local": protect_local})
    with ProjectLock(root):
        write_json(os.path.join(root, STATE_DIR, "config.json"), cfg)


def set_protect(root, on):
    """Turn Protect local on or off for a linked folder."""
    with ProjectLock(root):
        path = os.path.join(root, STATE_DIR, "config.json")
        cfg = validate_cfg(read_json(path, None, strict=True))
        cfg["protect_local"] = bool(on)
        write_json(path, cfg)
    return cfg


def cmd_protect(args):
    root, cfg = load_cfg(args)
    if args.state:
        cfg = set_protect(root, args.state == "on")
    on = protect_enabled(cfg)
    print(f"Protect local: {'ON' if on else 'OFF'}")
    print("  sync makes GitHub mirror this folder; nothing in the folder is replaced or deleted.\n"
          "  Pulling needs --allow-local-overwrite (files are backed up to .ghsync/backup/ first)."
          if on else
          "  sync is two-way: changes made on GitHub are pulled into this folder\n"
          "  (every replaced or deleted local file is backed up to .ghsync/backup/ first).")


def cmd_link(args):
    root = os.path.abspath(args.dir or os.getcwd())
    if "/" not in args.repo:
        die("use owner/repo")
    owner, repo = args.repo.strip().split("/", 1)
    validate_cfg({"owner": owner, "repo": repo, "branch": "main"})
    nesting_guard(root)                               # no network needed: check first
    info = api("GET", repo_api({"owner": owner, "repo": repo}))
    branch = args.branch or info["default_branch"]
    destination_guard(root, owner, repo, branch)      # before anything is written
    save_cfg(root, owner, repo, branch)
    print(f"Linked {root} -> {owner}/{repo} ({branch}).")
    print("Run 'status' to compare. Files that exist on both sides with different content show as CONFLICT.")


def cmd_init(args):
    root = os.path.abspath(args.dir or os.getcwd())
    body = {"name": args.name, "private": not args.public, "auto_init": True,
            "description": args.description or ""}
    path = f"/orgs/{urllib.parse.quote(args.org, safe='')}/repos" if args.org else "/user/repos"
    nesting_guard(root)                               # before creating anything on GitHub
    try:
        info = api("POST", path, body)
    except ApiError as e:
        die(f"could not create repo: {e}")
    destination_guard(root, info["owner"]["login"], info["name"], info["default_branch"])
    save_cfg(root, info["owner"]["login"], info["name"], info["default_branch"])
    print(f"Created {info['full_name']} ({'public' if args.public else 'private'}).")
    args.message, args.dry_run, args.yes, args.prefer = args.message or "Initial upload via ghsync", False, True, None
    run_sync(args, "sync")


def main():
    ap = argparse.ArgumentParser(description="Sync a folder with GitHub without git.")
    ap.add_argument("-C", dest="dir", help="project folder (default: current folder)")
    ap.add_argument("--version", action="version", version="ghsync " + __version__)
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
    p = sub.add_parser("clean", help="remove files your ignore rules exclude from GitHub")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("-m", "--message")
    p = sub.add_parser("protect")
    p.add_argument("state", nargs="?", choices=["on", "off"])
    p = sub.add_parser("diff")
    p.add_argument("paths", nargs="*")
    for name in ("push", "pull", "sync"):
        p = sub.add_parser(name)
        p.add_argument("-m", "--message")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--yes", action="store_true")
        p.add_argument("--prefer", choices=["local", "remote"])
        p.add_argument("--allow-secrets", action="store_true",
                       help="push files flagged as possible secrets, this once")
        p.add_argument("--allow-local-overwrite", action="store_true",
                       help="one-time permission to replace/delete local files while Protect local is on")

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
        elif args.cmd == "protect":
            cmd_protect(args)
        elif args.cmd == "clean":
            run_clean(args)
        elif args.cmd == "diff":
            cmd_diff(args)
        else:
            run_sync(args, args.cmd)
    except (ApiError, GhsyncError) as e:
        die(str(e))
    except KeyboardInterrupt:
        die("interrupted")


if __name__ == "__main__":
    main()
