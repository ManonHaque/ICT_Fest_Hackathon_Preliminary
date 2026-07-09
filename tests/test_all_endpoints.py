"""Comprehensive tests for every endpoint listed in README.md.

Endpoints from the README contract:
- GET   /health                              (no auth)
- POST  /auth/register                       (no auth)
- POST  /auth/login                          (no auth)
- POST  /auth/refresh                        (no auth, refresh token in body)
- POST  /auth/logout                         (yes auth)
- GET   /rooms                               (yes auth)
- POST  /rooms                               (yes auth, admin)
- GET   /rooms/{id}/availability             (yes auth)
- GET   /rooms/{id}/stats                    (yes auth)
- POST  /bookings                            (yes auth)
- GET   /bookings                            (yes auth)
- GET   /bookings/{id}                       (yes auth)
- POST  /bookings/{id}/cancel                (yes auth)
- GET   /admin/usage-report                  (yes auth, admin)
- GET   /admin/export                        (yes auth, admin)
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

import io
import csv
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _future(hours: int) -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(hours=hours))


def _setup_org(prefix: str) -> dict:
    """Register an org admin, login, create a room. Return tokens and ids."""
    org = f"{prefix}-{datetime.now().timestamp()}"
    r = client.post("/auth/register", json={
        "org_name": org, "username": "admin", "password": "pw12345"
    })
    assert r.status_code == 201
    admin_id = r.json()["user_id"]
    org_id = r.json()["org_id"]

    login = client.post("/auth/login", json={
        "org_name": org, "username": "admin", "password": "pw12345"
    })
    assert login.status_code == 200
    access = login.json()["access_token"]
    refresh = login.json()["refresh_token"]

    # Add a member
    client.post("/auth/register", json={
        "org_name": org, "username": "member1", "password": "pw12345"
    })
    member_login = client.post("/auth/login", json={
        "org_name": org, "username": "member1", "password": "pw12345"
    })
    member_access = member_login.json()["access_token"]

    return {
        "org": org, "org_id": org_id, "admin_id": admin_id,
        "admin_token": access, "admin_refresh": refresh,
        "member_token": member_access,
    }


# =============================================================================
# /health
# =============================================================================

def test_health_no_auth_required():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# =============================================================================
# /auth/register
# =============================================================================

def test_register_admin_for_new_org():
    org = f"neworg-{datetime.now().timestamp()}"
    r = client.post("/auth/register", json={
        "org_name": org, "username": "boss", "password": "pw12345"
    })
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "admin"
    assert "user_id" in body
    assert "org_id" in body
    assert body["username"] == "boss"


def test_register_member_for_existing_org():
    org = f"existingorg-{datetime.now().timestamp()}"
    client.post("/auth/register", json={
        "org_name": org, "username": "boss", "password": "pw12345"
    })
    r = client.post("/auth/register", json={
        "org_name": org, "username": "member", "password": "pw12345"
    })
    assert r.status_code == 201
    assert r.json()["role"] == "member"


def test_register_duplicate_username_409():
    s = _setup_org("dup")
    r = client.post("/auth/register", json={
        "org_name": s["org"], "username": "admin", "password": "pw12345"
    })
    assert r.status_code == 409
    assert r.json()["code"] == "USERNAME_TAKEN"


# =============================================================================
# /auth/login
# =============================================================================

def test_login_success():
    s = _setup_org("login")
    r = client.post("/auth/login", json={
        "org_name": s["org"], "username": "admin", "password": "pw12345"
    })
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


def test_login_bad_password_401():
    s = _setup_org("loginbad")
    r = client.post("/auth/login", json={
        "org_name": s["org"], "username": "admin", "password": "wrong"
    })
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CREDENTIALS"


def test_login_unknown_user_401():
    r = client.post("/auth/login", json={
        "org_name": "nosuchorg", "username": "x", "password": "y"
    })
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CREDENTIALS"


# =============================================================================
# /auth/refresh
# =============================================================================

def test_refresh_returns_new_tokens():
    s = _setup_org("ref")
    r = client.post("/auth/refresh", json={"refresh_token": s["admin_refresh"]})
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


def test_refresh_old_token_revoked():
    s = _setup_org("refrev")
    old = s["admin_refresh"]
    r1 = client.post("/auth/refresh", json={"refresh_token": old})
    assert r1.status_code == 200
    r2 = client.post("/auth/refresh", json={"refresh_token": old})
    assert r2.status_code == 401


def test_refresh_with_access_token_rejected():
    s = _setup_org("refacc")
    r = client.post("/auth/refresh", json={"refresh_token": s["admin_token"]})
    assert r.status_code == 401


def test_refresh_invalid_token_401():
    r = client.post("/auth/refresh", json={"refresh_token": "not.a.jwt"})
    assert r.status_code == 401


# =============================================================================
# /auth/logout
# =============================================================================

def test_logout_revokes_access_token():
    s = _setup_org("lo")
    token = s["admin_token"]
    H = {"Authorization": f"Bearer {token}"}
    # works before logout
    assert client.get("/rooms", headers=H).status_code == 200
    # logout
    r = client.post("/auth/logout", headers=H)
    assert r.status_code == 200
    # 401 after
    assert client.get("/rooms", headers=H).status_code == 401


def test_logout_requires_auth():
    r = client.post("/auth/logout")
    assert r.status_code == 401


def test_logout_invalidates_for_all_subsequent_uses():
    s = _setup_org("lo2")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    client.post("/auth/logout", headers=H)
    # every subsequent request with that token must fail
    for _ in range(3):
        assert client.get("/rooms", headers=H).status_code == 401


# =============================================================================
# /rooms  (list & create)
# =============================================================================

def test_list_rooms_only_caller_org():
    s1 = _setup_org("list1")
    s2 = _setup_org("list2")
    # create rooms
    H1 = {"Authorization": f"Bearer {s1['admin_token']}"}
    H2 = {"Authorization": f"Bearer {s2['admin_token']}"}
    client.post("/rooms", json={"name": "R1a", "capacity": 4, "hourly_rate_cents": 1000}, headers=H1)
    client.post("/rooms", json={"name": "R1b", "capacity": 6, "hourly_rate_cents": 1500}, headers=H1)
    client.post("/rooms", json={"name": "R2a", "capacity": 4, "hourly_rate_cents": 1000}, headers=H2)

    r = client.get("/rooms", headers=H1)
    assert r.status_code == 200
    rooms = r.json()
    assert len(rooms) == 2
    names = {r_["name"] for r_ in rooms}
    assert names == {"R1a", "R1b"}


def test_list_rooms_requires_auth():
    r = client.get("/rooms")
    assert r.status_code == 401


def test_create_room_as_admin():
    s = _setup_org("create")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.post("/rooms", json={
        "name": "Big Room", "capacity": 12, "hourly_rate_cents": 2500
    }, headers=H)
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Big Room"
    assert body["capacity"] == 12
    assert body["hourly_rate_cents"] == 2500
    assert body["org_id"] == s["org_id"]


def test_create_room_as_member_forbidden():
    s = _setup_org("crmem")
    H = {"Authorization": f"Bearer {s['member_token']}"}
    r = client.post("/rooms", json={
        "name": "Nope", "capacity": 4, "hourly_rate_cents": 1000
    }, headers=H)
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


# =============================================================================
# /rooms/{id}/availability
# =============================================================================

def _create_room(s, name="R", rate=1000):
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.post("/rooms", json={"name": name, "capacity": 4, "hourly_rate_cents": rate}, headers=H)
    return r.json()


def test_availability_returns_confirmed_busy_intervals():
    s = _setup_org("av1")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    # create a booking 50h from now
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H).json()
    # get availability on that date
    target_date = (datetime.now(timezone.utc) + timedelta(hours=50)).date().isoformat()
    r = client.get(f"/rooms/{room['id']}/availability?date={target_date}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["room_id"] == room["id"]
    assert body["date"] == target_date
    assert len(body["busy"]) == 1
    assert body["busy"][0]["start_time"].endswith("+00:00")
    assert body["busy"][0]["end_time"].endswith("+00:00")


def test_availability_excludes_cancelled():
    s = _setup_org("av2")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H).json()
    # cancel
    client.post(f"/bookings/{b['id']}/cancel", headers=H)
    target_date = (datetime.now(timezone.utc) + timedelta(hours=50)).date().isoformat()
    r = client.get(f"/rooms/{room['id']}/availability?date={target_date}", headers=H)
    assert r.status_code == 200
    assert r.json()["busy"] == []


def test_availability_cross_org_404():
    s1 = _setup_org("avco1")
    s2 = _setup_org("avco2")
    room = _create_room(s1)
    H2 = {"Authorization": f"Bearer {s2['admin_token']}"}
    r = client.get(f"/rooms/{room['id']}/availability?date=2030-01-01", headers=H2)
    assert r.status_code == 404
    assert r.json()["code"] == "ROOM_NOT_FOUND"


def test_availability_invalid_date_400():
    s = _setup_org("avinv")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.get(f"/rooms/{room['id']}/availability?date=notadate", headers=H)
    assert r.status_code == 400


# =============================================================================
# /rooms/{id}/stats
# =============================================================================

def test_stats_initial_zero():
    s = _setup_org("stats1")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.get(f"/rooms/{room['id']}/stats", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["room_id"] == room["id"]
    assert body["total_confirmed_bookings"] == 0
    assert body["total_revenue_cents"] == 0


def test_stats_after_create_and_cancel():
    s = _setup_org("stats2")
    room = _create_room(s, rate=1000)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H).json()
    r = client.get(f"/rooms/{room['id']}/stats", headers=H).json()
    assert r["total_confirmed_bookings"] == 1
    assert r["total_revenue_cents"] == 2000
    # cancel
    client.post(f"/bookings/{b['id']}/cancel", headers=H)
    r = client.get(f"/rooms/{room['id']}/stats", headers=H).json()
    assert r["total_confirmed_bookings"] == 0
    assert r["total_revenue_cents"] == 0


def test_stats_cross_org_404():
    s1 = _setup_org("statsco1")
    s2 = _setup_org("statsco2")
    room = _create_room(s1)
    H2 = {"Authorization": f"Bearer {s2['admin_token']}"}
    r = client.get(f"/rooms/{room['id']}/stats", headers=H2)
    assert r.status_code == 404


# =============================================================================
# /bookings  (create)
# =============================================================================

def test_create_booking_response_shape():
    s = _setup_org("bkshape")
    room = _create_room(s, rate=1500)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(53),
    }, headers=H)
    assert r.status_code == 201
    body = r.json()
    expected_keys = {"id", "reference_code", "room_id", "user_id", "start_time",
                     "end_time", "status", "price_cents", "created_at"}
    assert set(body.keys()) == expected_keys
    assert body["status"] == "confirmed"
    assert body["price_cents"] == 4500  # 1500 * 3
    assert body["start_time"].endswith("+00:00")


def test_create_booking_naive_input_treated_as_utc():
    s = _setup_org("bknv")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    naive = (datetime.now(timezone.utc) + timedelta(hours=50)).replace(tzinfo=None)
    r = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": naive.isoformat(),
        "end_time": (naive + timedelta(hours=1)).isoformat(),
    }, headers=H)
    assert r.status_code == 201


def test_create_booking_with_offset_converted_to_utc():
    s = _setup_org("bkoff")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    # 2025-06-15T12:00:00-05:00 == 17:00 UTC; book that
    target_utc = (datetime.now(timezone.utc) + timedelta(days=30)).replace(
        hour=17, minute=0, second=0, microsecond=0
    )
    plus_minus_5 = target_utc.astimezone(timezone(timedelta(hours=-5)))
    r = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": plus_minus_5.isoformat(),
        "end_time": (plus_minus_5 + timedelta(hours=1)).isoformat(),
    }, headers=H)
    assert r.status_code == 201
    # Response should be in UTC
    assert r.json()["start_time"].endswith("+00:00")


def test_create_booking_room_not_found():
    s = _setup_org("bknf")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.post("/bookings", json={
        "room_id": 99999,
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H)
    assert r.status_code == 404
    assert r.json()["code"] == "ROOM_NOT_FOUND"


def test_create_booking_cross_org_room_404():
    s1 = _setup_org("bkco1")
    s2 = _setup_org("bkco2")
    room = _create_room(s1)
    H2 = {"Authorization": f"Bearer {s2['admin_token']}"}
    r = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H2)
    assert r.status_code == 404


def test_create_booking_requires_auth():
    s = _setup_org("bkauth")
    room = _create_room(s)
    r = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    })
    assert r.status_code == 401


def test_create_booking_invalid_window_past_start():
    s = _setup_org("bkpast")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    past_end = (datetime.now(timezone.utc) - timedelta(hours=0)).isoformat()
    r = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": past,
        "end_time": past_end,
    }, headers=H)
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"


# =============================================================================
# /bookings  (list)
# =============================================================================

def test_list_bookings_only_caller_own():
    s = _setup_org("listbk")
    room = _create_room(s)
    Ha = {"Authorization": f"Bearer {s['admin_token']}"}
    Hm = {"Authorization": f"Bearer {s['member_token']}"}
    # admin creates
    client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=Ha)
    # member creates
    client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(55),
        "end_time": _future(56),
    }, headers=Hm)
    r_admin = client.get("/bookings", headers=Ha).json()
    r_member = client.get("/bookings", headers=Hm).json()
    assert r_admin["total"] == 1
    assert r_member["total"] == 1


def test_list_bookings_response_shape():
    s = _setup_org("lbshape")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H)
    r = client.get("/bookings", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"items", "page", "limit", "total"}
    assert body["page"] == 1
    assert body["limit"] == 10
    assert body["total"] == 1


def test_list_bookings_default_page_and_limit():
    s = _setup_org("lbdef")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.get("/bookings", headers=H).json()
    assert r["page"] == 1
    assert r["limit"] == 10


def test_list_bookings_invalid_limit_422():
    s = _setup_org("lblim")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.get("/bookings?limit=200", headers=H)
    assert r.status_code == 422  # FastAPI validation: limit must be ≤ 100


def test_list_bookings_invalid_page_422():
    s = _setup_org("lbpg")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.get("/bookings?page=0", headers=H)
    assert r.status_code == 422


# =============================================================================
# /bookings/{id}
# =============================================================================

def test_get_booking_includes_refunds_field():
    s = _setup_org("gbrf")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H).json()
    r = client.get(f"/bookings/{b['id']}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "refunds" in body
    assert body["refunds"] == []


def test_get_booking_after_cancel_has_refund_entry():
    s = _setup_org("gbrf2")
    room = _create_room(s, rate=1000)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H).json()
    client.post(f"/bookings/{b['id']}/cancel", headers=H)
    r = client.get(f"/bookings/{b['id']}", headers=H).json()
    assert len(r["refunds"]) == 1
    entry = r["refunds"][0]
    assert entry["amount_cents"] == 2000
    assert entry["status"] == "processed"
    assert entry["processed_at"].endswith("+00:00")


def test_get_booking_not_found_404():
    s = _setup_org("gbnf")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.get("/bookings/99999", headers=H)
    assert r.status_code == 404
    assert r.json()["code"] == "BOOKING_NOT_FOUND"


def test_get_booking_cross_org_404():
    s1 = _setup_org("gbco1")
    s2 = _setup_org("gbco2")
    H1 = {"Authorization": f"Bearer {s1['admin_token']}"}
    room = _create_room(s1)
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H1).json()
    H2 = {"Authorization": f"Bearer {s2['admin_token']}"}
    r = client.get(f"/bookings/{b['id']}", headers=H2)
    assert r.status_code == 404


# =============================================================================
# /bookings/{id}/cancel
# =============================================================================

def test_cancel_response_shape():
    s = _setup_org("cshape")
    room = _create_room(s, rate=1000)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"id", "status", "refund_percent", "refund_amount_cents"}
    assert body["id"] == b["id"]
    assert body["status"] == "cancelled"
    assert body["refund_percent"] == 100
    assert body["refund_amount_cents"] == 2000


def test_cancel_24_to_48h_gives_50_percent():
    s = _setup_org("c50")
    room = _create_room(s, rate=1000)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    # 30h from now: notice = 30h - 0 = 30h, in [24h, 48h)
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(30),
        "end_time": _future(31),
    }, headers=H).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 50


def test_cancel_under_24h_gives_0_percent():
    s = _setup_org("c0")
    room = _create_room(s, rate=1000)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(10),
        "end_time": _future(11),
    }, headers=H).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 0
    assert r.json()["refund_amount_cents"] == 0


def test_cancel_over_48h_gives_100_percent():
    s = _setup_org("c100")
    room = _create_room(s, rate=1000)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(72),
        "end_time": _future(73),
    }, headers=H).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 100
    assert r.json()["refund_amount_cents"] == 1000


def test_cancel_refund_half_up_rounding():
    s = _setup_org("crnd")
    room = _create_room(s, rate=1001)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    # 50% of 1001 cents = 500.5 -> 501
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(30),
        "end_time": _future(31),
    }, headers=H).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert r.json()["refund_percent"] == 50
    assert r.json()["refund_amount_cents"] == 501


def test_double_cancel_409():
    s = _setup_org("dc")
    room = _create_room(s)
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H).json()
    client.post(f"/bookings/{b['id']}/cancel", headers=H)
    r = client.post(f"/bookings/{b['id']}/cancel", headers=H)
    assert r.status_code == 409
    assert r.json()["code"] == "ALREADY_CANCELLED"


def test_cancel_not_found_404():
    s = _setup_org("cnf")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.post("/bookings/99999/cancel", headers=H)
    assert r.status_code == 404
    assert r.json()["code"] == "BOOKING_NOT_FOUND"


def test_cancel_cross_org_404():
    s1 = _setup_org("cco1")
    s2 = _setup_org("cco2")
    H1 = {"Authorization": f"Bearer {s1['admin_token']}"}
    room = _create_room(s1)
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=H1).json()
    H2 = {"Authorization": f"Bearer {s2['admin_token']}"}
    r = client.post(f"/bookings/{b['id']}/cancel", headers=H2)
    assert r.status_code == 404


def test_cancel_another_members_booking_404():
    s = _setup_org("camb")
    Ha = {"Authorization": f"Bearer {s['admin_token']}"}
    Hm = {"Authorization": f"Bearer {s['member_token']}"}
    room = _create_room(s)
    # member creates a booking
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=Hm).json()
    # another member tries to cancel it (we use admin token but pretend a
    # different member; create one)
    client.post("/auth/register", json={
        "org_name": s["org"], "username": "member2", "password": "pw12345"
    })
    login = client.post("/auth/login", json={
        "org_name": s["org"], "username": "member2", "password": "pw12345"
    }).json()
    Hm2 = {"Authorization": f"Bearer {login['access_token']}"}
    r = client.post(f"/bookings/{b['id']}/cancel", headers=Hm2)
    assert r.status_code == 404


def test_admin_can_cancel_any_in_org():
    s = _setup_org("admincan")
    Ha = {"Authorization": f"Bearer {s['admin_token']}"}
    Hm = {"Authorization": f"Bearer {s['member_token']}"}
    room = _create_room(s)
    b = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": _future(50),
        "end_time": _future(52),
    }, headers=Hm).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=Ha)
    assert r.status_code == 200


# =============================================================================
# /admin/usage-report
# =============================================================================

def test_usage_report_response_shape():
    s = _setup_org("urshape")
    Ha = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.get("/admin/usage-report?from=2030-01-01&to=2030-01-31", headers=Ha)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"from", "to", "rooms"}
    assert body["from"] == "2030-01-01"
    assert body["to"] == "2030-01-31"
    assert isinstance(body["rooms"], list)


def test_usage_report_includes_zero_booking_rooms():
    s = _setup_org("urzero")
    Ha = {"Authorization": f"Bearer {s['admin_token']}"}
    room = _create_room(s)
    r = client.get("/admin/usage-report?from=2030-01-01&to=2030-01-31", headers=Ha).json()
    matching = [row for row in r["rooms"] if row["room_id"] == room["id"]]
    assert len(matching) == 1
    assert matching[0]["confirmed_bookings"] == 0
    assert matching[0]["revenue_cents"] == 0


def test_usage_report_counts_confirmed_excludes_cancelled():
    s = _setup_org("urcount")
    Ha = {"Authorization": f"Bearer {s['admin_token']}"}
    room = _create_room(s, rate=1000)
    # Booking in range
    b1 = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": "2030-06-15T10:00:00",
        "end_time": "2030-06-15T12:00:00",
    }, headers=Ha).json()
    b2 = client.post("/bookings", json={
        "room_id": room["id"],
        "start_time": "2030-06-15T14:00:00",
        "end_time": "2030-06-15T16:00:00",
    }, headers=Ha).json()
    # cancel b2
    client.post(f"/bookings/{b2['id']}/cancel", headers=Ha)
    r = client.get("/admin/usage-report?from=2030-06-01&to=2030-06-30", headers=Ha).json()
    matching = [row for row in r["rooms"] if row["room_id"] == room["id"]][0]
    assert matching["confirmed_bookings"] == 1
    assert matching["revenue_cents"] == 2000  # only b1 counted


def test_usage_report_requires_admin():
    s = _setup_org("uradm")
    Hm = {"Authorization": f"Bearer {s['member_token']}"}
    r = client.get("/admin/usage-report?from=2030-01-01&to=2030-01-31", headers=Hm)
    assert r.status_code == 403


def test_usage_report_invalid_date_400():
    s = _setup_org("urinv")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.get("/admin/usage-report?from=bogus&to=2030-01-31", headers=H)
    assert r.status_code == 400


def test_usage_report_only_caller_org():
    s1 = _setup_org("urco1")
    s2 = _setup_org("urco2")
    H1 = {"Authorization": f"Bearer {s1['admin_token']}"}
    H2 = {"Authorization": f"Bearer {s2['admin_token']}"}
    room1 = _create_room(s1)
    room2 = _create_room(s2)
    r1 = client.get("/admin/usage-report?from=2030-01-01&to=2030-01-31", headers=H1).json()
    r2 = client.get("/admin/usage-report?from=2030-01-01&to=2030-01-31", headers=H2).json()
    ids1 = {row["room_id"] for row in r1["rooms"]}
    ids2 = {row["room_id"] for row in r2["rooms"]}
    assert room1["id"] in ids1 and room1["id"] not in ids2
    assert room2["id"] in ids2 and room2["id"] not in ids1


# =============================================================================
# /admin/export
# =============================================================================

def test_export_csv_header_exact():
    s = _setup_org("exh")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    r = client.get("/admin/export", headers=H)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    first_line = r.text.splitlines()[0]
    expected = "id,reference_code,room_id,user_id,start_time,end_time,status,price_cents"
    assert first_line == expected, f"got: {first_line!r}"


def test_export_default_scoped_to_caller():
    s = _setup_org("exdef")
    Ha = {"Authorization": f"Bearer {s['admin_token']}"}
    Hm = {"Authorization": f"Bearer {s['member_token']}"}
    room = _create_room(s)
    # admin and member each book
    client.post("/bookings", json={
        "room_id": room["id"], "start_time": _future(50), "end_time": _future(51)
    }, headers=Ha)
    client.post("/bookings", json={
        "room_id": room["id"], "start_time": _future(60), "end_time": _future(61)
    }, headers=Hm)
    # admin's default export: only admin's bookings
    r = client.get("/admin/export", headers=Ha)
    reader = csv.reader(io.StringIO(r.text))
    rows = list(reader)
    assert len(rows) == 2  # header + 1 booking
    assert len(rows) - 1 == 1


def test_export_include_all_returns_all_org_bookings():
    s = _setup_org("exall")
    Ha = {"Authorization": f"Bearer {s['admin_token']}"}
    Hm = {"Authorization": f"Bearer {s['member_token']}"}
    room = _create_room(s)
    client.post("/bookings", json={
        "room_id": room["id"], "start_time": _future(50), "end_time": _future(51)
    }, headers=Ha)
    client.post("/bookings", json={
        "room_id": room["id"], "start_time": _future(60), "end_time": _future(61)
    }, headers=Hm)
    r = client.get("/admin/export?include_all=true", headers=Ha)
    rows = list(csv.reader(io.StringIO(r.text)))
    assert len(rows) - 1 == 2  # both bookings


def test_export_room_filter():
    s = _setup_org("exrf")
    Ha = {"Authorization": f"Bearer {s['admin_token']}"}
    r1 = _create_room(s, name="R1")
    r2 = _create_room(s, name="R2")
    client.post("/bookings", json={
        "room_id": r1["id"], "start_time": _future(50), "end_time": _future(51)
    }, headers=Ha)
    client.post("/bookings", json={
        "room_id": r2["id"], "start_time": _future(60), "end_time": _future(61)
    }, headers=Ha)
    r = client.get(f"/admin/export?room_id={r1['id']}&include_all=true", headers=Ha)
    rows = list(csv.reader(io.StringIO(r.text)))
    assert len(rows) - 1 == 1
    assert rows[1][2] == str(r1["id"])  # room_id column


def test_export_cross_org_room_404():
    s1 = _setup_org("exco1")
    s2 = _setup_org("exco2")
    H1 = {"Authorization": f"Bearer {s1['admin_token']}"}
    H2 = {"Authorization": f"Bearer {s2['admin_token']}"}
    room1 = _create_room(s1)
    # s2 admin tries to export s1's room
    r = client.get(f"/admin/export?room_id={room1['id']}&include_all=true", headers=H2)
    assert r.status_code == 404
    assert r.json()["code"] == "ROOM_NOT_FOUND"


def test_export_requires_admin():
    s = _setup_org("exmem")
    Hm = {"Authorization": f"Bearer {s['member_token']}"}
    r = client.get("/admin/export", headers=Hm)
    assert r.status_code == 403


def test_export_requires_auth():
    r = client.get("/admin/export")
    assert r.status_code == 401


# =============================================================================
# Cross-cutting: response field shapes
# =============================================================================

def test_error_response_shape():
    """Every error must be {"detail": str, "code": str}."""
    s = _setup_org("errshape")
    H = {"Authorization": f"Bearer {s['admin_token']}"}
    # 401
    r = client.get("/rooms")
    assert "detail" in r.json() and "code" in r.json()
    # 404
    r = client.post("/bookings", json={
        "room_id": 99999, "start_time": _future(50), "end_time": _future(52)
    }, headers=H)
    assert "detail" in r.json() and "code" in r.json()
    # 400
    r = client.post("/bookings", json={
        "room_id": _create_room(s)["id"],
        "start_time": _future(60),
        "end_time": _future(58),
    }, headers=H)
    assert "detail" in r.json() and "code" in r.json()


def test_bearer_token_format():
    """Access tokens should start with 'eyJ' (JWT base64)."""
    s = _setup_org("tokshape")
    r = client.post("/auth/login", json={
        "org_name": s["org"], "username": "admin", "password": "pw12345"
    }).json()
    assert r["access_token"].count(".") == 2
    assert r["refresh_token"].count(".") == 2
    assert r["token_type"] == "bearer"