"""REST API - the programmatic interface used by the console and helper tools.

Every route is authenticated; admin-only routes depend on ``require_admin``.
No open endpoints (spec 9.3).
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, Response, UploadFile

from ..core.logging_setup import get_logger
from ..core.security import hash_password
from ..pipeline.capture import build_stream_address, probe_host
from ..pipeline.discovery import fetch_onvif_stream_url, local_ipv4_networks
from ..pipeline.zones import Zone
from .auth import current_user, require_admin, resolve_session, sign_in, sign_out

log = get_logger("api")
router = APIRouter(prefix="/api")


def _app(request: Request):
    return request.app.state.app


# ==========================================================================
# Auth
# ==========================================================================
@router.post("/auth/login")
def login(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    app = _app(request)
    ua = request.headers.get("user-agent", "")
    return sign_in(app, str(payload.get("login_name", "")).strip(), str(payload.get("password", "")), ua)


@router.post("/auth/login-cookie")
def login_cookie(request: Request, response: Response, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Sign-in that also sets the HttpOnly session cookie used by the UI."""
    app = _app(request)
    result = sign_in(app, str(payload.get("login_name", "")).strip(), str(payload.get("password", "")), request.headers.get("user-agent", ""))
    from .auth import SESSION_COOKIE, SESSION_TTL_SECONDS

    response.set_cookie(
        SESSION_COOKIE,
        result["token"],
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=str(request.url.scheme) == "https",
        path="/",
    )
    return {"user": result["user"], "expires_in": result["expires_in"]}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> Dict[str, Any]:
    from .auth import SESSION_COOKIE

    sign_out(_app(request), request)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request, user=Depends(current_user)) -> Dict[str, Any]:
    app = _app(request)
    return {"user": user, "stream_token": app.sealer.sign_token("stream", str(user["id"]), 3600)}


# ==========================================================================
# Health / resources
# ==========================================================================
@router.get("/health")
def health(request: Request) -> Dict[str, Any]:
    # Deliberately unauthenticated: liveness probes must always answer.  It
    # exposes no operational data beyond a boolean status.
    app = _app(request)
    h = app.health()
    return {"status": h["status"], "ready": h["ready"], "uptime_seconds": h["uptime_seconds"]}


@router.get("/health/full")
def health_full(request: Request, user=Depends(current_user)) -> Dict[str, Any]:
    return _app(request).health()


@router.get("/resources")
def resources(request: Request, user=Depends(current_user)) -> Dict[str, Any]:
    return _app(request).resources()


# ==========================================================================
# Cameras
# ==========================================================================
def _validate_camera(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = str(payload.get("name", "")).strip()
    address = str(payload.get("address", "")).strip()
    if not name:
        raise HTTPException(status_code=422, detail="camera name is required")
    if not address:
        raise HTTPException(status_code=422, detail="camera address is required")
    try:
        raw_port = payload.get("port")
        port = int(raw_port) if raw_port not in (None, "") else 554
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="port must be a number") from None
    if not (0 <= port <= 65535):
        raise HTTPException(status_code=422, detail="port out of range")
    st = str(payload.get("stream_type") or "rtsp").lower()
    if st not in ("rtsp", "http", "https", "webcam", "file"):
        raise HTTPException(status_code=422, detail="unsupported stream type")
    stream_address = str(payload.get("stream_address") or "").strip()
    if not stream_address:
        # Every source type needs a resolved address: network types get a
        # composed URL, file/webcam sources keep the operator's path/index
        # verbatim (previously these were left empty and the camera worker
        # had nothing to open).
        stream_address = build_stream_address(
            st, address, port, str(payload.get("profile_name") or "main"),
            str(payload.get("username") or ""), str(payload.get("password") or ""),
        )
    return {
        "name": name,
        "address": address,
        "port": port,
        "profile_name": str(payload.get("profile_name") or "main"),
        "stream_address": stream_address,
        "stream_type": st,
        "gate_label": str(payload.get("gate_label") or ""),
        "username": str(payload.get("username") or ""),
        "password": str(payload.get("password") or ""),
        "analysis_enabled": bool(payload.get("analysis_enabled", True)),
    }


