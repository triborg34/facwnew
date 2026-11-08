"""Settings, users, zones, live change feed and NodeInfo-style admin routes."""
from __future__ import annotations

import asyncio
import json
import queue
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..core.config import RuntimeSettings
from ..pipeline.relay import HttpRelay, SimRelay
from .auth import current_user, require_admin

router = APIRouter(prefix="/api")

# Settings an observer may not read-modify and admin-only switches.
ADMIN_ONLY_KEYS = set(RuntimeSettings.__dataclass_fields__.keys())


def _app(request: Request):
    return request.app.state.app


# ==========================================================================
# Settings
# ==========================================================================
@router.get("/settings")
def get_settings(request: Request, user=Depends(current_user)) -> Dict[str, Any]:
    app = _app(request)
    return {"settings": app.settings.get_dict(), "index": app.index.stats()}


@router.put("/settings")
def update_settings(request: Request, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="settings payload must be an object")
    unknown = set(payload) - ADMIN_ONLY_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown settings: {sorted(unknown)}")
    before = app.settings.get_dict()
    after = app.settings.update(payload)
    changes = {k: after.as_dict()[k] for k in payload if before.get(k) != after.as_dict()[k]}
    return {"settings": after.as_dict(), "changed": changes}


@router.post("/settings/relay-test")
def relay_test(request: Request, user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    s = app.settings.get()
    return app.relay.test(s)


@router.post("/settings/relay-pulse")
def relay_pulse(request: Request, payload: Dict[str, Any] = Body(default={}), user=Depends(require_admin)) -> Dict[str, Any]:
    """Manual pulse so an operator can verify wiring."""
    app = _app(request)
    s = app.settings.get()
    channel = (payload or {}).get("channel") or ""
    results = app.relay.actuate(s, zone_channel=channel or None, reason=f"manual test by {user['login_name']}")
    app.audit.record("relay_manual", actor=user["login_name"], detail=str(results))
    return {"results": results}


@router.get("/settings/relay-log")
def relay_log(request: Request, user=Depends(current_user)) -> Dict[str, Any]:
    return {"items": _app(request).relay.recent(50)}


# ==========================================================================
# Users (admin only)
# ==========================================================================
@router.get("/users")
def list_users(request: Request, user=Depends(require_admin)) -> Dict[str, Any]:
    return {"items": _app(request).users.list()}


@router.post("/users")
def create_user(request: Request, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    if app.users.get_by_login(str(payload.get("login_name", ""))):
        raise HTTPException(status_code=409, detail="login name already exists")
    try:
        uid = app.users.create(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    app.audit.record("user_created", actor=user["login_name"], target=str(uid), detail=str(payload.get("login_name")))
    return {"user": app.users.get(uid)}


@router.put("/users/{user_id}")
def update_user(request: Request, user_id: int, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    if app.users.get(user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    try:
        updated = app.users.update(user_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    app.audit.record("user_updated", actor=user["login_name"], target=str(user_id))
    return {"user": updated}


@router.delete("/users/{user_id}")
def delete_user(request: Request, user_id: int, user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    if int(user["id"]) == int(user_id):
        raise HTTPException(status_code=409, detail="you cannot delete your own account")
    remaining_admins = [u for u in app.users.list() if u["role"] == "admin" and int(u["id"]) != int(user_id)]
    target = app.users.get(user_id)
    if target and target["role"] == "admin" and not remaining_admins:
        raise HTTPException(status_code=409, detail="cannot remove the last administrator")
    ok = app.users.delete(user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="user not found")
    app.audit.record("user_deleted", actor=user["login_name"], target=str(user_id))
    return {"ok": True}


# ==========================================================================
# Zones
# ==========================================================================
def _camera_key(app, camera_id: int) -> str:
    cam = app.cameras.get(camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    return str(cam.get("address") or camera_id)


@router.get("/zones/{camera_id}")
def get_zones(request: Request, camera_id: int, user=Depends(current_user)) -> Dict[str, Any]:
    app = _app(request)
    key = _camera_key(app, camera_id)
    return {"camera_key": key, "zones": app.zones.list_for_camera(key)}


@router.put("/zones/{camera_id}")
def put_zones(request: Request, camera_id: int, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    key = _camera_key(app, camera_id)
    zones = payload.get("zones") or []
    if not isinstance(zones, list):
        raise HTTPException(status_code=422, detail="zones must be a list")
    for z in zones:
        points = z.get("points") or []
        if len(points) < 2:
            raise HTTPException(status_code=422, detail="each zone needs at least two points")
        for p in points:
            if len(p) < 2 or not (0.0 <= float(p[0]) <= 1.0) or not (0.0 <= float(p[1]) <= 1.0):
                raise HTTPException(status_code=422, detail="zone points must be normalised 0..1")
    count = app.zones.replace_for_camera(key, zones)
    app.pipeline.reload_zones()
    app.audit.record("zones_saved", actor=user["login_name"], target=key, detail=f"count={count}")
    return {"ok": True, "count": count, "zones": app.zones.list_for_camera(key)}


@router.delete("/zones/{camera_id}")
def delete_zones(request: Request, camera_id: int, user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    key = _camera_key(app, camera_id)
    n = app.zones.delete_for_camera(key)
    app.pipeline.reload_zones()
    return {"ok": True, "deleted": n}


# ==========================================================================
# Live change feed (SSE)
# ==========================================================================
@router.get("/events/stream")
async def live_events(request: Request, topics: str = "events,identities,cameras,alarm,discovery"):
    """Server-sent events: every open console updates live.

    Authentication is resolved explicitly here because EventSource cannot set
    headers; the session cookie is used.
    """
    from .auth import resolve_session

    user = resolve_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    app = _app(request)
    wanted = {t.strip() for t in topics.split(",") if t.strip()}
    q = app.db.bus.subscribe()

    async def gen():
        try:
            # initial comment opens the stream immediately
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.15)
                    yield ": ping\n\n"
                    continue
                if msg["topic"] in wanted or not wanted:
                    yield f"data: {json.dumps(msg)}\n\n"
        finally:
            app.db.bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/live/detections")
def live_detections(
    request: Request,
    if_none_match: str = "",
    ttl: float = 2.5,
    user=Depends(current_user),
) -> Dict[str, Any]:
    """Current on-screen tracks per camera (analysing / identified / unknown).

    The console polls this several times a second.  ``version`` changes only
    when a box, a state or a vote actually moved, so a quiet scene answers
    ``changed: false`` with no payload and the live view repaints instantly
    when something does happen.
    """
    app = _app(request)
    out: List[Dict[str, Any]] = []
    version = 0
    for t in app.tracks.all_tracks():
        version = (version * 31 + t.track_id * 7 + int(t.updated_at * 1000) + len(t.votes) + (5 if t.committed else 0)) % 2_147_483_647
        ident = t.identity or {}
        out.append(
            {
                "track_id": t.track_id,
                "camera_key": t.camera_key,
                "state": t.state,
                "identity_name": ident.get("name", "") if t.committed else "",
                "identity_id": ident.get("identity_id") if t.committed else None,
                "score": round(float(ident.get("score", 0.0)), 3) if ident else 0.0,
                "zone": t.zone_name,
                "samples": t.sample_count,
                "votes": len(t.votes),
                "age": ident.get("age"),
                "gender": ident.get("gender", ""),
                "since": round(t.first_seen, 3),
                "updated": round(t.updated_at, 3),
                "stale": (time.time() - t.last_seen) > max(1.0, float(ttl or 2.5)),
                "bbox": [round(float(v), 1) for v in t.bbox],
            }
        )
    out.sort(key=lambda d: d["track_id"], reverse=True)
    payload = {"tracks": out, "count": len(out), "version": str(version), "ts": round(time.time(), 3)}
    if str(if_none_match or "") == str(version) and not app.tracks.count:
        return {"tracks": [], "count": len(out), "version": str(version), "changed": False}
    payload["changed"] = True
    return payload


@router.get("/live/frame/{camera_id}")
def live_frame(camera_id: int, request: Request, flavour: str = "annotated", user=Depends(current_user)) -> Response:
    """Newest frame of one camera as a single JPEG (no multipart overhead).

    Used by the console's live view: a small XHR loop is cheaper per frame than
    a multipart stream for mosaic tiles and keeps the browser's per-host
    connection budget free for the API.
    """
    from ..storage.media import image_bytes

    app = _app(request)
    if user.get("role") != "admin":
        flavour = "raw"
    frame = app.latest_frame(camera_id, flavour)
    if frame is None:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    data = image_bytes(frame, quality=78, max_width=1280)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


# ==========================================================================
# Audit
# ==========================================================================
@router.get("/audit")
def audit_log(request: Request, limit: int = 200, action: str = "", user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    return {"items": app.audit.list(limit=limit, action=action), "biometric": app.audit.biometric_log(limit)}


@router.post("/maintenance/backup")
def backup(request: Request, user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = app.paths.backup_dir / f"facw-{stamp}.db"
    app.db.backup_to(dest)
    app.audit.record("backup", actor=user["login_name"], detail=str(dest))
    return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size if dest.exists() else 0}


@router.post("/maintenance/retention")
def run_retention(request: Request, user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    days = app.settings.get().retention_days
    old = app.events.retention_purge(days)
    for ev in old:
        app.media.delete_event_artefacts(ev["frame_file"], ev["face_file"], ev["person_file"])
    app.audit.record("retention_purge", actor=user["login_name"], detail=f"days={days} removed={len(old)}")
    return {"ok": True, "removed": len(old), "days": days}


@router.post("/maintenance/rebuild-index")
def rebuild_index(request: Request, user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    rows = app.enrolment.refresh_index()
    return {"ok": True, "rows": rows, "about_seconds": 0}
