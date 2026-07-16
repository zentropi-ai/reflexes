"""Tests for policy sources (`reflexes.policy_sources`) with a mocked HTTP client.

Covers file/zentropi/github fetch, not-found → PolicyNotFound (fail closed),
auth → PermissionError, and the scheme dispatch. No network.
"""
from __future__ import annotations

import json as _json

import pytest

from reflexes import policy_sources
from reflexes.policy_ref import parse_ref
from reflexes.policy_sources import PolicyNotFound


# --------------------------------------------------------------------------- #
# Fake httpx client
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text if text else (_json.dumps(json_body) if json_body is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Routes .get(url) to the first matching (predicate, response). A route's
    response may be a list, which is consumed one-per-call (to script retries)."""

    routes: list = []
    last_headers: dict = {}
    calls: int = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        _FakeClient.last_headers = headers or {}
        _FakeClient.calls += 1
        for pred, resp in _FakeClient.routes:
            if pred(url):
                item = resp.pop(0) if isinstance(resp, list) and len(resp) > 1 else (
                    resp[0] if isinstance(resp, list) else resp
                )
                if isinstance(item, Exception):
                    raise item  # script a network error
                return item
        raise AssertionError(f"no fake route for URL: {url}")


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.routes = []
    _FakeClient.last_headers = {}
    _FakeClient.calls = 0
    monkeypatch.setattr(policy_sources.httpx, "Client", _FakeClient)
    monkeypatch.setattr(policy_sources.time, "sleep", lambda *a, **k: None)  # no real delays
    return _FakeClient


# --------------------------------------------------------------------------- #
# file
# --------------------------------------------------------------------------- #

def test_file_fetch_reads_relative_to_base_dir(tmp_path):
    (tmp_path / "policies").mkdir()
    p = tmp_path / "policies" / "x.md"
    p.write_text("# Criteria\n\nlocal body")
    ref = parse_ref("./policies/x.md")
    text, src = policy_sources.file_fetch(ref, base_dir=tmp_path)
    assert "local body" in text
    assert src == str(p)


def test_file_fetch_missing_raises_filenotfound(tmp_path):
    ref = parse_ref("./nope.md")
    with pytest.raises(FileNotFoundError):
        policy_sources.file_fetch(ref, base_dir=tmp_path)


# --------------------------------------------------------------------------- #
# zentropi
# --------------------------------------------------------------------------- #

def test_zentropi_pinned_fetches_that_version(fake_http, monkeypatch):
    monkeypatch.setenv("ZENTROPI_API_KEY", "zt_test")
    fake_http.routes = [
        (lambda u: u.endswith("/versions/v5"),
         _FakeResponse(200, {"id": "v5", "criteria_text": "PINNED CRITERIA"})),
    ]
    ref = parse_ref("zentropi:lab123@v5")
    assert policy_sources.zentropi_fetch(ref) == "PINNED CRITERIA"
    assert fake_http.last_headers.get("Authorization") == "Bearer zt_test"


def test_zentropi_unpinned_picks_latest_deployed(fake_http):
    fake_http.routes = [
        (lambda u: u.endswith("/versions"),
         _FakeResponse(200, [
             {"id": "a", "criteria_text": "OLD", "is_deployed": True,  "created_at": "2026-01-01T00:00:00"},
             {"id": "b", "criteria_text": "NEW", "is_deployed": True,  "created_at": "2026-02-01T00:00:00"},
             {"id": "c", "criteria_text": "DRAFT", "is_deployed": False, "created_at": "2026-03-01T00:00:00"},
         ])),
    ]
    ref = parse_ref("zentropi:lab123")
    assert policy_sources.zentropi_fetch(ref) == "NEW"  # latest *deployed*, not the draft


def test_zentropi_no_deployed_version_is_not_found(fake_http):
    fake_http.routes = [
        (lambda u: u.endswith("/versions"),
         _FakeResponse(200, [{"id": "c", "criteria_text": "DRAFT", "is_deployed": False, "created_at": "x"}])),
    ]
    with pytest.raises(PolicyNotFound):
        policy_sources.zentropi_fetch(parse_ref("zentropi:lab123"))


def test_zentropi_404_is_not_found(fake_http):
    fake_http.routes = [(lambda u: True, _FakeResponse(404, text="not found"))]
    with pytest.raises(PolicyNotFound):
        policy_sources.zentropi_fetch(parse_ref("zentropi:lab123@v9"))


def test_zentropi_401_is_permission_error(fake_http):
    fake_http.routes = [(lambda u: True, _FakeResponse(401, text="unauthorized"))]
    with pytest.raises(PermissionError):
        policy_sources.zentropi_fetch(parse_ref("zentropi:lab123@v9"))


# --------------------------------------------------------------------------- #
# retries (transient only)
# --------------------------------------------------------------------------- #

def test_retries_transient_5xx_then_succeeds(fake_http):
    # 503, 503, then 200 → should retry and succeed within 3 attempts.
    fake_http.routes = [(lambda u: True, [
        _FakeResponse(503, text="overloaded"),
        _FakeResponse(503, text="overloaded"),
        _FakeResponse(200, {"criteria_text": "RECOVERED"}),
    ])]
    assert policy_sources.zentropi_fetch(parse_ref("zentropi:lab@v1")) == "RECOVERED"
    assert fake_http.calls == 3


def test_retries_on_network_error_then_succeeds(fake_http):
    fake_http.routes = [(lambda u: True, [
        policy_sources.httpx.ConnectError("boom"),
        _FakeResponse(200, {"criteria_text": "OK"}),
    ])]
    assert policy_sources.zentropi_fetch(parse_ref("zentropi:lab@v1")) == "OK"
    assert fake_http.calls == 2


def test_does_not_retry_terminal_404(fake_http):
    fake_http.routes = [(lambda u: True, _FakeResponse(404, text="nope"))]
    with pytest.raises(PolicyNotFound):
        policy_sources.zentropi_fetch(parse_ref("zentropi:lab@v9"))
    assert fake_http.calls == 1  # terminal → no retry


def test_retries_exhausted_raises(fake_http):
    fake_http.routes = [(lambda u: True, [
        _FakeResponse(503), _FakeResponse(503), _FakeResponse(503),
    ])]
    with pytest.raises(Exception):
        policy_sources.zentropi_fetch(parse_ref("zentropi:lab@v1"))
    assert fake_http.calls == 3


# --------------------------------------------------------------------------- #
# github
# --------------------------------------------------------------------------- #

def test_github_pinned_builds_raw_url_and_returns_text(fake_http):
    captured = {}

    def pred(u):
        captured["url"] = u
        return True

    fake_http.routes = [(pred, _FakeResponse(200, text="# Criteria\n\nfrom github"))]
    ref = parse_ref("github:acme/repo/pols/p.md@v1.2.0")
    assert policy_sources.github_fetch(ref) == "# Criteria\n\nfrom github"
    assert captured["url"] == "https://raw.githubusercontent.com/acme/repo/v1.2.0/pols/p.md"


def test_github_unpinned_uses_head(fake_http):
    captured = {}
    fake_http.routes = [((lambda u: captured.setdefault("url", u) or True),
                         _FakeResponse(200, text="body"))]
    policy_sources.github_fetch(parse_ref("github:acme/repo/p.md"))
    assert "/HEAD/" in captured["url"]


def test_github_404_is_not_found(fake_http):
    fake_http.routes = [(lambda u: True, _FakeResponse(404, text="nope"))]
    with pytest.raises(PolicyNotFound):
        policy_sources.github_fetch(parse_ref("github:acme/repo/p.md@v1"))


def test_github_malformed_locator_is_not_found(fake_http):
    with pytest.raises(PolicyNotFound):
        policy_sources.github_fetch(parse_ref("github:acme/repo"))  # no path component


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

def test_fetch_criteria_dispatches_by_scheme(fake_http, tmp_path):
    # file
    (tmp_path / "p.md").write_text("FILE")
    assert policy_sources.fetch_criteria(parse_ref("./p.md"), base_dir=tmp_path) == "FILE"
    # zentropi
    fake_http.routes = [(lambda u: "/versions/v1" in u,
                         _FakeResponse(200, {"criteria_text": "ZEN"}))]
    assert policy_sources.fetch_criteria(parse_ref("zentropi:l@v1")) == "ZEN"
    # github
    fake_http.routes = [(lambda u: "raw.githubusercontent.com" in u,
                         _FakeResponse(200, text="GH"))]
    assert policy_sources.fetch_criteria(parse_ref("github:o/r/p.md@v1")) == "GH"


def test_cacheable_schemes_excludes_file():
    assert "file" not in policy_sources.CACHEABLE_SCHEMES
    assert "zentropi" in policy_sources.CACHEABLE_SCHEMES
    assert "github" in policy_sources.CACHEABLE_SCHEMES
