"""Minimal GitHub client: publish a set of files as one commit on a new branch and open a pull request.

Uses the Git Data API so a whole export lands atomically (blobs -> tree -> commit -> ref -> PR). The token needs
`contents: write` and `pull requests: write` on the one repository. Nothing here ever pushes to the base branch.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, repo: str, token: str, base: str = "main", client: Optional[httpx.Client] = None):
        if "/" not in repo:
            raise GitHubError("Repository must look like owner/name.")
        self.repo, self.base = repo.strip(), (base or "main").strip()
        self.http = client or httpx.Client(base_url="https://api.github.com", timeout=60, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

    def _call(self, method: str, path: str, **kw):
        r = self.http.request(method, f"/repos/{self.repo}{path}", **kw)
        if r.status_code >= 300:
            try:
                msg = r.json().get("message", r.text)
            except Exception:  # noqa: BLE001
                msg = r.text
            raise GitHubError(f"GitHub {r.status_code}: {str(msg)[:200]}")
        return r.json() if r.content else {}

    def check(self) -> dict:
        info = self._call("GET", "")
        self._call("GET", f"/git/ref/heads/{self.base}")
        return {"repo": info.get("full_name"), "private": info.get("private"), "default_branch": info.get("default_branch"),
                "can_push": (info.get("permissions") or {}).get("push")}

    def get_file(self, path: str) -> Optional[str]:
        r = self.http.get(f"/repos/{self.repo}/contents/{path}", params={"ref": self.base},
                          headers={"Accept": "application/vnd.github.raw+json"})
        if r.status_code == 404:
            return None
        if r.status_code >= 300:
            raise GitHubError(f"GitHub {r.status_code} reading {path}")
        return r.text

    def publish(self, files: dict, owned_prefixes: tuple, title: str, body: str, branch: str = "") -> dict:
        head = self._call("GET", f"/git/ref/heads/{self.base}")["object"]["sha"]
        base_commit = self._call("GET", f"/git/commits/{head}")
        base_tree = base_commit["tree"]["sha"]
        existing = self._call("GET", f"/git/trees/{base_tree}", params={"recursive": "1"})
        entries = []
        for path, text in sorted(files.items()):
            blob = self._call("POST", "/git/blobs", json={"content": text, "encoding": "utf-8"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        for item in existing.get("tree", []):  # files the exporter used to own but no longer produces are removed
            p = item["path"]
            if item["type"] == "blob" and p not in files and any(p.startswith(x) for x in owned_prefixes):
                entries.append({"path": p, "mode": "100644", "type": "blob", "sha": None})
        tree = self._call("POST", "/git/trees", json={"base_tree": base_tree, "tree": entries})
        if tree["sha"] == base_tree:
            return {"changed": False, "message": "Nothing to publish: the repository already has exactly this."}
        commit = self._call("POST", "/git/commits", json={"message": title + "\n\n" + body, "tree": tree["sha"], "parents": [head]})
        branch = branch or time.strftime("studio/export-%Y%m%d-%H%M%S")
        self._call("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        pr = self._call("POST", "/pulls", json={"title": title, "head": branch, "base": self.base, "body": body})
        return {"changed": True, "branch": branch, "number": pr["number"], "url": pr["html_url"]}

    def pr_state(self, number: int) -> dict:
        pr = self._call("GET", f"/pulls/{number}")
        return {"number": number, "state": pr["state"], "merged": pr.get("merged", False), "url": pr["html_url"],
                "mergeable": pr.get("mergeable")}

    def merge(self, number: int) -> dict:
        r = self._call("PUT", f"/pulls/{number}/merge", json={"merge_method": "squash"})
        return {"merged": bool(r.get("merged")), "sha": r.get("sha"), "message": r.get("message", "")}
