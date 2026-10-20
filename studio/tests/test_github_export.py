import json

import httpx
import pytest

from studio.github import GitHub, GitHubError


def fake_github(state):
    """A tiny in-memory GitHub: refs, trees, commits, PRs."""
    def handler(req: httpx.Request) -> httpx.Response:
        p, m = req.url.path.replace("/repos/o/r", ""), req.method
        body = json.loads(req.content) if req.content else {}
        state.setdefault("calls", []).append((m, p))
        if p == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "H1"}})
        if p == "/git/commits/H1":
            return httpx.Response(200, json={"tree": {"sha": "T0"}})
        if p == "/git/trees/T0":
            return httpx.Response(200, json={"tree": [{"path": "customizations/csd_customizations/csd_customizations/fixtures/old.json", "type": "blob"},
                                                      {"path": "README.md", "type": "blob"}]})
        if p == "/git/blobs":
            return httpx.Response(201, json={"sha": "B" + str(len(state["calls"]))})
        if p == "/git/trees" and m == "POST":
            state["tree"] = body["tree"]
            return httpx.Response(201, json={"sha": "T0" if state.get("same") else "T1"})
        if p == "/git/commits" and m == "POST":
            return httpx.Response(201, json={"sha": "C1"})
        if p == "/git/refs":
            state["branch"] = body["ref"]
            return httpx.Response(201, json={})
        if p == "/pulls" and m == "POST":
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/o/r/pull/7"})
        if p == "/pulls/7/merge":
            return httpx.Response(200, json={"merged": True, "sha": "M"})
        if p == "/pulls/7":
            return httpx.Response(200, json={"state": "open", "merged": False, "html_url": "u", "mergeable": True})
        return httpx.Response(404, json={"message": "nope " + p})
    return httpx.MockTransport(handler)


def client(state):
    return httpx.Client(base_url="https://api.github.com", transport=fake_github(state))


def test_publish_opens_pr_and_removes_files_it_no_longer_produces():
    st = {}
    gh = GitHub("o/r", "t", client=client(st))
    res = gh.publish({"customizations/csd_customizations/csd_customizations/fixtures/new.json": "[]"},
                     ("customizations/csd_customizations/csd_customizations/fixtures/",), "t", "b")
    assert res["changed"] and res["number"] == 7 and st["branch"].startswith("refs/heads/studio/export-")
    deleted = [e["path"] for e in st["tree"] if e["sha"] is None]
    assert deleted == ["customizations/csd_customizations/csd_customizations/fixtures/old.json"]  # README untouched


def test_nothing_to_publish_when_tree_is_identical():
    st = {"same": True}
    res = GitHub("o/r", "t", client=client(st)).publish({"a.txt": "x"}, ("a",), "t", "b")
    assert res["changed"] is False and ("POST", "/pulls") not in st["calls"]


def test_merge_and_errors():
    st = {}
    gh = GitHub("o/r", "t", client=client(st))
    assert gh.merge(7)["merged"] is True and gh.pr_state(7)["state"] == "open"
    with pytest.raises(GitHubError):
        gh.pr_state(99)
    with pytest.raises(GitHubError):
        GitHub("badrepo", "t")
