"""End-to-end smoke test: boot the app, start it, exercise the fixed paths."""
import json
import os
import sys

sys.path.insert(0, r"D:\git\facwnew")

from fastapi.testclient import TestClient

from facw.core.app import App
from facw.web.server import bootstrap_admin, create_app

app = App()
bootstrap_admin(app)
api = create_app(app)
c = TestClient(api)

# sign in as the bootstrap admin
creds = {"login_name": "admin", "password": "admin12345"}
# TestClient does not persist Set-Cookie across requests by default; send the
# cookie explicitly so the same session is reused like a browser does.
r = c.post("/api/auth/login-cookie", json=creds)
tok = r.cookies.get("facw_session") or r.headers.get("set-cookie", "").split("facw_session=")[-1].split(";")[0]
print("login-cookie ok:", r.status_code)
c.cookies.set("facw_session", tok or "")
if r.status_code != 200:
    print("login failed", r.status_code, r.text[:300])
    sys.exit(1)
print("login ok, role =", r.json().get("user", {}).get("role"))

app.start()

h = c.get("/api/health/full").json()
print("health:", h.get("status"))
print("accelerator:", json.dumps(h.get("accelerator", {}), ensure_ascii=False))

# ---- cameras list
cams = c.get("/api/cameras").json()
print("cameras:", len(cams.get("items", [])))

# ---- live detections (fast path)
d = c.get("/api/live/detections")
print("live/detections:", d.status_code, "keys:", sorted(d.json().keys()))
etag = d.json().get("version")
d2 = c.get("/api/live/detections")
print("second call unchanged:", d2.json().get("changed"), "same version:", d2.json().get("version") == etag)

# ---- ONVIF describe against a dead address must degrade, not raise
r = c.post("/api/discovery/onvif", json={"address": "127.0.0.1", "port": 9, "timeout": 1.0})
print("onvif dead endpoint:", r.status_code, r.json().get("ok"), r.json().get("errors"))

# ---- probe-preview rejects junk
r = c.get("/api/cameras/probe-preview", params={"stream": "not-a-url"})
print("probe-preview junk:", r.status_code)

# ---- enrolment: upload a still as base64 ("capture from camera" path)
import base64

from facw.pipeline.onvif import OnvifClient  # noqa: E402

fixture = "D:/git/facwnew/tests/fixtures/largest_selfie.jpg"
raw = open(fixture, "rb").read()
data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()

det = c.post("/api/enrolment/detect-b64", json={"image": data_url})
print("detect-b64:", det.status_code, "faces:", len(det.json().get("faces", [])))

# exactly what the console sends: multipart with files/stills/faces/metadata
form = {
    "faces": json.dumps([0]),
    "stills": json.dumps([data_url]),
    "metadata": json.dumps({"name": "Smoke", "family_name": "Test", "role": "visitor", "gender": "male"}),
}
# the browser always sends FormData (multipart), even with no file parts
import httpx

files = [(k, (None, v)) for k, v in form.items()]
r = c.post("/api/enrolment/save", files=files)
print("enrolment/save (camera still):", r.status_code, r.text[:200])

# cleanup: remove the created identity so re-runs stay clean
if r.status_code == 200:
    ident_id = r.json().get("identity_id")
    print("created identity:", ident_id)
    if ident_id:
        print("cleanup:", c.delete(f"/api/identities/{ident_id}").status_code)

app.stop()
print("SMOKE OK")
