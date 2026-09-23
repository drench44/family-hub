"""The hub's /go2rtc/ proxy: the only way a browser reaches go2rtc.

go2rtc's API has no access control, so it is no longer published on the LAN
(2026-09-23): /api/streams and /api/config handed out every camera URL and let
anyone rewrite the config. These tests pin that the proxy serves the player
and its WebSocket for configured streams only, forwards nothing else, and
never goes quiet about a go2rtc that does not answer.

The WebSocket tests run against a REAL WebSocket server standing in for
go2rtc (websockets' threaded server on a free loopback port), so the bridge
is exercised end to end: handshake, both directions, text and binary, and
the close reaching go2rtc (an unclosed upstream keeps a consumer alive in
go2rtc).
"""
import asyncio
import importlib
import json
import logging
import threading

import httpx
import pytest
import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import serve

from family_hub import go2rtc_proxy


class FakeGo2rtc:
    """A WebSocket server that answers like go2rtc's /api/ws, on a thread."""

    def __init__(self, mode="echo"):
        self.mode = mode
        self.paths = []
        self.headers = []
        self.received = []
        self.closed = threading.Event()
        self.server = serve(self._handle, "127.0.0.1", 0, max_size=None)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def _handle(self, conn):
        self.paths.append(conn.request.path)
        self.headers.append(conn.request.headers)
        try:
            if self.mode == "drop":
                # go2rtc dying mid-stream (OOM-killed): no close frame at all
                conn.socket.close()
                return
            if self.mode == "close":
                conn.close()
                return
            for msg in conn:
                self.received.append(msg)
                if isinstance(msg, bytes):
                    conn.send("got %d bytes" % len(msg))
                elif msg == "bytes":
                    # bigger than websockets' 1 MiB default cap, like a 4K
                    # keyframe fragment in MSE mode
                    conn.send(b"\x00" * (2 * 1024 * 1024))
                else:
                    conn.send(json.dumps({"type": "echo", "value": msg}))
        finally:
            self.closed.set()

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _fresh_refusal_log():
    go2rtc_proxy._refused_logged.clear()
    yield
    go2rtc_proxy._refused_logged.clear()


@pytest.fixture
def fake():
    servers = []

    def make(mode="echo"):
        s = FakeGo2rtc(mode)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.stop()


def _app(tmp_path, monkeypatch, fetch_base, demo=False):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "port": 8138, "go2rtc_base": "http://unused-by-the-hub:1984", "calendars": [],
        "cameras": [{"src": "cam", "label": "Driveway", "hd": "cam_hd"}],
        "camera_page": [{"src": "cam", "label": "Driveway", "hd": "cam_hd"},
                        {"src": "grid_only", "label": "Mailbox"}],
        "panels": [],
    }))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("DISABLE_SYNC", "1")
    monkeypatch.setenv("CONFIG_PATH", str(p))
    monkeypatch.setenv("GO2RTC_FETCH_BASE", fetch_base)
    if demo:
        monkeypatch.setenv("DEMO", "1")
    else:
        monkeypatch.delenv("DEMO", raising=False)
    import family_hub.app as appmod
    importlib.reload(appmod)
    return appmod


# ---------------------------------------------------------------- helpers

def test_ws_base_maps_http_to_ws():
    assert go2rtc_proxy.ws_base("http://go2rtc:1984") == "ws://go2rtc:1984"
    assert go2rtc_proxy.ws_base("https://cams.example:443") == "wss://cams.example:443"
    with pytest.raises(ValueError):
        go2rtc_proxy.ws_base("rtsp://go2rtc:8554")


def test_upstream_url_carries_only_an_encoded_src():
    url = go2rtc_proxy.upstream_ws_url("http://go2rtc:1984/", "a b&dst=x")
    assert url == "ws://go2rtc:1984/api/ws?src=a%20b%26dst%3Dx"


# ---------------------------------------------------------------- WebSocket

