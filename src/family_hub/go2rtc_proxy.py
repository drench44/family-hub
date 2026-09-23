"""The hub's gate in front of go2rtc: the only way a browser reaches it.

go2rtc's HTTP API has no per-path access control. Published on the LAN, it
hands anyone who asks every camera URL it knows (`/api/streams`,
`/api/config`: an RTSP URL is often a password or a per-camera token), lets
them rewrite its config (`POST /api/config`, then `/api/restart`), add
streams (`PUT /api/streams`), or push their own video into a camera's stream
(`/api/ws?dst=...`). Basic auth does not fit a wall: the player is an iframe
on a kiosk nobody types a password into, and any password the page could
carry would be readable by every browser on the LAN anyway.

So go2rtc's API stays on the compose network, and the hub proxies exactly
what its camera tiles use, nothing else:

- the player page and its two scripts (PLAYER_FILES), fetched from go2rtc so
  they always match the go2rtc version that answers the WebSocket;
- the player's WebSocket, `/api/ws?src=<name>`, only for a stream name the
  hub's own config shows (the caller passes that set in), and with `src` as
  the only parameter forwarded (never `dst`, never a URL);
- the HLS files the same player falls back to on an old iPhone
  (HLS_FILES), by the session `id` the WebSocket handed out.

WebRTC media does not come through here: the browser gets it straight from
go2rtc's WebRTC port (8555), which carries no API.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote, urlsplit

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger("family_hub.go2rtc_proxy")

# go2rtc's stream.html loads ./video-stream.js, which imports ./video-rtc.js.
PLAYER_FILES = {
    "stream.html": "text/html; charset=utf-8",
    "video-stream.js": "text/javascript; charset=utf-8",
    "video-rtc.js": "text/javascript; charset=utf-8",
}
# video-rtc.js builds these from the WebSocket URL (.../api/ws -> .../api/hls/).
HLS_FILES = frozenset({"playlist.m3u8", "segment.m4s", "segment.ts", "init.mp4"})
# The only query parameters go2rtc's HLS handlers read.
HLS_PARAMS = ("id", "n")

# A camera that is down still answers the WebSocket at once (the error comes
# later, as a message), so a slow open means go2rtc itself is in trouble.
OPEN_TIMEOUT = 5.0
# MSE mode sends fMP4 fragments; a 4K keyframe fragment can pass 1 MiB (the
# websockets default cap). Bounded, not unlimited.
MAX_MESSAGE = 16 * 1024 * 1024

# WebSocket close codes (RFC 6455).
CLOSE_POLICY = 1008      # a stream this hub does not show
CLOSE_UNAVAILABLE = 1011  # go2rtc did not answer


def ws_base(http_base: str) -> str:
    """go2rtc's HTTP base as its WebSocket base (http -> ws, https -> wss)."""
    if http_base.startswith("https://"):
        return "wss://" + http_base[len("https://"):]
    if http_base.startswith("http://"):
        return "ws://" + http_base[len("http://"):]
    raise ValueError(f"go2rtc base is not an http(s) URL: {http_base!r}")


def upstream_ws_url(http_base: str, src: str) -> str:
    return f"{ws_base(http_base.rstrip('/'))}/api/ws?src={quote(src, safe='')}"


def _hostname(hostport: str) -> str:
    try:
        return (urlsplit("//" + hostport).hostname or "").lower()
    except ValueError:
        return ""


def same_origin(client: WebSocket) -> bool:
    """go2rtc's own check, kept now that the hub stands in front of it (the
    hub connects without an Origin, so go2rtc's check no longer applies):
    browsers do not apply the same-origin policy to WebSockets, so without
    this any web page open on a LAN browser could script a camera's video
    out through the hub. The Origin's host must be the host the browser
    asked for, port ignored as go2rtc ignores it. No Origin at all (not a
    browser) is allowed, as go2rtc allows it."""
    origin = client.headers.get("origin")
    if origin is None:
        return True
    try:
        o = urlsplit(origin).hostname
    except ValueError:
        return False
    host = _hostname(client.headers.get("host", ""))
    return bool(o) and bool(host) and o.lower() == host


# A refused stream is logged once per (reason, src) per REFUSED_LOG_EVERY_S:
# every open screen retries a refused player every few seconds.
REFUSED_LOG_EVERY_S = 600.0
REFUSED_LOG_MAX = 256
_refused_logged: dict[tuple[str, str], float] = {}


def _warn_refused(reason: str, src: str, detail: str) -> None:
    key = (reason, src[:200])
    now = time.monotonic()
    if len(_refused_logged) >= REFUSED_LOG_MAX:
        # src is whatever the caller sent: never let it grow this without end
        for k in [k for k, t in _refused_logged.items() if now - t >= REFUSED_LOG_EVERY_S]:
            del _refused_logged[k]
        if len(_refused_logged) >= REFUSED_LOG_MAX:
            _refused_logged.clear()
    last = _refused_logged.get(key)
    if last is not None and now - last < REFUSED_LOG_EVERY_S:
        return
    _refused_logged[key] = now
    log.warning("go2rtc proxy: refused the %r stream (%s): %s", src, reason, detail)


