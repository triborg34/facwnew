"""Multipart form parsing with a part size that fits the payload.

Starlette's form parser rejects any single *field* larger than 1 MB.  The
enrolment form carries a base64 copy of the reviewed camera frame in a text
field, and a 1280px JPEG is routinely 1.5-2 MB of base64 - so every "save
person from camera" failed with ``Part exceeded maximum size of 1024KB`` no
matter what the operator did.  Uploading the same picture as a *file* worked,
which is why the bug looked like "only the camera button is broken".

The limit is enforced inside ``MultiPartParser``, which Starlette constructs
with a hard-coded default.  FastAPI parses the form itself before resolving the
route's dependencies, so the only place to act is the ASGI layer.  This
middleware:

1. reads the whole body (bounded by ``MAX_BODY_BYTES``) for the enrolment
   routes only,
2. raises ``MultiPartParser.max_part_size`` for the duration of the request -
   the class attribute Starlette uses as the default limit,
3. replays the body to the application, which then parses it normally with the
   wide limit in place.

Patching a third-party default is not pretty, but it is contained: the patch is
applied per request, restored in ``finally``, and it does not change any
parsing behaviour other than the size guard.
"""
from __future__ import annotations

import asyncio

from starlette.formparsers import FormParser, MultiPartParser
from starlette.types import ASGIApp, Receive, Scope, Send

# A camera still plus metadata; handlers keep their own 12 MB ceiling for files.
MAX_FIELD_BYTES = 12 * 1024 * 1024
MAX_BODY_BYTES = 40 * 1024 * 1024

# Routes whose multipart fields may carry a camera still.
WIDE_FORM_PATHS = ("/enrolment/save", "/enrolment/detect", "/enrolment/camera-save")

_patch_lock = asyncio.Lock()


class LargeEnrolmentForm:
    """ASGI middleware: wide multipart limits on the enrolment endpoints."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not any(path.endswith(p) for p in WIDE_FORM_PATHS):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if "multipart/form-data" not in headers.get("content-type", "").lower():
            await self.app(scope, receive, send)
            return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            more = bool(message.get("more_body"))
            if len(body) > MAX_BODY_BYTES:
                await _json(send, 413, "upload too large")
                return

        sent = False

        async def replay() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            # after the replayed body the client is done; wait for disconnect
            return await receive()

        # ``Request._get_form`` passes ``max_part_size=1024*1024`` explicitly
        # and the guard compares against ``self.max_part_size``, so neither the
        # class attribute nor ``__init__`` alone is enough: the instance value
        # is forced after construction.  Run the body replay under the patch.
        old_mp_init = MultiPartParser.__init__
        old_fp_init = FormParser.__init__

        def wide_mp_init(parser_self, *args, **kwargs):  # type: ignore[no-untyped-def]
            old_mp_init(parser_self, *args, **kwargs)
            parser_self.max_part_size = MAX_FIELD_BYTES

        def wide_fp_init(parser_self, *args, **kwargs):  # type: ignore[no-untyped-def]
            old_fp_init(parser_self, *args, **kwargs)
            parser_self.max_part_size = MAX_FIELD_BYTES

        async with _patch_lock:
            MultiPartParser.__init__ = wide_mp_init  # type: ignore[method-assign]
            FormParser.__init__ = wide_fp_init  # type: ignore[method-assign]
            try:
                await self.app(scope, replay, send)
            finally:
                MultiPartParser.__init__ = old_mp_init  # type: ignore[method-assign]
                FormParser.__init__ = old_fp_init  # type: ignore[method-assign]


async def _json(send: Send, status: int, detail: str) -> None:
    import json

    payload = json.dumps({"detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
    })
    await send({"type": "http.response.body", "body": payload})
