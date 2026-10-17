"""Batch 4 (2026-10-17): redirects never carry a credential to another host, ftp:/file: redirects are refused, the
redirect chain is capped, hostile JSON becomes the source's error. Real sockets: local http.server instances."""

from __future__ import annotations

import http.server
import threading
import urllib.error
import urllib.request

import pytest

from redactor_common.core.lookup_client import (
    SafeRedirects,
    SameSiteRedirects,
    fetch_bytes,
    fetch_json,
    make_default_fetch,
)


class _Err(Exception):
    pass


class _Server:
    """A tiny local server: `routes` maps a path to (status, headers, body); every request's headers are recorded."""

    def __init__(self):
        self.routes = {}
        self.seen = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                status, headers, body = outer.routes.get(self.path, (404, {}, b"missing"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def two_servers():
    first, second = _Server(), _Server()
    yield first, second
    first.close()
    second.close()


SECRETS = {"Authorization": "Bearer s3cret", "Api-Key": "k-1", "X-Api-Key": "k-2", "Cookie": "sid=1",
           "Proxy-Authorization": "Basic x", "X-Custom-Token": "k-3"}


def _request(url, extra=None):
    headers = dict(SECRETS)
    headers["X-Harmless"] = "yes"
    headers.update(extra or {})
    return urllib.request.Request(url, headers=headers)


def test_credentials_are_not_forwarded_to_another_host(two_servers):
    first, second = two_servers
    first.routes["/start"] = (302, {"Location": f"{second.base}/landing"}, b"")
    second.routes["/landing"] = (200, {}, b"landed")
    fetch = make_default_fetch("UA/1", secret_headers=("X-Custom-Token",))
    assert fetch(_request(f"{first.base}/start")) == b"landed"
    sent_first = first.seen[0][1]
    assert sent_first["authorization"] == "Bearer s3cret" and sent_first["api-key"] == "k-1"
    sent_second = second.seen[0][1]
    for name in ("authorization", "api-key", "x-api-key", "cookie", "proxy-authorization", "x-custom-token"):
        assert name not in sent_second, name
    assert sent_second["x-harmless"] == "yes"


def test_credentials_stay_on_a_same_origin_redirect(two_servers):
    first, _ = two_servers
    first.routes["/start"] = (302, {"Location": "/final"}, b"")
    first.routes["/final"] = (200, {}, b"ok")
    assert make_default_fetch("UA/1")(_request(f"{first.base}/start")) == b"ok"
    assert first.seen[1][1]["authorization"] == "Bearer s3cret"


def test_a_download_without_credentials_follows_a_cross_host_redirect(two_servers):
    first, second = two_servers
    first.routes["/file"] = (302, {"Location": f"{second.base}/cdn/file"}, b"")
    second.routes["/cdn/file"] = (200, {}, b"payload")
    assert fetch_bytes(f"{first.base}/file", make_default_fetch("UA/1"), error_cls=_Err) == b"payload"


def test_an_ftp_redirect_is_refused(two_servers):
    first, _ = two_servers
    first.routes["/start"] = (302, {"Location": "ftp://127.0.0.1:1/secret"}, b"")
    with pytest.raises(urllib.error.HTTPError) as caught:
        make_default_fetch("UA/1")(f"{first.base}/start")
    assert caught.value.code == 302


def test_a_file_redirect_is_refused(two_servers):
    first, _ = two_servers
    first.routes["/start"] = (302, {"Location": "file:///C:/Windows/win.ini"}, b"")
    with pytest.raises(urllib.error.HTTPError) as caught:
        make_default_fetch("UA/1")(f"{first.base}/start")
    assert caught.value.code == 302


def test_too_many_redirects_are_refused(two_servers):
    first, _ = two_servers
    for number in range(9):
        first.routes[f"/r{number}"] = (302, {"Location": f"/r{number + 1}"}, b"")
    first.routes["/r9"] = (200, {}, b"end")
    with pytest.raises(urllib.error.HTTPError):
        make_default_fetch("UA/1")(f"{first.base}/r0")
    # a short chain still works
    first.routes["/s0"] = (302, {"Location": "/s1"}, b"")
    first.routes["/s1"] = (200, {}, b"short")
    assert make_default_fetch("UA/1")(f"{first.base}/s0") == b"short"


def test_https_is_never_downgraded_to_http():
    handler = SafeRedirects()
    request = urllib.request.Request("https://api.example.com/x", headers={"Authorization": "Bearer k"})
    assert handler.redirect_request(request, None, 302, "Found", {}, "http://api.example.com/x") is None
    assert handler.redirect_request(request, None, 302, "Found", {}, "http://cdn.example.net/x") is None
    assert handler.redirect_request(request, None, 302, "Found", {}, "ftp://api.example.com/x") is None
    ok = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example.net/x")
    assert ok is not None and not ok.has_header("Authorization")


def test_same_site_redirects_follow_only_the_same_origin():
    handler = SameSiteRedirects()
    request = urllib.request.Request("https://api.example.com/scenes", headers={"Authorization": "Bearer k"})
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example.net/x") is None
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://api.example.com:8443/x") is None
    same = handler.redirect_request(request, None, 302, "Found", {}, "https://api.example.com/other")
    assert same is not None and same.get_header("Authorization") == "Bearer k"


def test_fetch_json_turns_absurd_nesting_into_the_source_error():
    deep = b"[" * 100000 + b"]" * 100000
    with pytest.raises(_Err, match="unreadable response"):
        fetch_json("https://x/", fetch=lambda url: deep, error_cls=_Err, source_name="Src")


def test_json_shape_helpers_never_raise_on_hostile_values():
    from redactor_common.core.lookup_client import as_dict, as_int, as_text, dict_items, json_object

    assert json_object(None, _Err) == {} and json_object({"a": 1}, _Err) == {"a": 1}
    for bad in ([], [1], 5, "x", True):
        with pytest.raises(_Err, match="unreadable response from Src"):
            json_object(bad, _Err, "Src")
    assert dict_items([1, None, {}, {"a": 1}, [2], "x"]) == [{}, {"a": 1}] and dict_items("x") == [] and dict_items(None) == []
    assert as_dict([1]) == {} and as_dict({"a": 1}) == {"a": 1}
    assert as_text({"a": 1}) == "" and as_text(None, "d") == "d" and as_text(7) == "7" and as_text(True) == "" and as_text("x") == "x"
    assert as_int("12") == 12 and as_int(7) == 7 and as_int(None) == 0 and as_int(True, 9) == 9 and as_int({}, 3) == 3
    assert as_int(float("nan"), 4) == 4 and as_int("1e999", 5) == 5
