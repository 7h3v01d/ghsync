# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""
In-memory stand-in for the slice of the GitHub REST API that ghsync uses.

It models the Git Data endpoints (refs, commits, recursive trees, blobs) closely
enough to exercise push/pull/delete/conflict flows, including the fast-forward
check on ref updates. Install it with `fake.install(ghsync_module)`.
"""
import base64
import hashlib
import json


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FakeApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code}: {msg}")
        self.code, self.msg = code, msg


def serve_http(fake):
    """Expose a FakeGitHub over real HTTP on 127.0.0.1. Returns (server, base_url)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            raw = "raw" in (self.headers.get("Accept") or "")
            try:
                res = fake.api(self.command, self.path, body, raw=raw)
                out = res if isinstance(res, bytes) else json.dumps(res).encode()
                code = 200
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "code", 500) or 500
                out = json.dumps({"message": str(e)}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = do_PATCH = _do

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class FakeGitHub:
    def __init__(self, owner="me", repo="proj", branch="main"):
        self.owner, self.repo, self.branch = owner, repo, branch
        self.blobs = {}        # sha -> bytes
        self.trees = {}        # sha -> {path: {"mode", "type", "sha"}}
        self.commits = {}      # sha -> {"tree", "parents"}
        self.ref = None
        self.on_blob_download = None   # optional hook(sha) called before returning blob bytes
        self.calls = []
        self._mod = None
        self._n = 0
        self.commit_files({})

    # ---- helpers for tests
    def _store_tree(self, flat):
        sha = hashlib.sha1(json.dumps(flat, sort_keys=True).encode()).hexdigest()
        self.trees[sha] = {k: dict(v) for k, v in flat.items()}
        return sha

    def _new_commit(self, tree, parents):
        self._n += 1
        sha = hashlib.sha1(f"commit{self._n}{tree}{parents}".encode()).hexdigest()
        self.commits[sha] = {"tree": tree, "parents": list(parents)}
        return sha

    def put_blob(self, data):
        sha = blob_sha(data)
        self.blobs[sha] = data
        return sha

    def commit_files(self, files, keep=False):
        """files: {path: bytes | (mode, bytes) | ("commit", sha)}; keep=True merges into current tree."""
        flat = dict(self.flat()) if (keep and self.ref) else {}
        for p, v in files.items():
            if v is None:
                flat.pop(p, None)
                continue
            if isinstance(v, tuple) and v[0] == "commit":
                flat[p] = {"mode": "160000", "type": "commit", "sha": v[1]}
                continue
            mode, data = v if isinstance(v, tuple) else ("100644", v)
            flat[p] = {"mode": mode, "type": "blob", "sha": self.put_blob(data)}
        tree = self._store_tree(flat)
        parents = [self.ref] if self.ref else []
        self.ref = self._new_commit(tree, parents)
        return self.ref

    def flat(self):
        return self.trees[self.commits[self.ref]["tree"]]

    def files(self):
        return {p: self.blobs[e["sha"]] for p, e in self.flat().items() if e["type"] == "blob"}

    def install(self, mod):
        self._mod = mod
        mod.api = self.api
        return self

    # ---- API surface
    def _err(self, code, msg):
        exc = getattr(self._mod, "ApiError", FakeApiError)
        return exc(code, msg)

    def api(self, method, path, body=None, raw=False):
        self.calls.append((method, path))
        pre = f"/repos/{self.owner}/{self.repo}"
        if method == "GET" and path == f"{pre}/git/ref/heads/{self.branch}":
            return {"object": {"sha": self.ref}}
        if method == "GET" and path.startswith(f"{pre}/git/commits/"):
            c = self.commits[path.rsplit("/", 1)[1]]
            return {"sha": path.rsplit("/", 1)[1], "tree": {"sha": c["tree"]}}
        if method == "GET" and path.startswith(f"{pre}/git/trees/"):
            sha = path.rsplit("/", 1)[1].split("?")[0]
            flat = self.trees[sha]
            out, dirs = [], set()
            for p, e in sorted(flat.items()):
                parts = p.split("/")
                for i in range(1, len(parts)):
                    dirs.add("/".join(parts[:i]))
                out.append({"path": p, **e})
            out += [{"path": d, "mode": "040000", "type": "tree", "sha": "0" * 40} for d in sorted(dirs)]
            return {"sha": sha, "tree": out, "truncated": False}
        if method == "GET" and path.startswith(f"{pre}/git/blobs/"):
            sha = path.rsplit("/", 1)[1]
            if self.on_blob_download:
                self.on_blob_download(sha)
            if sha not in self.blobs:
                raise self._err(404, "no blob")
            return self.blobs[sha] if raw else {"content": base64.b64encode(self.blobs[sha]).decode()}
        if method == "POST" and path == f"{pre}/git/blobs":
            data = base64.b64decode(body["content"])
            return {"sha": self.put_blob(data)}
        if method == "POST" and path == f"{pre}/git/trees":
            flat = dict(self.trees[body["base_tree"]]) if body.get("base_tree") else {}
            for e in body["tree"]:
                if e["sha"] is None:
                    flat.pop(e["path"], None)
                else:
                    if e["type"] == "blob" and e["sha"] not in self.blobs:
                        raise self._err(422, "missing blob")
                    flat[e["path"]] = {"mode": e["mode"], "type": e["type"], "sha": e["sha"]}
            return {"sha": self._store_tree(flat)}
        if method == "POST" and path == f"{pre}/git/commits":
            return {"sha": self._new_commit(body["tree"], body["parents"])}
        if method == "PATCH" and path == f"{pre}/git/refs/heads/{self.branch}":
            new = body["sha"]
            if new != self.ref and self.ref not in self._ancestors(new):
                raise self._err(422, "Update is not a fast forward")
            self.ref = new
            return {"object": {"sha": new}}
        if method == "GET" and path == f"/repos/{self.owner}/{self.repo}":
            return {"default_branch": self.branch}
        if method == "GET" and path == "/user":
            return {"login": self.owner}
        raise self._err(404, f"fake: unhandled {method} {path}")

    def _ancestors(self, sha):
        seen, stack = set(), [sha]
        while stack:
            s = stack.pop()
            for p in self.commits.get(s, {}).get("parents", []):
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return seen
