import json
import urllib.error

import pytest

from redactor_common.core.lookup_client import LookupError, fetch_bytes, fetch_json, make_default_fetch


class _SourceError(LookupError):
    pass


def test_fetch_json_success():
    data = fetch_json("https://x/", fetch=lambda url: b'{"a": 1}')
    assert data == {"a": 1}


def test_fetch_json_uses_given_error_class():
    def _raise_404(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with pytest.raises(_SourceError):
        fetch_json("https://x/", fetch=_raise_404, error_cls=_SourceError)


def test_fetch_json_http_error_message_includes_source_name_and_code():
    def _raise_500(url):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    with pytest.raises(_SourceError, match="Widget API.*HTTP 500"):
        fetch_json("https://x/", fetch=_raise_500, error_cls=_SourceError, source_name="Widget API")


def test_fetch_json_ignore_404_returns_none_instead_of_raising():
    def _raise_404(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    assert fetch_json("https://x/", fetch=_raise_404, error_cls=_SourceError, ignore_404=True) is None


def test_fetch_json_network_error_translated():
    def _raise_network_error(url):
        raise urllib.error.URLError("no route to host")

    with pytest.raises(_SourceError, match="Could not reach"):
        fetch_json("https://x/", fetch=_raise_network_error, error_cls=_SourceError)


def test_fetch_json_malformed_json_translated():
    with pytest.raises(_SourceError, match="unreadable"):
        fetch_json("https://x/", fetch=lambda url: b"not json at all", error_cls=_SourceError)


def test_make_default_fetch_sets_user_agent(monkeypatch):
    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"ok"

    def _fake_urlopen(request, timeout):
        captured["user_agent"] = request.get_header("User-agent")
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    fetch = make_default_fetch("MyApp/1.0", timeout=5.0)
    result = fetch("https://example.com/x")

    assert result == b"ok"
    assert captured["user_agent"] == "MyApp/1.0"
    assert captured["url"] == "https://example.com/x"
    assert captured["timeout"] == 5.0


def test_fetch_bytes_success():
    assert fetch_bytes("https://x/", fetch=lambda url: b"\xff\xd8\xff") == b"\xff\xd8\xff"


def test_fetch_bytes_http_error_names_what_was_being_downloaded():
    def _raise_404(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with pytest.raises(_SourceError, match="cover image.*HTTP 404"):
        fetch_bytes("https://x/", fetch=_raise_404, error_cls=_SourceError, what="cover image")


def test_fetch_bytes_network_error_translated():
    def _raise_network_error(url):
        raise urllib.error.URLError("no route to host")

    with pytest.raises(_SourceError, match="Could not download"):
        fetch_bytes("https://x/", fetch=_raise_network_error, error_cls=_SourceError, what="cover image")