def test_ws_bridges_both_ways_text_and_binary(tmp_path, monkeypatch, fake):
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base)
    with TestClient(appmod.app) as c:
        with c.websocket_connect("/go2rtc/api/ws?src=cam",
                                 headers={"user-agent": "WallBrowser/1.0"}) as ws:
            ws.send_text('{"type":"webrtc/offer","value":"sdp"}')
            assert json.loads(ws.receive_text()) == {
                "type": "echo", "value": '{"type":"webrtc/offer","value":"sdp"}'}
            ws.send_text("bytes")
            assert len(ws.receive_bytes()) == 2 * 1024 * 1024
            ws.send_bytes(b"\x01\x02\x03")
            assert ws.receive_text() == "got 3 bytes"
    # the browser leaving closes go2rtc's side too (else its consumer lingers)
    assert g.closed.wait(5), "the proxy never closed the upstream socket"
    assert g.paths == ["/api/ws?src=cam"]
    # go2rtc lists viewers by user agent and address: the browser's, not the hub's
    assert g.headers[0]["User-Agent"] == "WallBrowser/1.0"
    assert g.headers[0]["X-Forwarded-For"]


def test_ws_forwards_src_only_never_dst(tmp_path, monkeypatch, fake):
    """`dst` makes go2rtc take video IN (a producer): someone could replace a
    camera's picture on the wall. Only `src` may reach go2rtc."""
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base)
    with TestClient(appmod.app) as c:
        with c.websocket_connect("/go2rtc/api/ws?src=grid_only&dst=cam&name=x") as ws:
            ws.send_text("hi")
            ws.receive_text()
    assert g.paths == ["/api/ws?src=grid_only"]


@pytest.mark.parametrize("query", [
    "src=evil",                        # not a configured stream
    "src=rtsp%3A%2F%2Fattacker%2Fx",   # go2rtc would create a stream from a URL
    "src=exec%3Aid",                   # ... or run a command
    "dst=cam",                         # producer mode
    "",                                # nothing at all
])
def test_ws_refuses_unconfigured_streams_without_asking_go2rtc(
        tmp_path, monkeypatch, fake, query):
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base)
    with TestClient(appmod.app) as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(f"/go2rtc/api/ws?{query}") as ws:
                ws.receive_text()
    assert exc.value.code == go2rtc_proxy.CLOSE_POLICY
    assert g.paths == []


@pytest.mark.parametrize("origin", [
    "http://evil.example",          # any other web page on a LAN browser
    "http://testserver.evil.example",
    "null",                         # sandboxed / file:// pages
])
def test_ws_refuses_a_cross_origin_page(tmp_path, monkeypatch, fake, origin):
    """Browsers let any page open a WebSocket anywhere; go2rtc refused a
    foreign Origin, and the hub must keep refusing it now that go2rtc only
    ever sees the hub."""
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base)
    with TestClient(appmod.app) as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect("/go2rtc/api/ws?src=cam",
                                     headers={"origin": origin}) as ws:
                ws.receive_text()
    assert exc.value.code == go2rtc_proxy.CLOSE_POLICY
    assert g.paths == []


@pytest.mark.parametrize("origin", [
    "http://testserver",            # the hub's own page (the wall's iframe)
    "http://TESTSERVER:8138",       # port and case ignored, as go2rtc does
])
def test_ws_allows_the_hubs_own_pages(tmp_path, monkeypatch, fake, origin):
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base)
    with TestClient(appmod.app) as c:
        with c.websocket_connect("/go2rtc/api/ws?src=cam",
                                 headers={"origin": origin}) as ws:
            ws.send_text("hi")
            ws.receive_text()
    assert g.paths == ["/api/ws?src=cam"]


def test_ws_refusals_are_logged_once_per_stream(tmp_path, monkeypatch, fake, caplog):
    """A refused player leaves its tile black; the log must say why, with the
    values that disagree, but not on every retry of every screen."""
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base)
    caplog.set_level(logging.WARNING, logger="family_hub.go2rtc_proxy")
    with TestClient(appmod.app) as c:
        for _ in range(3):
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect("/go2rtc/api/ws?src=cam",
                                         headers={"origin": "http://elsewhere"}) as ws:
                    ws.receive_text()
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect("/go2rtc/api/ws?src=typo") as ws:
                    ws.receive_text()
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2, lines
    assert "'cam' stream (cross-origin)" in lines[0]
    assert "'http://elsewhere'" in lines[0] and "'testserver'" in lines[0]
    assert "'typo' stream (not a configured stream)" in lines[1]


