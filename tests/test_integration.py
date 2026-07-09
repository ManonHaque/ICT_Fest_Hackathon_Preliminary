"""Integration tests verifying the spec rules that the smoke test doesn't cover."""
import os
import tempfile

# Use a temp DB so tests are isolated.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _future_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


def _register(org: str, username: str, password: str = "pw12345"):
    r = client.post("/auth/register", json={"org_name": org, "username": username, "password": password})
    return r


def test_register_admin_then_member_duplicate():
    org = f"org-{datetime.now().timestamp()}"
    r = _register(org, "alice")
    assert r.status_code == 201
    assert r.json()["role"] == "admin"

    r = _register(org, "bob")
    assert r.status_code == 201
    assert r.json()["role"] == "member"

    # Duplicate username
    r = _register(org, "alice")
    assert r.status_code == 409
    assert r.json()["code"] == "USERNAME_TAKEN"


def test_access_token_lifetime():
    org = f"org-tok-{datetime.now().timestamp()}"
    _register(org, "tokuser")
    login = client.post("/auth/login", json={"org_name": org, "username": "tokuser", "password": "pw12345"})
    import jwt as pyjwt
    from app.config import JWT_SECRET, JWT_ALGORITHM
    payload = pyjwt.decode(login.json()["access_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert payload["exp"] - payload["iat"] == 900, f"got {payload['exp'] - payload['iat']}"


def test_refresh_single_use():
    org = f"org-ref-{datetime.now().timestamp()}"
    _register(org, "refuser")
    login = client.post("/auth/login", json={"org_name": org, "username": "refuser", "password": "pw12345"})
    rt = login.json()["refresh_token"]
    r1 = client.post("/auth/refresh", json={"refresh_token": rt})
    assert r1.status_code == 200
    r2 = client.post("/auth/refresh", json={"refresh_token": rt})
    assert r2.status_code == 401


def test_logout_revokes_access():
    org = f"org-lo-{datetime.now().timestamp()}"
    _register(org, "louser")
    login = client.post("/auth/login", json={"org_name": org, "username": "louser", "password": "pw12345"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # Works before logout
    r = client.get("/rooms", headers=headers)
    assert r.status_code == 200
    # Logout
    r = client.post("/auth/logout", headers=headers)
    assert r.status_code == 200
    # 401 after logout
    r = client.get("/rooms", headers=headers)
    assert r.status_code == 401


def test_booking_creation_and_pricing():
    org = f"org-bk-{datetime.now().timestamp()}"
    _register(org, "bkuser")
    login = client.post("/auth/login", json={"org_name": org, "username": "bkuser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}

    room = client.post("/rooms", json={"name": "R1", "capacity": 4, "hourly_rate_cents": 1500}, headers=H)
    assert room.status_code == 201
    rid = room.json()["id"]

    # 3 hours
    b = client.post("/bookings", json={
        "room_id": rid, "start_time": _future_iso(50), "end_time": _future_iso(53)
    }, headers=H)
    assert b.status_code == 201, b.json()
    assert b.json()["price_cents"] == 4500  # 1500 * 3


def test_past_start_rejected():
    org = f"org-past-{datetime.now().timestamp()}"
    _register(org, "puser")
    login = client.post("/auth/login", json={"org_name": org, "username": "puser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    # Start in the past
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    past_end = (datetime.now(timezone.utc) - timedelta(hours=0)).replace(microsecond=0).isoformat()
    r = client.post("/bookings", json={"room_id": room["id"], "start_time": past, "end_time": past_end}, headers=H)
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"


def test_end_before_start_rejected():
    org = f"org-end-{datetime.now().timestamp()}"
    _register(org, "euser")
    login = client.post("/auth/login", json={"org_name": org, "username": "euser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    # end before start
    r = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future_iso(60),
        "end_time": _future_iso(58),
    }, headers=H)
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"


def test_duration_too_long():
    org = f"org-dur-{datetime.now().timestamp()}"
    _register(org, "duser")
    login = client.post("/auth/login", json={"org_name": org, "username": "duser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    r = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future_iso(50),
        "end_time": _future_iso(60),  # 10 hours
    }, headers=H)
    assert r.status_code == 400


def test_back_to_back_bookings_allowed():
    org = f"org-b2b-{datetime.now().timestamp()}"
    _register(org, "b2buser")
    login = client.post("/auth/login", json={"org_name": org, "username": "b2buser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    s = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=50)
    b1 = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": s.isoformat(),
        "end_time": (s + timedelta(hours=2)).isoformat(),
    }, headers=H)
    assert b1.status_code == 201
    b2 = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": (s + timedelta(hours=2)).isoformat(),  # starts where b1 ended
        "end_time": (s + timedelta(hours=4)).isoformat(),
    }, headers=H)
    assert b2.status_code == 201, b2.json()


def test_overlap_rejected():
    org = f"org-ov-{datetime.now().timestamp()}"
    _register(org, "ovuser")
    login = client.post("/auth/login", json={"org_name": org, "username": "ovuser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    s = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=50)
    b1 = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": s.isoformat(),
        "end_time": (s + timedelta(hours=3)).isoformat(),
    }, headers=H)
    assert b1.status_code == 201
    b2 = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": (s + timedelta(hours=1)).isoformat(),  # overlaps b1
        "end_time": (s + timedelta(hours=4)).isoformat(),
    }, headers=H)
    assert b2.status_code == 409
    assert b2.json()["code"] == "ROOM_CONFLICT"


def test_quota_limit_three():
    org = f"org-q-{datetime.now().timestamp()}"
    _register(org, "quser")
    login = client.post("/auth/login", json={"org_name": org, "username": "quser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    # 4 different rooms so no conflict; all within the 24h quota window.
    rooms = [
        client.post("/rooms", json={"name": f"R{i}", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
        for i in range(4)
    ]
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    for i in range(3):
        r = client.post("/bookings", json={
            "room_id": rooms[i]["id"],
            "start_time": (base + timedelta(hours=i*3)).isoformat(),
            "end_time": (base + timedelta(hours=i*3 + 1)).isoformat(),
        }, headers=H)
        assert r.status_code == 201, f"booking {i} failed: {r.json()}"
    # 4th within 24h window -> quota exceeded
    r = client.post("/bookings", json={
        "room_id": rooms[3]["id"],
        "start_time": (base + timedelta(hours=10)).isoformat(),
        "end_time": (base + timedelta(hours=11)).isoformat(),
    }, headers=H)
    assert r.status_code == 409
    assert r.json()["code"] == "QUOTA_EXCEEDED"


def test_refund_tiers():
    # Tier 1: notice >= 48h -> 100%
    # Tier 2: 24h <= notice < 48h -> 50%
    # Tier 3: notice < 24h -> 0%
    from app.services.refunds import compute_refund_cents
    # Spec test case
    assert compute_refund_cents(1001, 50) == 501
    assert compute_refund_cents(1000, 100) == 1000
    assert compute_refund_cents(1000, 0) == 0


def test_cancel_refund_full():
    org = f"org-cr-{datetime.now().timestamp()}"
    _register(org, "cruser")
    login = client.post("/auth/login", json={"org_name": org, "username": "cruser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    # 50h from now -> >48h notice -> 100%
    b = client.post("/bookings", json={
        "room_id": room["id"], "start_time": _future_iso(50), "end_time": _future_iso(52)
    }, headers=H).json()
    c = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert c.status_code == 200
    assert c.json()["refund_percent"] == 100
    assert c.json()["refund_amount_cents"] == 2000


def test_cancel_refund_zero_for_short_notice():
    org = f"org-cz-{datetime.now().timestamp()}"
    _register(org, "czuser")
    login = client.post("/auth/login", json={"org_name": org, "username": "czuser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    # 2h from now -> <24h notice -> 0%
    b = client.post("/bookings", json={
        "room_id": room["id"], "start_time": _future_iso(2), "end_time": _future_iso(3)
    }, headers=H).json()
    c = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert c.status_code == 200
    assert c.json()["refund_percent"] == 0
    assert c.json()["refund_amount_cents"] == 0


def test_double_cancel_rejected():
    org = f"org-dc-{datetime.now().timestamp()}"
    _register(org, "dcuser")
    login = client.post("/auth/login", json={"org_name": org, "username": "dcuser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    b = client.post("/bookings", json={
        "room_id": room["id"], "start_time": _future_iso(50), "end_time": _future_iso(52)
    }, headers=H).json()
    c1 = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert c1.status_code == 200
    c2 = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert c2.status_code == 409
    assert c2.json()["code"] == "ALREADY_CANCELLED"


def test_list_pagination_ordering():
    org = f"org-pg-{datetime.now().timestamp()}"
    _register(org, "pguser")
    login = client.post("/auth/login", json={"org_name": org, "username": "pguser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=50)
    # 5 bookings, spaced 5h apart
    ids = []
    for i in range(5):
        b = client.post("/bookings", json={
            "room_id": room["id"],
            "start_time": (base + timedelta(hours=i*5)).isoformat(),
            "end_time": (base + timedelta(hours=i*5 + 1)).isoformat(),
        }, headers=H)
        ids.append(b.json()["id"])
    # Page 1, limit 2
    r1 = client.get("/bookings?page=1&limit=2", headers=H).json()
    assert len(r1["items"]) == 2
    assert r1["total"] == 5
    # Ascending by start_time
    times = [it["start_time"] for it in r1["items"]]
    assert times == sorted(times), f"not sorted: {times}"
    # Page 2
    r2 = client.get("/bookings?page=2&limit=2", headers=H).json()
    assert len(r2["items"]) == 2
    page1_ids = {it["id"] for it in r1["items"]}
    page2_ids = {it["id"] for it in r2["items"]}
    assert page1_ids.isdisjoint(page2_ids)
    # Page 3 has the last one
    r3 = client.get("/bookings?page=3&limit=2", headers=H).json()
    assert len(r3["items"]) == 1


def test_cross_org_isolation():
    org_a = f"org-a-{datetime.now().timestamp()}"
    org_b = f"org-b-{datetime.now().timestamp()}"
    _register(org_a, "aadmin")
    _register(org_b, "badmin")
    la = client.post("/auth/login", json={"org_name": org_a, "username": "aadmin", "password": "pw12345"}).json()
    lb = client.post("/auth/login", json={"org_name": org_b, "username": "badmin", "password": "pw12345"}).json()
    Ha = {"Authorization": f"Bearer {la['access_token']}"}
    Hb = {"Authorization": f"Bearer {lb['access_token']}"}
    # Admin A creates a room
    room = client.post("/rooms", json={"name": "AR", "capacity": 4, "hourly_rate_cents": 1000}, headers=Ha).json()
    rid = room["id"]
    # Admin B cannot see it
    r = client.get(f"/rooms/{rid}/availability?date=2030-01-01", headers=Hb)
    assert r.status_code == 404
    # Admin B cannot create a booking in it
    r = client.post("/bookings", json={
        "room_id": rid, "start_time": _future_iso(50), "end_time": _future_iso(52)
    }, headers=Hb)
    assert r.status_code == 404


def test_reference_code_format_and_uniqueness():
    org = f"org-rc-{datetime.now().timestamp()}"
    _register(org, "rcuser")
    login = client.post("/auth/login", json={"org_name": org, "username": "rcuser", "password": "pw12345"})
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=H).json()
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=50)
    codes = []
    for i in range(3):
        b = client.post("/bookings", json={
            "room_id": room["id"],
            "start_time": (base + timedelta(hours=i*5)).isoformat(),
            "end_time": (base + timedelta(hours=i*5 + 1)).isoformat(),
        }, headers=H).json()
        assert b["reference_code"].startswith("CW-")
        codes.append(b["reference_code"])
    assert len(set(codes)) == 3, f"duplicate codes: {codes}"


def test_member_cannot_see_other_member_booking():
    org = f"org-pr-{datetime.now().timestamp()}"
    _register(org, "pradmin")
    la = client.post("/auth/login", json={"org_name": org, "username": "pradmin", "password": "pw12345"}).json()
    Ha = {"Authorization": f"Bearer {la['access_token']}"}
    room = client.post("/rooms", json={"name": "R", "capacity": 4, "hourly_rate_cents": 1000}, headers=Ha).json()
    # Admin creates a booking (acts as the admin, so user_id is admin's id)
    b = client.post("/bookings", json={
        "room_id": room["id"], "start_time": _future_iso(50), "end_time": _future_iso(52)
    }, headers=Ha).json()
    bid = b["id"]
    # Add a member to the same org
    _register(org, "prmember")
    lm = client.post("/auth/login", json={"org_name": org, "username": "prmember", "password": "pw12345"}).json()
    Hm = {"Authorization": f"Bearer {lm['access_token']}"}
    # Member cannot see admin's booking
    r = client.get(f"/bookings/{bid}", headers=Hm)
    assert r.status_code == 404
    # Member cannot cancel it
    r = client.post(f"/bookings/{bid}/cancel", headers=Hm)
    assert r.status_code == 404
    # Admin can see and cancel
    r = client.get(f"/bookings/{bid}", headers=Ha)
    assert r.status_code == 200
