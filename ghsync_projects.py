# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
ghsync project registry (0.4.0) - which project folders the GUI manages.

The registry only *lists* projects. Each project keeps everything that governs its
sync (config, last-synced state, lock, ignore rules, journal) in its own .ghsync/
folder, exactly as in 0.3.x, and every sync still goes through the unchanged engine
in ghsync.py. This module adds the cross-project safety rules:

  * Identity is the folder's real location (symlinks and Windows junctions resolved,
    case-insensitive on Windows), so one folder can't be registered twice under two
    names.
  * Project folders may not be nested inside one another: two locks would then guard
    different roots while one sync touched the other project's files.
  * Two projects may not target the same GitHub repo and branch: with Protect local on,
    two folders mirroring one branch would keep undoing each other's work.

Qt-free. Stored as JSON in %APPDATA%\\ghsync\\projects.json (Windows) or
~/.config/ghsync/projects.json; GHSYNC_HOME overrides the folder.
"""
import contextlib
import os
import secrets
import time

import ghsync

REGISTRY_VERSION = 1


class RegistryError(ghsync.GhsyncError):
    pass


# ------------------------------------------------------------------ storage
def registry_dir():
    env = os.environ.get("GHSYNC_HOME")
    if env:
        return env
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ghsync")


def registry_path():
    return os.path.join(registry_dir(), "projects.json")


@contextlib.contextmanager
def _registry_lock(timeout=5.0):
    """Serialise registry updates across GHSYNC windows (OS lock, released on exit/crash)."""
    d = registry_dir()
    os.makedirs(d, exist_ok=True)
    fd = os.open(os.path.join(d, "registry.lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        end = time.time() + timeout
        while True:
            try:
                ghsync._os_lock(fd)
                break
            except OSError:
                if time.time() > end:
                    raise RegistryError("the project list is being changed by another GHSYNC window; "
                                        "try again") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            ghsync._os_unlock(fd)
    finally:
        os.close(fd)


def load_registry():
    """Registered projects, in the order they were added. Missing file = none.
    A damaged file raises RegistryError and is never overwritten."""
    path = registry_path()
    try:
        data = ghsync.read_json(path, {"version": REGISTRY_VERSION, "projects": []}, strict=True)
    except ghsync.StateError as e:
        raise RegistryError(f"the project list {path} can't be read ({e}). It was left untouched; "
                            "fix or remove it to continue.") from None
    def bad(why):
        return RegistryError(f"the project list {path} is not valid ({why}). It was left untouched; "
                             "fix or remove it to continue.")

    if not isinstance(data, dict):
        raise bad("not a JSON object")
    if data.get("version") != REGISTRY_VERSION:
        raise bad(f"version {data.get('version')!r}; this GHSYNC reads version {REGISTRY_VERSION}")
    projects = data.get("projects")
    if not isinstance(projects, list):
        raise bad("no project list")
    entries, ids, keys = [], set(), []
    for p in projects:
        if not (isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"]
                and isinstance(p.get("root"), str) and isinstance(p.get("name", ""), str)):
            raise bad("an entry is malformed")
        if p["id"] in ids:
            raise bad(f"duplicate project id {p['id']!r}")
        if not os.path.isabs(p["root"]):
            raise bad(f"project folder {p['root']!r} is not an absolute path")
        key = canonical(p["root"])[1]
        for other in keys:
            if key == other or _inside(key, other) or _inside(other, key):
                raise bad(f"{p['root']!r} is the same as, inside, or contains another listed project")
        ids.add(p["id"])
        keys.append(key)
        entries.append({"id": p["id"], "root": p["root"], "name": p.get("name") or os.path.basename(p["root"])})
    return entries


def _save(entries):
    ghsync.write_json(registry_path(), {"version": REGISTRY_VERSION, "projects": entries})


# ------------------------------------------------------------------ identity
def canonical(path):
    """(real path, comparison key). Resolves symlinks and junctions; case-folded on Windows."""
    real = os.path.realpath(os.path.abspath(path))
    return real, os.path.normcase(real)


def _inside(key, parent_key):
    p = parent_key.rstrip("\\/")
    return key.startswith(p + os.sep) or (os.altsep and key.startswith(p + os.altsep))


def destination(root):
    """(owner, repo, branch) a folder syncs to, compared case-insensitively; None if not linked."""
    cfg = ghsync.read_json(os.path.join(root, ghsync.STATE_DIR, "config.json"), None)
    try:
        ghsync.validate_cfg(cfg)
    except ghsync.GhsyncError:
        return None
    return cfg["owner"].casefold(), cfg["repo"].casefold(), cfg["branch"]


def _dest_label(dest):
    return f"{dest[0]}/{dest[1]} · {dest[2]}"


def _check_root(entries, real, key, skip_id=None):
    if not os.path.isdir(real):
        raise RegistryError(f"{real} is not a folder")
    for e in entries:
        if e["id"] == skip_id:
            continue
        ekey = canonical(e["root"])[1]
        if ekey == key:
            raise RegistryError(f"this folder is already in the list as '{e['name']}' ({e['root']})")
        if _inside(key, ekey):
            raise RegistryError(f"this folder is inside another project, '{e['name']}' ({e['root']}). "
                                "Projects can't be nested.")
        if _inside(ekey, key):
            raise RegistryError(f"this folder contains another project, '{e['name']}' ({e['root']}). "
                                "Projects can't be nested.")


def _check_dest(entries, key, dest):
    if not dest:
        return
    for e in entries:
        if canonical(e["root"])[1] == key:
            continue
        if destination(e["root"]) == dest:
            raise RegistryError(f"'{e['name']}' ({e['root']}) already syncs to {_dest_label(dest)}. "
                                "Two folders mirroring one branch would keep undoing each other's work; "
                                "link one of them to a different repo or branch.")


# ------------------------------------------------------------------ changes
def add_project(root, name=None):
    real, key = canonical(root)
    with _registry_lock():
        entries = load_registry()
        _check_root(entries, real, key)
        _check_dest(entries, key, destination(real))
        entry = {"id": secrets.token_hex(8), "root": real,
                 "name": (name or os.path.basename(real.rstrip("\\/")) or real)}
        entries.append(entry)
        _save(entries)
    return entry


def remove_project(project_id):
    """Forget a project. Its folder and .ghsync/ are not touched."""
    with _registry_lock():
        entries = load_registry()
        _save([e for e in entries if e["id"] != project_id])


def find(entries, root):
    key = canonical(root)[1]
    return next((e for e in entries if canonical(e["root"])[1] == key), None)


def check_destination(root, owner, repo, branch):
    """Raise RegistryError if another registered project already syncs to this destination."""
    _check_dest(load_registry(), canonical(root)[1], (owner.casefold(), repo.casefold(), branch))


def clashes(entries):
    """{project id: [ids of other projects with the same destination]}."""
    groups = {}
    for e in entries:
        d = destination(e["root"])
        if d:
            groups.setdefault(d, []).append(e["id"])
    out = {}
    for ids in groups.values():
        if len(ids) > 1:
            for i in ids:
                out[i] = [j for j in ids if j != i]
    return out


def overlap_message(root):
    """Why `root` may not be synced because it is nested inside, or contains, a listed
    project (GHS-016), or None. A listed project itself is fine. Fails closed if the
    registry can't be read."""
    try:
        entries = load_registry()
    except RegistryError as e:
        return str(e)
    key = canonical(root)[1]
    for e in entries:
        ekey = canonical(e["root"])[1]
        if ekey == key:
            continue
        if _inside(key, ekey):
            return (f"this folder is inside the listed project '{e['name']}' ({e['root']}). Projects "
                    "can't be nested: syncing it separately would put the same files under two "
                    "projects. Sync it as part of that project, or move it out.")
        if _inside(ekey, key):
            return (f"this folder contains the listed project '{e['name']}' ({e['root']}). Projects "
                    "can't be nested: syncing it would put that project's files under two projects.")
    return None