# The upstream connector. A module attribute so tests can hand in a fake.
connect = websockets.connect


async def bridge(client: WebSocket, http_base: str, src: str,
                 allowed: set[str] | frozenset[str]) -> None:
    """Accept `client` and pipe it to go2rtc's /api/ws for stream `src`, both
    ways, until either side closes. Refuses (closes, never contacts go2rtc) a
    stream that is not in `allowed` or a hub with no go2rtc configured."""
    if not http_base:   # no go2rtc here (DEMO, or cameras not set up): nothing to log
        await client.close(code=CLOSE_POLICY)
        return
    if src not in allowed:
        _warn_refused("not a configured stream", src,
                      "the player asked for a stream config.json does not show")
        await client.close(code=CLOSE_POLICY)
        return
    if not same_origin(client):
        # e.g. the hub behind something that rewrites Host: every tile goes
        # black, so say why, with the two values that disagree
        _warn_refused("cross-origin", src, "Origin %r is not Host %r" % (
            client.headers.get("origin"), client.headers.get("host")))
        await client.close(code=CLOSE_POLICY)
        return
    # go2rtc lists each viewer (its WebUI, /api/streams) by address and user
    # agent: pass the browser's, or every viewer shows up as the hub.
    headers = {}
    if client.client is not None:
        headers["X-Forwarded-For"] = client.client.host
    try:
        url = upstream_ws_url(http_base, src)
        upstream = await connect(url, open_timeout=OPEN_TIMEOUT,
                                 max_size=MAX_MESSAGE, compression=None,
                                 additional_headers=headers,
                                 user_agent_header=client.headers.get("user-agent"))
    except Exception as e:
        # warning: the tile stays black with nothing else to say why
        log.warning("go2rtc proxy: could not open the %s stream at %s: %s: %s",
                    src, http_base, type(e).__name__, e)
        # Accept first: a close before accept reaches a browser as a bare
        # handshake failure (HTTP 403), the same as a refusal. After accept
        # it sees 1011, "go2rtc is down", and the player retries.
        try:
            await client.accept()
            await client.close(code=CLOSE_UNAVAILABLE)
        except (RuntimeError, OSError, WebSocketDisconnect):
            pass   # the browser already left
        return
    pumps: list[asyncio.Task] = []
    try:
        await client.accept()
        pumps = [asyncio.create_task(_client_to_upstream(client, upstream)),
                 asyncio.create_task(_upstream_to_client(upstream, client))]
        done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            e = t.exception()
            if e is not None and not _normal_close(e):
                log.warning("go2rtc proxy: the %s stream ended on an error: %s: %s",
                            src, type(e).__name__, e)
    finally:
        # Always close go2rtc's side: an open upstream socket keeps a
        # consumer (and its buffers) alive inside go2rtc. Shielded, so it
        # still runs to the end when this bridge is itself being cancelled
        # (the server stopping, or the ASGI server giving up on the client).
        closing = asyncio.ensure_future(_teardown(pumps, upstream, client))
        _closing.add(closing)
        closing.add_done_callback(_closing.discard)
        await asyncio.shield(closing)


# Teardowns still running after their bridge was cancelled: a reference
# keeps each alive until it finishes.
_closing: set[asyncio.Future] = set()


async def _teardown(pumps: list[asyncio.Task], upstream, client: WebSocket) -> None:
    for t in pumps:
        t.cancel()
    await asyncio.gather(*pumps, return_exceptions=True)
    await upstream.close()
    await _close_quietly(client)


def _normal_close(e: BaseException) -> bool:
    """A browser leaving the page (however abruptly: tabs get killed) or
    go2rtc closing its socket cleanly: the normal end of every stream, not
    worth a log line. go2rtc dropping the socket WITHOUT a close (it crashed,
    or was OOM-killed) is not normal and is logged."""
    return isinstance(e, (WebSocketDisconnect, websockets.ConnectionClosedOK))


async def _client_to_upstream(client: WebSocket, upstream) -> None:
    while True:
        msg = await client.receive()
        if msg["type"] == "websocket.disconnect":
            return
        if msg.get("text") is not None:
            await upstream.send(msg["text"])
        elif msg.get("bytes") is not None:
            await upstream.send(msg["bytes"])


async def _upstream_to_client(upstream, client: WebSocket) -> None:
    async for data in upstream:
        if isinstance(data, str):
            await client.send_text(data)
        else:
            await client.send_bytes(data)


async def _close_quietly(client: WebSocket) -> None:
    try:
        await client.close()
    except (RuntimeError, OSError, WebSocketDisconnect):
        pass   # already closed or gone (the browser left first): nothing to close
