"""End-to-end test of fix #22: cross-org room_id leakage via /admin/export."""
import sys, time
sys.path.insert(0, "/app")
from fastapi.testclient import TestClient
from app.main import app

c = TestClient(app)
suffix = str(int(time.time()))

# Org A
r = c.post("/auth/register", json={"org_name": f"orgA_{suffix}", "username": "adminA", "password": "pw1234567"})
assert r.status_code == 201, r.text
r = c.post("/auth/login", json={"org_name": f"orgA_{suffix}", "username": "adminA", "password": "pw1234567"})
assert r.status_code == 200, r.text
tokA = r.json()["access_token"]; hA = {"Authorization": "Bearer " + tokA}
r = c.post("/rooms", headers=hA, json={"name": "RoomA", "capacity": 4, "hourly_rate_cents": 1000})
assert r.status_code == 201, r.text
roomA_id = r.json()["id"]
start_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 30*3600))
end_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 31*3600))
r = c.post("/bookings", headers=hA, json={"room_id": roomA_id, "start_time": start_iso, "end_time": end_iso})
assert r.status_code == 201, r.text
bookingA_id = r.json()["id"]

# Org B
orgB = "orgB_" + suffix
r = c.post("/auth/register", json={"org_name": orgB, "username": "adminB", "password": "pw1234567"})
assert r.status_code == 201, r.text
r = c.post("/auth/login", json={"org_name": orgB, "username": "adminB", "password": "pw1234567"})
assert r.status_code == 200, r.text
tokB = r.json()["access_token"]; hB = {"Authorization": "Bearer " + tokB}
r = c.post("/rooms", headers=hB, json={"name": "RoomB", "capacity": 4, "hourly_rate_cents": 2000})
assert r.status_code == 201, r.text
roomB_id = r.json()["id"]
r = c.post("/bookings", headers=hB, json={"room_id": roomB_id, "start_time": start_iso, "end_time": end_iso})
assert r.status_code == 201, r.text
bookingB_id = r.json()["id"]

# Cross-org export: adminA asks for room B's data -- must be empty
r = c.get("/admin/export?room_id=" + str(roomB_id) + "&include_all=true", headers=hA)
assert r.status_code == 200, r.text
lines = [l for l in r.text.strip().split("\n")[1:] if l.strip()]
assert len(lines) == 0, "expected 0 rows, got " + str(lines)
assert str(bookingB_id) not in r.text, "LEAK: B booking " + str(bookingB_id) + " in A export"
print("FIX_22_OK: cross-org room_id export correctly excluded foreign booking")

# Regression: own-org export still works
r = c.get("/admin/export?room_id=" + str(roomA_id) + "&include_all=true", headers=hA)
assert r.status_code == 200, r.text
assert str(bookingA_id) in r.text, "A own room export missing booking A"
print("FIX_22_REGRESSION_OK: own-org export still works")
