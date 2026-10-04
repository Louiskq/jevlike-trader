"""Local dashboard: FastAPI, one SSE stream, static page. Bound to 127.0.0.1 only.

The store (SQLite) is the single source of truth. Every number on the page comes from it:
P&L, fees and position from `Ledger.from_fills(store.fills())`, calibration from
`calibration.horizon_report`. The only live inputs are the optional `live` dict (current
mid, feed state, kill state) supplied by the running trader. A new stream connection starts
with a full snapshot, so a reconnect or a restart reloads history from SQLite.

`live`, if given, is a callable returning
{"mid": float | None, "feed": str, "kill_reason": str | None, "stopped": bool}.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse

from .calibration import horizon_report
from .ledger import Ledger
from .store import Store

HOST = "127.0.0.1"
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})  # Host header (DNS-rebinding guard)
HISTORY_DECISIONS = 300  # about 26 minutes at one decision per 5.3 s
HISTORY_FILLS = 100
RETRY_MS = 2000
HEADLINE_FIRST = (60, 30, 300)
STATIC = Path(__file__).parent / "static"
ASSETS = {
    "app.js": "text/javascript",
    "logic.js": "text/javascript",
    "style.css": "text/css",
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def _clean(x):
    """Non-finite floats become None so the JSON is strictly valid."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_clean(v) for v in x]
    return x


def _dumps(x) -> str:
    return json.dumps(_clean(x), allow_nan=False, separators=(",", ":"))


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {_dumps(data)}\n\n"


def build_summary(store: Store, live: dict | None) -> dict:
    ledger = Ledger.from_fills(store.fills())
    last = store.recent_decisions(1)
    last_decision = dict(last[0]) if last else None
    mid = live.get("mid") if live else None
    if mid is None or not (isinstance(mid, int | float) and math.isfinite(mid) and mid > 0):
        mid = last_decision["mid"] if last_decision else None
    flat = ledger.position == 0.0
    if mid is None and not flat:
        pnl_after = pnl_before = position_usd = None  # no price to mark an open position
    else:
        mark = mid if mid is not None else 0.0
        pnl_after, pnl_before = ledger.pnl(mark), ledger.pnl_before_fees(mark)
        position_usd = ledger.position * mark
    counts = store.decision_counts()
    kill_reason = live.get("kill_reason") if live else None
    return {
        "now": time.time(),
        "mid": mid,
        "position": ledger.position,
        "position_usd": position_usd,
        "pnl_after_fees": pnl_after,
        "pnl_before_fees": pnl_before,
        "fees": ledger.fees,
        "n_fills": len(store.fills()),
        "decisions": counts["total"],
        "holds": counts["holds"],
        "late_holds": counts["late"],
        "hold_rate": counts["holds"] / counts["total"] if counts["total"] else None,
        "decision": last_decision,
        "calibration": [
            {**r, "bins": [asdict(b) for b in r["bins"]]}
            for r in (horizon_report(store, h) for h in HEADLINE_FIRST)
        ],
        "feed": live["feed"] if live else "offline",
        "stopped": bool(live["stopped"]) if live else False,
        "kill": (
            {"state": "unknown", "reason": None}
            if live is None
            else {"state": "tripped", "reason": kill_reason}
            if kill_reason
            else {"state": "ok", "reason": None}
        ),
    }


def build_snapshot(store: Store, live: dict | None) -> dict:
    decisions = [dict(r) for r in store.recent_decisions(HISTORY_DECISIONS)]
    fills = [dict(r) for r in store.recent_fills(HISTORY_FILLS)]
    return {
        "summary": build_summary(store, live),
        "decisions": decisions,
        "fills": fills,
        "last_decision_id": decisions[-1]["id"] if decisions else 0,
        "last_fill_id": fills[-1]["id"] if fills else 0,
    }


async def _sliced_sleep(poll_s: float, should_stop: Callable[[], bool]) -> None:
    """Sleep up to poll_s but wake within 0.1 s of should_stop(), so shutdown never waits
    on a long poll."""
    end = time.monotonic() + poll_s
    while not should_stop():
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.1))


async def event_stream(
    db_path: str,
    live: Callable[[], dict | None] | None,
    should_stop: Callable[[], bool],
    poll_s: float,
    sleep=_sliced_sleep,
) -> AsyncIterator[str]:
    """SSE text chunks: a retry hint, a full snapshot, then every poll the new decisions and
    fills (by id cursor) and a fresh summary. Ends when should_stop() is true."""
    store = Store(db_path)
    try:
        yield f"retry: {RETRY_MS}\n\n"
        snap = build_snapshot(store, live() if live else None)
        yield _sse("snapshot", snap)
        last_decision, last_fill = snap["last_decision_id"], snap["last_fill_id"]
        while True:
            await sleep(poll_s, should_stop)
            if should_stop():  # re-check: the wait may have ended because of the stop
                return
            for row in store.decisions_after(last_decision):
                last_decision = row["id"]
                yield _sse("decision", dict(row))
            for row in store.fills_after(last_fill):
                last_fill = row["id"]
                yield _sse("fill", dict(row))
            yield _sse("summary", build_summary(store, live() if live else None))
    finally:
        store.close()


class _HostGuard:
    """Pure ASGI middleware: reject any request whose Host header is not loopback."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            raw = dict(scope["headers"]).get(b"host", b"").decode("latin-1")
            host = raw.rsplit(":", 1)[0] if not raw.startswith("[") and ":" in raw else raw
            if host not in ALLOWED_HOSTS:
                if scope["type"] == "http":
                    await Response("forbidden host", status_code=403)(scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


def create_app(
    db_path: str,
    live: Callable[[], dict | None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    poll_s: float = 1.0,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    headers = {"Content-Security-Policy": CSP, "Cache-Control": "no-cache"}

    def asset(name: str) -> Response:
        if name != "index.html" and name not in ASSETS:
            raise HTTPException(404)
        media = "text/html" if name == "index.html" else ASSETS[name]
        return Response((STATIC / name).read_bytes(), media_type=media, headers=headers)

    @app.get("/")
    def index() -> Response:
        return asset("index.html")

    @app.get("/static/{name}")
    def static(name: str) -> Response:
        return asset(name)

    @app.get("/api/state")
    def state() -> Response:
        store = Store(db_path)
        try:
            body = _dumps(build_snapshot(store, live() if live else None))
        finally:
            store.close()
        return Response(body, media_type="application/json", headers=headers)

    @app.get("/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            event_stream(db_path, live, should_stop, poll_s),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return _HostGuard(app)  # type: ignore[return-value]


def make_server(app, host: str = HOST, port: int = 8765) -> uvicorn.Server:
    """A uvicorn server bound to loopback. Any other host is refused here, in code."""
    if host != HOST:
        raise ValueError(f"the dashboard binds {HOST} only, got {host!r}")
    return uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