def clash_message(root):
    """Why a mutating operation on `root` must be refused, or None. Fails closed if the
    registry can't be read."""
    try:
        entries = load_registry()
    except RegistryError as e:
        return str(e)
    dest = destination(root)
    if not dest:
        return None
    key = canonical(root)[1]
    for e in entries:
        if canonical(e["root"])[1] != key and destination(e["root"]) == dest:
            return (f"'{e['name']}' ({e['root']}) also syncs to {_dest_label(dest)}. With two folders "
                    "on one branch each sync would undo the other's work, so syncing is blocked until "
                    "one of them is linked elsewhere or removed from the list.")
    return None


# ------------------------------------------------------------------ status
def _have_token():
    return bool(os.environ.get("GITHUB_TOKEN")) or os.path.exists(ghsync.TOKEN_FILE)


def project_status(root):
    """Cheap, read-only status for the dashboard: one local scan and one GitHub request.

    state: unlinked | new | clean | local | remote | both | error
    Never raises and never prints; problems come back as state 'error' with a message.
    """
    st = {"state": "unlinked", "repo": "", "protect": None, "files": 0, "local_changes": 0,
          "remote_moved": False, "error": "", "checked": time.time()}
    try:
        if not os.path.isdir(root):
            raise RegistryError("folder not found")
        cfg = ghsync.read_json(os.path.join(root, ghsync.STATE_DIR, "config.json"), None, strict=True)
        if not cfg:
            return st
        ghsync.validate_cfg(cfg)
        st["repo"] = f"{cfg['owner']}/{cfg['repo']} · {cfg['branch']}"
        st["protect"] = ghsync.protect_enabled(cfg)
        state = ghsync.load_state(root)
        base = state.get("files", {})
        local, excluded = ghsync.scan_local(root)
        st["files"] = len(local)
        pats = ghsync.load_ignore(root)
        changes = 0
        for p in set(local) | set(base):
            if local.get(p) == base.get(p):
                continue
            if p not in local and (p in excluded or ghsync.check_repo_path(p)
                                   or ghsync.ignored(p, False, pats)):
                continue                      # held, not deleted: not a change
            changes += 1
        st["local_changes"] = changes
        if not _have_token():
            raise RegistryError("not signed in to GitHub")
        head = ghsync.remote_head(cfg)
        last = state.get("commit")
        st["remote_moved"] = head != last
        if not last:
            st["state"] = "new"
        elif changes and st["remote_moved"]:
            st["state"] = "both"
        elif changes:
            st["state"] = "local"
        elif st["remote_moved"]:
            st["state"] = "remote"
        else:
            st["state"] = "clean"
    except SystemExit:
        st["state"], st["error"] = "error", "GitHub request failed"
    except ghsync.ApiError as e:
        st["state"] = "error"
        st["error"] = ("repo or branch not found on GitHub" if e.code in (404, 409)
                       else f"GitHub error {e.code}")
    except (ghsync.GhsyncError, OSError, ValueError) as e:
        st["state"], st["error"] = "error", str(e)
    return st