@router.get("/cameras")
def list_cameras(request: Request, user=Depends(current_user)) -> Dict[str, Any]:
    app = _app(request)
    status = {s["camera_id"]: s for s in app.streams.all_status()}
    cams = app.cameras.list()
    for c in cams:
        st = status.get(int(c["id"]), {})
        c["status"] = st.get("state", "idle")
        c["fps"] = st.get("fps", 0.0)
        c["viewers"] = st.get("viewers", 0)
    return {"items": cams}


@router.post("/cameras")
def create_camera(request: Request, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    data = _validate_camera(payload)
    cid = app.cameras.create(data)
    app.resync_cameras()
    app.audit.record("camera_created", actor=user["login_name"], target=str(cid), detail=data["name"])
    return {"id": cid, "camera": app.cameras.get(cid)}


@router.put("/cameras/{camera_id}")
def update_camera(request: Request, camera_id: int, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    if app.cameras.get(camera_id) is None:
        raise HTTPException(status_code=404, detail="camera not found")
    data = _validate_camera(payload)
    app.cameras.update(camera_id, data)
    app.resync_cameras()
    app.audit.record("camera_updated", actor=user["login_name"], target=str(camera_id))
    return {"camera": app.cameras.get(camera_id)}


@router.delete("/cameras/{camera_id}")
def delete_camera(request: Request, camera_id: int, user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    cam = app.cameras.get(camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    stream = app.streams.get(camera_id)
    ok = app.cameras.delete(camera_id)
    app.resync_cameras()
    app.zones.delete_for_camera(cam.get("address", str(camera_id)))
    app.audit.record("camera_deleted", actor=user["login_name"], target=str(camera_id), detail=cam.get("name", ""))
    return {"ok": ok}


@router.post("/cameras/{camera_id}/check")
def check_camera(request: Request, camera_id: int, user=Depends(current_user)) -> Dict[str, Any]:
    """Connectivity health check before opening a feed (spec 3.4)."""
    app = _app(request)
    cam = app.cameras.get(camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    host = cam.get("address", "")
    ok = probe_host(host, int(cam.get("port") or 554)) if cam.get("stream_type") != "file" else True
    return {"reachable": ok, "host": host, "port": cam.get("port"), "detail": "" if ok else "host did not answer"}


@router.post("/cameras/{camera_id}/restart")
def restart_camera(request: Request, camera_id: int, user=Depends(require_admin)) -> Dict[str, Any]:
    """Reopen a camera source (replays a finished file, retries a failed feed)."""
    app = _app(request)
    if app.cameras.get(camera_id) is None:
        raise HTTPException(status_code=404, detail="camera not found")
    stream = app.streams.get(camera_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="camera has no stream")
    stream.restart()
    app.audit.record(
        action="camera.restart",
        actor=str((user or {}).get("login_name") or ""),
        target=f"camera:{camera_id}",
        detail="source reopened",
    )
    return {"restarted": True, "camera_id": camera_id, "state": stream.status.state}


@router.post("/cameras/test-address")
def test_address(request: Request, payload: Dict[str, Any] = Body(...), user=Depends(current_user)) -> Dict[str, Any]:
    st = str(payload.get("stream_type") or "rtsp").lower()
    addr = str(payload.get("address") or "")
    port = int(payload.get("port") or 554)
    if st == "file":
        return {"reachable": os.path.exists(addr), "detail": "file exists" if os.path.exists(addr) else "file missing"}
    if st == "webcam":
        return {"reachable": True, "detail": "local device"}
    ok = probe_host(addr, port)
    return {"reachable": ok, "detail": "host answered" if ok else "host did not answer"}


# -- discovery -------------------------------------------------------------
@router.post("/discovery/start")
def discovery_start(request: Request, payload: Dict[str, Any] = Body(default={}), user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    subnets = payload.get("subnets") if isinstance(payload, dict) else None
    started = app.discovery.start(
        on_result=lambda d: app.db.bus.publish("discovery", {"device": d}),
        on_done=lambda n: app.db.bus.publish("discovery", {"done": True, "count": n}),
        subnets=subnets,
    )
    if not started:
        return {"started": False, "detail": app.discovery.progress, "devices": app.discovery.devices}
    app.audit.record("discovery_started", actor=user["login_name"])
    return {"started": True, "subnets": subnets or local_ipv4_networks()}


@router.post("/discovery/cancel")
def discovery_cancel(request: Request, user=Depends(require_admin)) -> Dict[str, Any]:
    _app(request).discovery.cancel()
    return {"ok": True}


@router.get("/discovery/results")
def discovery_results(request: Request, user=Depends(current_user)) -> Dict[str, Any]:
    app = _app(request)
    return {"devices": app.discovery.devices, "running": app.discovery.running, "progress": app.discovery.progress}


@router.post("/discovery/stream-uri")
def discovery_stream_uri(request: Request, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    """Fetch a device's live-stream address via its management interface."""
    url = str(payload.get("service_url") or "")
    result = fetch_onvif_stream_url(url, str(payload.get("username") or ""), str(payload.get("password") or ""))
    if not result:
        # graceful fallback: build the conventional address for the device
        result = build_stream_address(
            "rtsp", str(payload.get("address") or ""), int(payload.get("port") or 554),
            str(payload.get("profile_name") or "main"),
            str(payload.get("username") or ""), str(payload.get("password") or ""),
        )
        return {"stream_address": result, "source": "constructed"}
    return {"stream_address": result, "source": "onvif"}


# ==========================================================================
# Identities / enrolment
# ==========================================================================
@router.get("/identities")
def list_identities(
    request: Request,
    page: int = 1,
    page_size: int = 24,
    search: str = "",
    role: str = "",
    user=Depends(current_user),
) -> Dict[str, Any]:
    return _app(request).identities.list(page=page, page_size=page_size, search=search, role=role)


@router.get("/identities/{identity_id}")
def get_identity(request: Request, identity_id: int, user=Depends(current_user)) -> Dict[str, Any]:
    app = _app(request)
    ident = app.identities.get(identity_id)
    if ident is None:
        raise HTTPException(status_code=404, detail="identity not found")
    return {
        "identity": ident,
        "references": app.identities.list_refs(identity_id),
        "events": app.events.list({"name": ident.get("name", "")}, page=1, page_size=12),
    }


@router.delete("/identities/{identity_id}")
def delete_identity(request: Request, identity_id: int, user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    ok = app.enrolment.delete_identity(identity_id, actor=user["login_name"])
    if not ok:
        raise HTTPException(status_code=404, detail="identity not found")
    return {"ok": True}


def _decode_upload(data: bytes, filename: str) -> np.ndarray:
    app_limits = 12
    if len(data) == 0:
        raise HTTPException(status_code=422, detail="empty upload")
    if len(data) > app_limits * 1024 * 1024:
        raise HTTPException(status_code=413, detail="upload too large")
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".bmp", ".webp", ""):
        raise HTTPException(status_code=415, detail="unsupported file type")
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        raise HTTPException(status_code=422, detail="could not read image")
    return img


@router.post("/enrolment/detect")
async def enrolment_detect(request: Request, file: UploadFile = File(...), user=Depends(require_admin)) -> Dict[str, Any]:
    """Detect every face in an upload and return selectable candidates."""
    app = _app(request)
    data = await file.read()
    img = _decode_upload(data, file.filename or "")
    candidates = app.enrolment.detect_faces(img)
    if not candidates:
        # explicit, not a silent empty success (pitfall 7)
        return {"faces": [], "preview": app.enrolment.annotate_preview(img), "detail": "no face found"}
    return {"faces": [c.as_dict() for c in candidates], "preview": app.enrolment.annotate_preview(img)}


@router.post("/enrolment/capture")
def enrolment_capture(request: Request, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    """Grab a still from a registered camera for enrolment (spec 5.4)."""
    app = _app(request)
    cam_id = int(payload.get("camera_id") or 0)
    stream = app.streams.get(cam_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="camera not registered")
    stream.acquire()
    frame = None
    try:
        for _ in range(50):
            frame = stream.snapshot()
            if frame is not None:
                break
            time.sleep(0.1)
    finally:
        stream.release()
    if frame is None:
        raise HTTPException(status_code=503, detail="no frame available from camera")
    candidates = app.enrolment.detect_faces(frame)
    return {
        "faces": [c.as_dict() for c in candidates],
        "preview": app.enrolment.annotate_preview(frame),
        "detail": "" if candidates else "no face found in the captured still",
    }


@router.post("/enrolment/save")
async def enrolment_save(
    request: Request,
    files: List[UploadFile] = File(...),
    faces: str = Form("[]"),
    metadata: str = Form("{}"),
    identity_id: Optional[str] = Form(None),
    user=Depends(require_admin),
) -> Dict[str, Any]:
    app = _app(request)
    try:
        picked = json.loads(faces or "[]")
        meta = json.loads(metadata or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="malformed faces/metadata") from None
    if not isinstance(picked, list) or len(picked) != len(files):
        raise HTTPException(status_code=422, detail="one face selection per photo is required")
    images = [_decode_upload(await f.read(), f.filename or "") for f in files]
    try:
        target = int(identity_id) if identity_id not in (None, "", "null") else None
        result = app.enrolment.enrol(
            images, [int(p) for p in picked], meta, actor=user["login_name"], identity_id=target
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return result


@router.post("/enrolment/camera-save")
def enrolment_camera_save(request: Request, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    """Enrol from a still taken from a camera, picking one detected face."""
    app = _app(request)
    cam_id = int(payload.get("camera_id") or 0)
    face_index = int(payload.get("face_index") or 0)
    meta = payload.get("metadata") or {}
    stream = app.streams.get(cam_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="camera not registered")
    stream.acquire()
    frame = None
    try:
        for _ in range(50):
            frame = stream.snapshot()
            if frame is not None:
                break
            time.sleep(0.1)
    finally:
        stream.release()
    if frame is None:
        raise HTTPException(status_code=503, detail="no frame available from camera")
    try:
        result = app.enrolment.enrol([frame], [face_index], meta, actor=user["login_name"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return result


@router.post("/identities/{identity_id}/references")
async def add_reference(request: Request, identity_id: int, file: UploadFile = File(...), face_index: int = Form(0), user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    img = _decode_upload(await file.read(), file.filename or "")
    try:
        return app.enrolment.add_reference(identity_id, img, int(face_index), actor=user["login_name"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.delete("/identities/{identity_id}/references/{index}")
def remove_reference(request: Request, identity_id: int, index: int, user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    try:
        took = app.enrolment.remove_reference(identity_id, index, actor=user["login_name"])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"ok": True, "took_ms": round(took, 2), "references": app.identities.list_refs(identity_id)}


@router.put("/identities/{identity_id}")
def update_identity(request: Request, identity_id: int, payload: Dict[str, Any] = Body(...), user=Depends(require_admin)) -> Dict[str, Any]:
    app = _app(request)
    try:
        updated = app.identities.update(identity_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="identity not found")
    app.enrolment.refresh_index()
    app.audit.record("identity_updated", actor=user["login_name"], target=str(identity_id))
    return {"identity": updated}


@router.post("/identities/{identity_id}/describe")
def describe_identity(request: Request, identity_id: int, user=Depends(current_user)) -> Dict[str, Any]:
    """Description lookup used by the detection detail panel."""
    app = _app(request)
    ident = app.identities.get(identity_id)
    if ident is None:
        return {"description": "", "identity": None}
    return {"description": ident.get("description", ""), "identity": ident}


# ==========================================================================
# Events / reports
# ==========================================================================
def _filters_from_params(request: Request) -> Dict[str, Any]:
    q = request.query_params
    keys = (
        "name", "national_number", "gender", "role", "age_min", "age_max",
        "date_from", "date_to", "time_from", "time_to", "identified_only",
        "similarity_max", "camera_id", "sort", "order",
    )
    return {k: q.get(k) for k in keys if q.get(k) not in (None, "")}


@router.get("/events")
def list_events(
    request: Request,
    page: int = 1,
    page_size: int = 30,
    user=Depends(current_user),
) -> Dict[str, Any]:
    return _app(request).events.list(_filters_from_params(request), page=page, page_size=page_size)


# ORDERING NOTE: "/api/events/stream" (the live SSE feed) is declared in
# api_more, and api_more's router is included *before* this one.  FastAPI matches
# routes in registration order, so "/events/{event_id}" here must never be
# registered ahead of it, or the feed 422s trying to parse "stream" as an int.
@router.get("/events/export")
def export_events(request: Request, user=Depends(current_user)):
    """Printable multi-page report of the filtered result (spec 7.3)."""
    from .reports import build_report

    app = _app(request)
    filters = _filters_from_params(request)
    if app.settings.get().report_lock:
        # report-lock: an administrator password is required
        password = request.query_params.get("admin_password", "")
        admin = app.users.get_by_login(str(request.query_params.get("admin_login", "")), with_hash=True)
        from ..core.security import verify_password

        if not admin or admin["role"] != "admin" or not verify_password(password, admin["password_hash"]):
            raise HTTPException(status_code=403, detail="report lock: administrator password required")
        app.audit.record("report_export_unlocked", actor=admin["login_name"], detail=str(filters))
    rows = app.events.all_for_export(filters)
    pdf = build_report(rows, filters, app.media, meta={"generated": datetime.now()})
    app.audit.record("report_exported", actor=user["login_name"], detail=f"rows={len(rows)}")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="facw-report.pdf"'},
    )

@router.post("/events/search-image")
async def search_image(request: Request, file: UploadFile = File(...), limit: int = 20, user=Depends(current_user)) -> Dict[str, Any]:
    app = _app(request)
    img = _decode_upload(await file.read(), file.filename or "")
    try:
        return app.enrolment.search_by_image(img, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/events/{event_id}")
def get_event(request: Request, event_id: int, user=Depends(current_user)) -> Dict[str, Any]:
    app = _app(request)
    ev = app.events.get(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    identity = None
    if ev.get("identity_id"):
        identity = app.identities.get(int(ev["identity_id"]))
    elif ev.get("identity_name") and ev["identity_name"] != "unknown":
        identity = app.identities.get_by_name(ev["identity_name"])
    return {"event": ev, "identity": identity}


@router.delete("/events/{event_id}")
def delete_event(request: Request, event_id: int, user=Depends(require_admin)) -> Dict[str, Any]:
    """Single code path for event deletion (pitfall 20)."""
    app = _app(request)
    ev = app.events.delete(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    app.media.delete_event_artefacts(ev["frame_file"], ev["face_file"], ev["person_file"])
    app.audit.record("event_deleted", actor=user["login_name"], target=str(event_id))
    return {"ok": True}


@router.get("/events/{event_id}/download")
def download_event(request: Request, event_id: int, user=Depends(current_user)):
    app = _app(request)
    ev = app.events.get(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    path = app.media.path_of(ev.get("frame_file") or "")
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="stored frame missing")
    safe_name = f"{(ev.get('identity_name') or 'unknown').replace(' ', '_')}_{ev.get('event_date','')}_{(ev.get('event_time') or '').replace(':','')}.jpg"
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )

