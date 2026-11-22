# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Leon Priest (GitHub: 7h3v01d)
"""Several fake GitHub repos behind one api() function, routed by /repos/<owner>/<repo>."""
from fake_github import FakeGitHub


class MultiFake:
    def __init__(self, owner="me"):
        self.owner = owner
        self.repos = {}
        self.calls = []

    def repo(self, name, branch="main"):
        if name not in self.repos:
            self.repos[name] = FakeGitHub(owner=self.owner, repo=name, branch=branch)
        return self.repos[name]

    def install(self, mod):
        for r in self.repos.values():
            r._mod = mod
        self._mod = mod
        mod.api = self.api
        return self

    def api(self, method, path, body=None, raw=False):
        self.calls.append((method, path))
        parts = path.split("/")
        if len(parts) > 3 and parts[1] == "repos" and parts[3] in self.repos:
            return self.repos[parts[3]].api(method, path, body, raw)
        if path == "/user":
            return {"login": self.owner}
        exc = getattr(self._mod, "ApiError", Exception)
        raise exc(404, f"multi-fake: unknown repo in {path}")
