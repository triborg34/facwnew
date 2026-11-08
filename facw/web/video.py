"""MJPEG delivery and artefact serving.

Shared-decode guarantee (spec 3.3): the camera is captured by exactly one
``CameraStream``.  RAW viewers pull from the capture buffer; ANNOTATED viewers
pull from the pipeline worker's annotated buffer.  Both increase the stream's
viewer reference count, so the device is released when the last viewer leaves,
no matter how many viewers are watching.
"""
from __future__ import annotations

import io
import time
from typing import Optional

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..core.logging_setup import get_logger
from ..storage.media import image_bytes
from .auth import current_user

log = get_logger("video")
router = APIRouter()

BOUNDARY = "facwframe"


def _placeholder(text: str, size=(640, 360)) -> np.ndarray:
    w, h = size
    img = np.full((h, w, 3), 28, np.uint8)
    cv2.putText(img, text[:46], (16, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 2, cv2.LINE_AA)
    return img


def _encode(frame: np.ndarray, quality: int = 70) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else b""


def _mjpeg(frames_iter) -> StreamingResponse:
    def gen():
        for chunk in frames_iter:
            if chunk is None:
                continue
            yield (
                b"--" + BOUNDARY.encode() + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(chunk)).encode() + b"\r\n\r\n" + chunk + b"\r\n"
            )

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/video/{camera_id}")
def video_stream(
    camera_id: int,
    request: Request,
    flavour: str = "annotated",
    user=Depends(current_user),
):
    app = request.app.state.app
    stream = app.streams.get(camera_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="camera not registered")

    # Observers get the RAW flavour: annotated output is for privileged users
    # (spec 7.2).  Admin may request either.
    if user.get("role") != "admin":
        flavour = "raw"
    want_annotated = flavour == "annotated"
    settings_mgr = app.settings
    # Quality of the RAW flavour.  The annotated flavour is already produced at
    # the snapshot quality and only needs a small reduction for the wire.
    self_quality = 72
    # Viewer streams are capped at ~15 fps: smooth to the eye, and keeps the
    # bandwidth of a 4-tile mosaic well under a megabyte per second.
    min_interval = 1.0 / 15.0

    def frames():
        stream.acquire(owner="viewer")
        try:
            last = -1
            last_send = 0.0
            while True:
                # Pace the stream.  Without this the loop pushes frames as fast
                # as the camera produces them (measured 74 frames / 1.9 MB in
                # 6s for one tile), which saturates both the wire and the
                # encoder when several cameras are open.
                now = time.monotonic()
                wait = min_interval - (now - last_send)
                if wait > 0:
                    time.sleep(wait)
                last_send = time.monotonic()
                if request.client is None:  # pragma: no cover
                    break
                try:
                    if want_annotated:
                        buf = app.pipeline.annotated(camera_id)
                        if buf is None:
                            yield _encode(_placeholder("camera not analysed"), 60)
                            time.sleep(0.5)
                            continue
                        got = buf.get(last, timeout=1.0)
                        if got is None:
                            yield _encode(_placeholder(f"waiting for analysis ({stream.status.state})"), 60)
                            continue
                        frame, seq = got
                        last = seq
                        yield image_bytes(frame, max(45, settings_mgr.get().snapshot_quality - 30), max_width=1280)
                    else:
                        got = stream.frames.get(last, timeout=1.0)
                        if got is None:
                            state = stream.status.state
                            msg = {
                                "offline": f"camera offline: {stream.status.last_error or 'unreachable'}",
                                "reconnecting": "camera reconnecting...",
                                "idle": "camera idle",
                            }.get(state, f"waiting for frames ({state})")
                            yield _encode(_placeholder(msg), 60)
                            continue
                        frame, seq, _ts = got
                        last = seq
                        yield image_bytes(frame, self_quality, max_width=1600)
                except GeneratorExit:  # client disconnected
                    break
                except Exception as exc:  # noqa: BLE001 - a broken stream must not kill the server
                    log.warning("stream error", extra={"camera": camera_id, "error": str(exc)})
                    time.sleep(0.3)
        finally:
            stream.release(owner="viewer")

    return _mjpeg(frames())


@router.get("/video/{camera_id}/snapshot")
def snapshot(camera_id: int, request: Request, flavour: str = "raw", user=Depends(current_user)):
    app = request.app.state.app
    stream = app.streams.get(camera_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="camera not registered")
    if user.get("role") != "admin":
        flavour = "raw"
    stream.acquire(owner="viewer")
    try:
        # Give the worker a brief moment to produce a frame, then fall back to a
        # placeholder.  Never block for seconds: the console polls one snapshot
        # per tile and a slow response would stall the whole mosaic.
        deadline = time.time() + (1.2 if flavour == "raw" else 0.45)
        while True:
            frame = app.latest_frame(camera_id, flavour)
            if frame is not None:
                ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                if ok:
                    return Response(
                        content=buf.tobytes(),
                        media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"},
                    )
            if time.time() >= deadline:
                break
            time.sleep(0.03)
    finally:
        stream.release(owner="viewer")
    # No frame (offline camera / finished file): answer with a placeholder so the
    # tile shows a calm "no signal" state instead of a broken image.
    label = f"{stream.name} - {stream.status.state}"
    ok, buf = cv2.imencode(".jpg", _placeholder(label))
    if not ok:
        raise HTTPException(status_code=503, detail="camera has no frame yet")
    return Response(
        content=buf.tobytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/media/{kind}/{name}")
def media_file(kind: str, name: str, request: Request, user=Depends(current_user)):
    """Serve a stored artefact with a safe, sanitised path."""
    app = request.app.state.app
    path = app.media.path_of(f"{kind}/{name}")
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    data = path.read_bytes()
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=600"},
    )