def test_ws_refusal_logs_again_after_the_window_and_stays_bounded(monkeypatch, caplog):
    clock = {"t": 1000.0}
    monkeypatch.setattr(go2rtc_proxy.time, "monotonic", lambda: clock["t"])
    caplog.set_level(logging.WARNING, logger="family_hub.go2rtc_proxy")
    go2rtc_proxy._warn_refused("cross-origin", "cam", "x")
    go2rtc_proxy._warn_refused("cross-origin", "cam", "x")
    assert len(caplog.records) == 1
    # a misconfiguration that lasts is reported again, not once per process
    clock["t"] += go2rtc_proxy.REFUSED_LOG_EVERY_S
    go2rtc_proxy._warn_refused("cross-origin", "cam", "x")
    assert len(caplog.records) == 2
    # src is caller-chosen: random names must not grow the table without end
    for i in range(10_000):
        go2rtc_proxy._warn_refused("not a configured stream", f"junk{i}", "x")
    assert len(go2rtc_proxy._refused_logged) <= go2rtc_proxy.REFUSED_LOG_MAX


def test_ws_go2rtc_hanging_on_the_handshake_times_out_to_1011(
        tmp_path, monkeypatch, caplog):
    """go2rtc swapping (2026-09-22) took connections and never answered. The
    player must get a 1011 (and retry), not wait on a black tile forever."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)   # the kernel accepts; nobody ever answers
    port = srv.getsockname()[1]
    monkeypatch.setattr(go2rtc_proxy, "OPEN_TIMEOUT", 0.3)
    appmod = _app(tmp_path, monkeypatch, f"http://127.0.0.1:{port}")
    caplog.set_level(logging.WARNING, logger="family_hub.go2rtc_proxy")
    try:
        with TestClient(appmod.app) as c:
            with c.websocket_connect("/go2rtc/api/ws?src=cam") as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
    finally:
        srv.close()
    assert exc.value.code == go2rtc_proxy.CLOSE_UNAVAILABLE
    assert "could not open the cam stream" in caplog.text
    assert "TimeoutError" in caplog.text


def test_stream_names_with_spaces_survive_the_round_trip(tmp_path, monkeypatch, fake):
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base)
    monkeypatch.setattr(appmod.cfg, "cameras", [{"src": "front door", "label": "F"}])
    monkeypatch.setattr(appmod.cfg, "camera_page", [])
    with TestClient(appmod.app) as c:
        tile = c.get("/api/hub").json()["links"]["cameras"][0]["tile"]
        assert tile == "/go2rtc/stream.html?src=front%20door&mode=webrtc"
        # the player encodes src the same way (encodeURIComponent)
        with c.websocket_connect("/go2rtc/api/ws?src=front%20door") as ws:
            ws.send_text("hi")
            ws.receive_text()
    assert g.paths == ["/api/ws?src=front%20door"]


def test_a_non_dict_camera_entry_does_not_break_the_proxy(tmp_path, monkeypatch, fake):
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base)
    monkeypatch.setattr(appmod.cfg, "cameras", [{"src": "good", "label": "G"}, "oops"])
    monkeypatch.setattr(appmod.cfg, "camera_page", [])
    with TestClient(appmod.app) as c:
        with c.websocket_connect("/go2rtc/api/ws?src=good") as ws:
            ws.send_text("hi")
            ws.receive_text()
    assert g.paths == ["/api/ws?src=good"]


def test_ws_hd_twin_is_allowed(tmp_path, monkeypatch, fake):
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base)
    with TestClient(appmod.app) as c:
        with c.websocket_connect("/go2rtc/api/ws?src=cam_hd") as ws:
            ws.send_text("hi")
            ws.receive_text()
    assert g.paths == ["/api/ws?src=cam_hd"]


def test_ws_go2rtc_down_closes_1011_and_logs(tmp_path, monkeypatch, fake, caplog):
    g = fake()
    dead = g.base
    g.stop()   # nothing listens on that port any more
    appmod = _app(tmp_path, monkeypatch, dead)
    caplog.set_level(logging.WARNING, logger="family_hub.go2rtc_proxy")
    with TestClient(appmod.app) as c:
        # accepted, THEN closed 1011: a close before accept would reach a real
        # browser as a bare HTTP 403, indistinguishable from a refusal
        with c.websocket_connect("/go2rtc/api/ws?src=cam") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == go2rtc_proxy.CLOSE_UNAVAILABLE
    assert "could not open the cam stream" in caplog.text


def test_ws_go2rtc_dying_mid_stream_is_logged(tmp_path, monkeypatch, fake, caplog):
    g = fake("drop")
    appmod = _app(tmp_path, monkeypatch, g.base)
    caplog.set_level(logging.WARNING, logger="family_hub.go2rtc_proxy")
    with TestClient(appmod.app) as c:
        with c.websocket_connect("/go2rtc/api/ws?src=cam") as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    assert "the cam stream ended on an error" in caplog.text


def test_ws_clean_close_is_not_logged(tmp_path, monkeypatch, fake, caplog):
    g = fake("close")
    appmod = _app(tmp_path, monkeypatch, g.base)
    caplog.set_level(logging.WARNING, logger="family_hub.go2rtc_proxy")
    with TestClient(appmod.app) as c:
        with c.websocket_connect("/go2rtc/api/ws?src=cam") as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    assert caplog.text == ""


class _IdleBrowser:
    """The browser side of a bridge that just sits on an open stream."""
    client = None
    headers: dict = {}

    def __init__(self):
        self.closed = False

    async def accept(self):
        pass

    async def receive(self):
        await asyncio.Event().wait()   # never says anything

    async def send_text(self, _):
        pass

    async def send_bytes(self, _):
        pass

    async def close(self, code=1000):
        self.closed = True


def test_ws_bridge_cancelled_still_closes_go2rtc(fake):
    """The server stopping (or the ASGI server dropping the client) cancels
    the bridge mid-stream. go2rtc's side must still be closed, or its
    consumer stays alive in go2rtc."""
    g = fake()

    async def run():
        browser = _IdleBrowser()
        task = asyncio.create_task(go2rtc_proxy.bridge(browser, g.base, "cam", {"cam"}))
        for _ in range(200):
            if g.paths:
                break
            await asyncio.sleep(0.01)
        assert g.paths == ["/api/ws?src=cam"]
        # Cancel, then cancel again while it tears down: anyio (under
        # Starlette) re-delivers a cancellation at every await until the
        # task ends, so the teardown itself gets hit too.
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(200):   # the shielded teardown finishes on its own
            if browser.closed:
                break
            await asyncio.sleep(0.01)
        return browser.closed
    assert asyncio.run(run())
    assert g.closed.wait(5), "a cancelled bridge left go2rtc's socket open"


def test_ws_off_in_demo(tmp_path, monkeypatch, fake):
    g = fake()
    appmod = _app(tmp_path, monkeypatch, g.base, demo=True)
    with TestClient(appmod.app) as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect("/go2rtc/api/ws?src=cam") as ws:
                ws.receive_text()
    assert exc.value.code == go2rtc_proxy.CLOSE_POLICY
    assert g.paths == []


# ---------------------------------------------------------------- HTTP

def _mock_http(appmod, monkeypatch, handler):
    seen = []

    def wrapped(req):
        seen.append(req)
        return handler(req)
    monkeypatch.setattr(appmod, "_http", httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return seen


def test_player_files_come_from_go2rtc(tmp_path, monkeypatch):
    appmod = _app(tmp_path, monkeypatch, "http://go2rtc:1984")
    seen = _mock_http(appmod, monkeypatch, lambda req: httpx.Response(
        200, content=b"<html>player</html>", headers={"content-type": "text/plain"}))
    with TestClient(appmod.app) as c:
        r = c.get("/go2rtc/stream.html?src=cam&mode=webrtc")
        assert r.status_code == 200 and r.content == b"<html>player</html>"
        assert r.headers["content-type"].startswith("text/html")
        # revalidated like every other page, so a go2rtc upgrade reaches the wall
        assert r.headers["cache-control"] == "no-cache"
        for js in ("video-stream.js", "video-rtc.js"):
            r = c.get(f"/go2rtc/{js}")
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/javascript")
    assert [str(q.url) for q in seen] == [
        "http://go2rtc:1984/stream.html",
        "http://go2rtc:1984/video-stream.js",
        "http://go2rtc:1984/video-rtc.js"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/go2rtc/api/streams"),
    ("GET", "/go2rtc/api/config"),
    ("POST", "/go2rtc/api/config"),
    ("POST", "/go2rtc/api/restart"),
    ("PUT", "/go2rtc/api/streams?src=exec:id"),
    ("GET", "/go2rtc/api/frame.jpeg?src=cam"),
    ("GET", "/go2rtc/index.html"),
    ("GET", "/go2rtc/config.html"),
    ("GET", "/go2rtc/api/hls/..%2Fstreams"),
    ("GET", "/go2rtc/api%2Fstreams"),
    ("GET", "/go2rtc/api%2Fconfig"),
])
def test_nothing_else_of_go2rtc_is_reachable(tmp_path, monkeypatch, method, path):
    appmod = _app(tmp_path, monkeypatch, "http://go2rtc:1984")
    seen = _mock_http(appmod, monkeypatch, lambda req: httpx.Response(200, content=b"secret"))
    with TestClient(appmod.app) as c:
        r = c.request(method, path)
    assert r.status_code in (404, 405)
    assert b"secret" not in r.content
    assert seen == []


@pytest.mark.parametrize("upstream,why", [
    (lambda req: httpx.Response(500), "answered 500"),
    (lambda req: httpx.Response(404), "answered 404"),   # a player file must exist
    (lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused")), "ConnectError"),
])
def test_player_file_failures_are_502_and_logged(tmp_path, monkeypatch, caplog,
                                                 upstream, why):
    appmod = _app(tmp_path, monkeypatch, "http://go2rtc:1984")
    _mock_http(appmod, monkeypatch, upstream)
    caplog.set_level(logging.WARNING, logger="family_hub")
    with TestClient(appmod.app) as c:
        assert c.get("/go2rtc/stream.html").status_code == 502
    assert why in caplog.text


def test_hls_passes_only_the_session_params(tmp_path, monkeypatch):
    appmod = _app(tmp_path, monkeypatch, "http://go2rtc:1984")
    seen = _mock_http(appmod, monkeypatch, lambda req: httpx.Response(
        200, content=b"#EXTM3U", headers={"content-type": "application/vnd.apple.mpegurl"}))
    with TestClient(appmod.app) as c:
        r = c.get("/go2rtc/api/hls/playlist.m3u8?id=abc&src=exec:id&dst=cam")
        assert r.status_code == 200 and r.content == b"#EXTM3U"
        assert r.headers["content-type"] == "application/vnd.apple.mpegurl"
        assert c.get("/go2rtc/api/hls/segment.m4s?id=abc&n=3").status_code == 200
        assert c.get("/go2rtc/api/hls/other.bin?id=abc").status_code == 404
    assert [str(q.url) for q in seen] == [
        "http://go2rtc:1984/api/hls/playlist.m3u8?id=abc",
        "http://go2rtc:1984/api/hls/segment.m4s?id=abc&n=3"]


@pytest.mark.parametrize("upstream", [
    lambda req: httpx.Response(503),
    lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused")),
])
def test_hls_go2rtc_failure_is_502(tmp_path, monkeypatch, caplog, upstream):
    appmod = _app(tmp_path, monkeypatch, "http://go2rtc:1984")
    _mock_http(appmod, monkeypatch, upstream)
    caplog.set_level(logging.WARNING, logger="family_hub")
    with TestClient(appmod.app) as c:
        assert c.get("/go2rtc/api/hls/playlist.m3u8?id=a").status_code == 502
    assert "go2rtc proxy: GET" in caplog.text


def test_hls_ended_session_404_passes_through(tmp_path, monkeypatch):
    appmod = _app(tmp_path, monkeypatch, "http://go2rtc:1984")
    _mock_http(appmod, monkeypatch, lambda req: httpx.Response(404))
    with TestClient(appmod.app) as c:
        assert c.get("/go2rtc/api/hls/playlist.m3u8?id=gone").status_code == 404


def test_player_off_in_demo(tmp_path, monkeypatch):
    appmod = _app(tmp_path, monkeypatch, "http://go2rtc:1984", demo=True)
    seen = _mock_http(appmod, monkeypatch, lambda req: httpx.Response(200))
    with TestClient(appmod.app) as c:
        assert c.get("/go2rtc/stream.html").status_code == 404
        assert c.get("/go2rtc/api/hls/playlist.m3u8?id=a").status_code == 404
    assert seen == []


def test_camera_links_point_at_the_hub_not_go2rtc(tmp_path, monkeypatch):
    """The wall's iframes load the player from the hub (same origin), never
    from go2rtc_base, which the browser can no longer reach."""
    appmod = _app(tmp_path, monkeypatch, "http://go2rtc:1984")
    with TestClient(appmod.app) as c:
        links = c.get("/api/hub").json()["links"]
    urls = [u for cam in links["cameras"] + links["camera_page"]
            for u in (cam["tile"], cam["full"])]
    assert urls and all(u.startswith("/go2rtc/stream.html?src=") for u in urls)
    assert not any("1984" in u or "unused-by-the-hub" in u for u in urls)
