"""End-to-end acceptance check against a running service.

Run with the service up:  python tests/acceptance.py [base_url]
"""
import io, json, sys, time, urllib.request, urllib.error
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"; C={"v":""}
def req(m,p,body=None,cookie=True):
    h={}
    if cookie and C["v"]: h["Cookie"]=C["v"]
    d=None
    if body is not None:
        d=json.dumps(body).encode(); h["Content-Type"]="application/json"
    r=urllib.request.Request(BASE+p,data=d,headers=h,method=m)
    try:
        with urllib.request.urlopen(r,timeout=30) as resp:
            sc=resp.headers.get("Set-Cookie")
            if sc: C["v"]=sc.split(";")[0]
            raw=resp.read()
            return resp.status, (json.loads(raw) if raw and "json" in resp.headers.get("Content-Type","") else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200]
ok=fail=0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok+=1; print(f"  PASS  {name} {detail}")
    else: fail+=1; print(f"  FAIL  {name} {detail}")

print("== auth ==")
s,_=req("GET","/api/auth/me",cookie=False); check("unauthenticated /me rejected", s==401, f"({s})")
s,_=req("POST","/api/auth/login-cookie",{"login_name":"admin","password":"admin12345"}); check("admin sign-in", s==200)
s,d=req("POST","/api/auth/login-cookie",{"login_name":"admin","password":"wrong"}); check("bad password rejected", s in (401,429), f"({s})")
ob=type(C["v"]); C2=C["v"]
req("POST","/api/auth/login-cookie",{"login_name":"observer","password":"observer12345"})
s,_=req("POST","/api/cameras",{"name":"x","address":"1.2.3.4"}); check("observer cannot create camera", s==403, f"({s})")
C["v"]=C2

print("== health/resources ==")
s,d=req("GET","/api/health"); check("health ready", s==200 and d.get("ready"))
s,d=req("GET","/api/resources"); check("resources exposed", s==200 and "cameras" in d)

print("== cameras ==")
s,d=req("GET","/api/cameras"); check("camera list", s==200 and isinstance(d.get("items"),list), f"({len(d.get('items',[]))} cams)")
s,d=req("POST","/api/cameras/test-address",{"stream_type":"rtsp","address":"127.0.0.1","port":554})
check("address probe returns a verdict", s==200 and "reachable" in d, f"(reachable={d.get('reachable')})")

print("== identities ==")
s,d=req("GET","/api/identities"); check("identity list", s==200, f"({d.get('total')} people)")
# resolve a real id instead of assuming a seeded row survives
s,il=req("GET","/api/identities?page_size=1")
_id = (il.get("items") or [{}])[0].get("id") if s==200 else None
if _id:
    s,d=req("GET",f"/api/identities/{_id}"); check("identity detail with refs", s==200 and "references" in d)
else:
    check("identity detail with refs", False, "(no identities to inspect)")

print("== events ==")
s,d=req("GET","/api/events?page=1&page_size=5"); check("event list", s==200 and "total" in d, f"({d.get('total')} events)")
eid = d["items"][0]["id"] if d.get("items") else None
if eid:
    s,d=req("GET",f"/api/events/{eid}"); check("event detail", s==200)
    s,d=req("GET",f"/api/events/{eid}/download"); check("event artefact download", s==200 and len(d)>1000, f"({len(d)//1024} KB)")
s,d=req("GET","/api/events?role=approve"); check("filter by role", s==200 and d.get("total",0)>0, f"({d.get('total')})")
s,d=req("GET","/api/events?gender=male"); check("filter by gender", s==200)
s,d=req("GET","/api/events?page=1&page_size=3&sort=score&order=desc"); check("sorting", s==200)

print("== settings / users / zones / audit ==")
s,d=req("GET","/api/settings"); check("settings read", s==200 and "match_threshold" in d.get("settings",{}))
s,d=req("PUT","/api/settings",{"match_threshold":0.36}); check("settings write", s==200)
s,d=req("GET","/api/users"); check("user list", s==200 and len(d.get("items",[]))>=2)
s,cl=req("GET","/api/cameras")
_cams=[c["id"] for c in (cl.get("items") or [])]
if _cams:
    s,d=req("GET",f"/api/zones/{_cams[0]}"); check("zones read", s==200 and "zones" in d)
else:
    check("zones read", False, "(no cameras registered)")
s,d=req("GET","/api/audit"); check("audit trail", s==200 and len(d.get("items",[]))>0, f"({len(d.get('items',[]))} rows)")

print("== live ==")
s,d=req("GET","/api/live/detections"); check("live detections", s==200 and "tracks" in d)
# any camera that produced a frame will do; an offline one answers a
# placeholder (correct behaviour, but not a JPEG frame)
_ok=None
for _c in (cl.get("items") or []):
    s,d=req("GET","/video/%d/snapshot?flavour=raw" % _c["id"])
    if s==200 and isinstance(d,(bytes,bytearray)) and d[:2]==bytes([255,216]):
        _ok=(_c["id"], len(d)); break
check("snapshot jpeg", _ok is not None, "(%d KB from camera %s)" % ((_ok[1]//1024 if _ok else 0), (_ok[0] if _ok else "-")))

print("== report ==")
s,d=req("GET","/api/events/export?page_size=3"); check("PDF export", s==200 and d[:4]==b"%PDF", f"({len(d)//1024} KB)")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
