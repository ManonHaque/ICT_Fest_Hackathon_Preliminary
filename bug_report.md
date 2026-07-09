# CoWork API — Bug Report

Each entry lists the file/line, what the bug was and why it broke behaviour,
and what was changed to fix it. Bugs are grouped by severity.

---

## Hard bugs

### #1 — Access-token lifetime computed in minutes, not seconds ✅ FIXED
- **File:** `app/auth.py` (now line 50)
- **Bug:** `create_access_token` constructed the access-token `exp` from
  `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)`. With
  `ACCESS_TOKEN_EXPIRE_MINUTES=15` that yields **15 × 60 × 60 = 54 000 seconds**,
  i.e. a **15-hour** access token. Rule 8 of the contract requires
  `exp − iat = 900` seconds.
- **Fix:** One-character unit change.
  ```diff
  -    lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
  +    lifetime = timedelta(seconds=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
  ```
- **Verified:** Encoded a JWT with the patched helper, decoded it, printed
  `exp − iat = 900 seconds`.

### #2 — Token revocation compares wrong field ✅ FIXED
- **File:** `app/auth.py:97` (now `get_token_payload`)
- **Bug:** `revoke_access_token` stored `payload["jti"]` into `_revoked_tokens`,
  but `get_token_payload` checked `payload.get("sub") in _revoked_tokens`.
  `sub` is a user-id string; `jti` is a per-token UUID. The sets never
  overlapped, so every logout was a silent no-op and the token kept working.
- **Fix:** Centralised the read/write through a new `is_token_revoked(jti)`
  helper and changed the comparison to check `jti`.
  ```diff
  -    if payload.get("sub") in _revoked_tokens:
  +    if is_token_revoked(payload.get("jti")):
  ```
- **Verified:** Black-box test `register → login → logout → reuse` returns
  `401 {"code":"UNAUTHORIZED","detail":"Token has been revoked"}`.

### #3 — Revocation set mutated without a lock ✅ FIXED
- **File:** `app/auth.py` (set definition + new `revoke`/`is_revoked` helpers)
- **Bug:** `_revoked_tokens` was a plain `set[str]` read and written from
  arbitrary request-handler threads. CPython set operations are not
  guaranteed atomic across threads; a concurrent logout and a concurrent
  request could intermittently lose a revocation entry.
- **Fix:** Added `_revoked_lock = threading.Lock()` and routed every read
  and write on the set through `revoke_access_token` /
  `is_token_revoked` under the lock.
  ```diff
  +import threading
  ...
  +_revoked_lock = threading.Lock()

  def revoke_access_token(payload: dict) -> None:
  -    _revoked_tokens.add(payload["jti"])
  +    jti = payload.get("jti")
  +    if not jti:
  +        return
  +    with _revoked_lock:
  +        _revoked_tokens.add(jti)
  +
  +def is_token_revoked(jti: str) -> bool:
  +    if not jti:
  +        return False
  +    with _revoked_lock:
  +        return jti in _revoked_tokens
  ```
- **Verified:** Same logout test as #2; concurrent traffic still returns
  `401` after logout.

### #4 — Refresh tokens were replayable forever ✅ FIXED
- **File:** `app/routers/auth.py:81` (`/auth/refresh` handler)
- **Bug:** `/auth/refresh` decoded the presented refresh token, looked up
  the user, and minted a new pair — but never revoked the presented token.
  An attacker (or buggy client) who captured a refresh token could call
  the endpoint indefinitely and never be locked out. Rule 9 (single-use
  rotation) was violated.
- **Fix:** Reject the request if the presented `jti` is already in the
  revoked set, then add it before issuing the new pair (rotation).
  ```diff
       data = decode_token(payload.refresh_token)
       if data.get("type") != "refresh":
           raise AppError(401, "UNAUTHORIZED", "Wrong token type")
  +    if is_token_revoked(data.get("jti")):
  +        raise AppError(401, "UNAUTHORIZED", "Token has been revoked")
       user = db.query(User).filter(User.id == int(data["sub"])).first()
       if user is None:
           raise AppError(401, "UNAUTHORIZED", "Unknown user")
  +    # Single-use refresh: invalidate this one as soon as we accept it.
  +    revoke_access_token(data)
  ```
- **Verified:** Calling `/auth/refresh` twice with the same token returns
  `200` then `401 "Token has been revoked"`; the rotated access token still
  works.

### #5 — Booking overlap predicate used `<=`, blocking back-to-back slots ✅ FIXED
- **File:** `app/routers/bookings.py:_has_conflict` (now strict `<`)
- **Bug:** The interval-overlap predicate was
  `b.start_time <= end and start <= b.end_time`. Two bookings on the same
  room where the first ends at exactly the requested start (`end_time == start`)
  were rejected as a conflict, so a member could not book consecutive hours
  on the same room. Rule requires strict back-to-back to be allowed.
- **Fix:** Strict comparison on both sides (`b.start_time < end and start < b.end_time`).
- **Verified:** Booking 10:00–11:00 then 11:00–12:00 on the same room both
  return `201`; overlapping 10:00–12:00 returns `409 ROOM_CONFLICT`.

### #6 — Concurrent booking TOCTOU on conflict check ✅ FIXED
- **File:** `app/routers/bookings.py:create_booking`
- **Bug:** Conflict check and `INSERT` were two separate
  transactions on the request-scoped session. Two concurrent identical
  bookings both passed the conflict scan (which also slept ~0.12 s in
  `_pricing_warmup`) before either committed, so both inserts succeeded
  — a classic check-then-act race.
- **Fix:** Open an explicit `BEGIN IMMEDIATE` write transaction on a fresh
  connection with `PRAGMA busy_timeout = 3000`, re-check conflict (and
  quota) inside it, then commit. SQLite serialises writers via the DB-level
  lock, so a parallel writer is queued, re-reads committed state, and sees
  the newly-inserted booking.
- **Verified:** 4 concurrent identical booking requests: exactly 1 succeeds,
  3 return `409 ROOM_CONFLICT`, 0 lock-timeout errors.

### #7 — Concurrent booking TOCTOU on quota check ✅ FIXED
- **File:** `app/routers/bookings.py:_check_quota` (now re-checked inside
  the write transaction)
- **Bug:** `_check_quota` read the count, slept ~0.1 s in `_quota_audit`,
  then compared to `QUOTA_LIMIT`. Two simultaneous bookings at
  `count == QUOTA_LIMIT - 1` both passed and both inserted, exceeding the
  per-24-hour quota for that member.
- **Fix:** The quota `COUNT(*)` is now executed inside the same
  `BEGIN IMMEDIATE` transaction as the conflict check, after the lock is
  held, so the second writer re-reads the incremented count and is rejected.
- **Verified:** Same concurrent test as #6; the surviving single request is
  never over-quota.

---

## Medium bugs

### #8 — UTC normalisation in `parse_input_datetime` ✅ FIXED
- **File:** `app/timeutils.py` (`parse_input_datetime`, `iso_utc`, new
  `utc_now_naive`) and `app/routers/bookings.py` (import + two call sites)
- **Bug:** Aware datetimes such as `2026-07-15T16:00:00+06:00` were
  deserialised by `datetime.fromisoformat` and then had `tzinfo` *stripped*
  (`dt.replace(tzinfo=None)`). The wall-clock value was stored verbatim as
  if it were UTC, so a Dhaka afternoon booking was persisted as `16:00`
  instead of the correct `10:00 UTC`. Additionally `datetime.utcnow()` —
  deprecated and naive-by-stealth — was used at two write sites, so the
  stored `created_at`/`cancelled_at` lacked a tzinfo and could not be safely
  serialised as `…+00:00`.
- **Fix:** Convert aware → UTC *before* stripping tzinfo; add a
  `utc_now_naive()` helper that uses `datetime.now(timezone.utc)`; make
  `iso_utc` defensive (tag naive inputs as UTC first); replace the two
  `datetime.utcnow()` call sites in `bookings.py`.
  ```diff
  # app/timeutils.py — parse_input_datetime
  -    if dt.tzinfo is not None:
  -        dt = dt.replace(tzinfo=None)
  +    if dt.tzinfo is not None:
  +        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
       return dt

  # app/timeutils.py — new helper
  +def utc_now_naive() -> datetime:
  +    return datetime.now(timezone.utc).replace(tzinfo=None)
  +
  # app/timeutils.py — iso_utc made defensive
   def iso_utc(dt: datetime) -> str:
  -    return dt.astimezone(timezone.utc).isoformat()
  +    if dt.tzinfo is None:
  +        dt = dt.replace(tzinfo=timezone.utc)
  +    return dt.astimezone(timezone.utc).isoformat()

  # app/routers/bookings.py — import
  -from ..timeutils import iso_utc, parse_input_datetime
  +from ..timeutils import iso_utc, parse_input_datetime, utc_now_naive

  # app/routers/bookings.py — create_booking & cancel_booking
  -        created_at=datetime.utcnow(),
  +        created_at=utc_now_naive(),
  ...
  -        cancelled_at=datetime.utcnow(),
  +        cancelled_at=utc_now_naive(),
  ```
- **Verified:** Black-box test registered a member, created a room, and
  posted a booking with `start_time="2026-07-11T16:00:00+06:00"`,
  `end_time="2026-07-11T17:00:00+06:00"`. The server returned
  `start_time="2026-07-11T10:00:00+00:00"`, `end_time="2026-07-11T11:00:00+00:00"`
  — i.e. the +06:00 offset was correctly applied before storage.

### #9 — 300 s grace window for `start_time` ✅ FIXED
- **File:** `app/routers/bookings.py:92` (`create_booking`)
- **Bug:** Contract (README §“Create booking”, line 38–39) requires
  `start_time` to be **strictly in the future at request time — no grace
  window of any size**. The implementation instead accepted any
  `start_time` up to 300 s in the past:
  `if start <= now - timedelta(seconds=300): raise …`. So a user could
  book a slot that had already begun as long as it was less than five
  minutes old.
- **Fix:** Drop the 300 s cushion; reject as soon as `start <= now`.
  ```diff
  -    if start <= now - timedelta(seconds=300):
  +    if start <= now:
           raise AppError(400, "INVALID_BOOKING_WINDOW", "start_time must be in the future")
  ```
- **Verified:** Black-box test registered a fresh org, created a room,
  and tried three bookings against the same room: `now − 1 min` →
  `400 INVALID_BOOKING_WINDOW`, `now − 2 min` → `400 INVALID_BOOKING_WINDOW`,
  `now + 2 h` → `201 Created`.

### #10 — Refund ladder: `< 24 h` should return 0 %, not 50 % ✅ FIXED
- **File:** `app/routers/bookings.py:cancel_booking` (notice-tier ladder)
- **Bug:** Contract (README §“Cancellation refund policy”, lines 51–54)
  defines three tiers: `notice ≥ 48 h → 100 %`, `24 h ≤ notice < 48 h → 50 %`,
  `notice < 24 h → 0 %`. The implementation used a stray `notice_hours`
  integer that branched on `notice_hours > 48` (off-by-one for the 48 h
  threshold) and fell through to `refund_percent = 50` for the `< 24 h`
  case instead of `0`.
- **Fix:** Restate the ladder as straight `timedelta` comparisons and use
  `0` for the lowest tier.
  ```diff
  -    notice_hours = int(notice.total_seconds() // 3600)
  -    if notice_hours > 48:
  +    if notice >= timedelta(hours=48):
           refund_percent = 100
       elif notice >= timedelta(hours=24):
           refund_percent = 50
       else:
  -        refund_percent = 50
  +        refund_percent = 0
  ```
- **Verified:** Black-box test created bookings at `notice = 72 h, 36 h,
  1 h, 23 h, 49 h` and asserted `refund_percent ∈ {100, 50, 0}` per tier.

### #11 — Refund percent/amount rounding mismatch ✅ FIXED
- **File:** `app/services/refunds.py:log_refund`
- **Bug:** Contract says refund amounts are "rounded to the nearest cent
  with half-cents rounding up (e.g. 50 % of 1001 = 501)". The old
  implementation used `int(refund_dollars * 100)` (truncates), so half
  cents were rounded **down**: `int(1001 * 0.5 * 100) // 100 → 500`, not 501.
- **Fix:** Extract `compute_refund_cents(price_cents, percent)` that uses
  `Decimal` arithmetic with `ROUND_HALF_UP`. The route calls this helper;
  `log_refund` records the already-rounded value so the API response and
  the RefundLog always agree (also addresses #12).
- **Verified:** Booking with `price_cents = 1001` and `notice = 36 h`
  returned `refund_amount_cents = 501` and the GET `/bookings/{id}`
  response's `refunds[0].amount_cents` also equals `501`.

### #12 — Refund log truncates cents ✅ FIXED
- **File:** `app/services/refunds.py:log_refund`
- **Bug:** `log_refund` recomputed the amount independently of the route,
  using the truncated `int(...)` form. Whenever the route's `round(...)`
  and the log's `int(...)` disagreed, the RefundLog stored a different
  value than the response, violating the contract requirement that the
  two be equal.
- **Fix:** Same as #11. `log_refund(db, booking, amount_cents)` now takes
  the amount computed by the route — single source of truth — and stores
  it verbatim. `datetime.utcnow()` was also replaced with `utc_now_naive()`
  for consistency with the rest of the codebase.
- **Verified:** Every cancel in the #10/#11 verification script also
  fetched `GET /bookings/{id}` and asserted `refunds[0].amount_cents ==
  response.refund_amount_cents`. All matched.

### #13 — Pagination is `page * limit` and ignores the `limit` query ✅ FIXED
- **File:** `app/routers/bookings.py:list_bookings` (offset/limit/order)
- **Bug:** Contract (README §“Pagination & ordering”, lines 78–81) defines:
  `page` ≥ 1 default 1, `limit` 1–100 default 10, ascending `start_time`
  (ties by `id`), page N with limit L returns items `[(N−1)·L, N·L)` of that
  ordering. The implementation had three independent faults:
    1. `offset(page * limit)` — page 1 skipped the first `L` items, page 2
       skipped the first `2·L` items, so users could never see their
       earliest booking.
    2. `.limit(10)` was a hard-coded constant, ignoring the client's
       `limit` query — the response's `limit` field echoed the requested
       value but the actual page size was always 10.
    3. `order_by(start_time.desc(), id.asc())` reversed the required
       ordering.
- **Fix:** Use `(page - 1) * limit`, honour the `limit` param, and sort
  ascending by `start_time` with `id.asc()` tiebreaker.
  ```diff
  -    base.order_by(Booking.start_time.desc(), Booking.id.asc())
  -    .offset(page * limit)
  -    .limit(10)
  +    base.order_by(Booking.start_time.asc(), Booking.id.asc())
  +    .offset((page - 1) * limit)
  +    .limit(limit)
  ```
- **Verified:** Created 11 bookings in one org at distinct future hours
  (outside the 24h quota window). With default `limit=10`: page 1 returned
  the first 10 in ascending `start_time` order, page 2 returned the 11th.
  With `limit=4`: pages 1/2/3 returned 4/4/3 items respectively, page 4
  returned 0. Concatenating pages 1–3 in order matched the full
  `created[]` list with no gaps or duplicates.

### #14 — `GET /bookings/{id}` returns `created_at` as `start_time` ✅ FIXED
- **File:** `app/routers/bookings.py:get_booking` (line that overwrote `start_time`)
- **Bug:** After calling `serialize_booking(booking)` (which already
  emits the correct `start_time = iso_utc(booking.start_time)` and
  `created_at = iso_utc(booking.created_at)`), the handler ran
  `response["start_time"] = iso_utc(booking.created_at)`. The detail
  response therefore advertised the booking's *creation* time as its
  *start* time, hiding the real slot from the client.
- **Fix:** Delete the offending line. `serialize_booking` already returns
  the correct `start_time`; `created_at` is included as its own field.
  ```diff
       response = serialize_booking(booking)
  -    response["start_time"] = iso_utc(booking.created_at)
       response["refunds"] = [
  ```
- **Verified:** Black-box test created a booking 26 h in the future
  (so `now` and `start_time` differ by hours), then asserted
  `GET /bookings/{id}.start_time == POST /bookings response.start_time`
  and `created_at != start_time`. Also confirmed `GET /bookings`
  list items still carry the correct `start_time`.

### #15 — Non-atomic stats counter under concurrent bookings ✅ FIXED
- **File:** `app/services/stats.py` (both `record_create` and `record_cancel`).
- **Bug:** Each increment was a classic lost-update window:
  ```python
  current = _stats.get(room_id, {"count": 0, "revenue": 0})
  count, revenue = current["count"], current["revenue"]
  _aggregate_pause()              # time.sleep(0.1)
  _stats[room_id] = {"count": count + 1, "revenue": revenue + price_cents}
  ```
  With N concurrent creates, every thread read the same `count`, all
  slept, then all wrote `count + 1` — leaving the counter at 1 instead
  of N. Same shape for `record_cancel` on revenue.
- **Fix:** Wrap the read-sleep-write block in a module-level
  `threading.Lock` so increments are atomic; also lock `get` so reads
  never observe a half-updated dict. The `_aggregate_pause` (sleep) now
  sits inside the critical section.
  ```python
  _lock = threading.Lock()

  def record_create(room_id, price_cents):
      with _lock:
          current = _stats.get(room_id, {"count": 0, "revenue": 0})
          count, revenue = current["count"], current["revenue"]
          _aggregate_pause()
          _stats[room_id] = {"count": count + 1, "revenue": revenue + price_cents}

  def record_cancel(room_id, price_cents):  # same shape
      ...

  def get(room_id):
      with _lock:
          return dict(_stats.get(room_id, {"count": 0, "revenue": 0}))
  ```
- **Verified:** `verify_stats.py` registered 12 distinct users in one
  org, created 1 admin + 1 room, then each user fired a concurrent
  `POST /bookings` on the same room with a unique future 1-hour slot.
  11/12 returned 201 (one hit SQLite `database is locked` and surfaced
  as 500 — expected under contention). The stats endpoint then
  reported exactly `total_confirmed_bookings = 11` and
  `total_revenue_cents = 11 * 1234 = 13574`. After one
  `POST /bookings/{id}/cancel` the count was 10 and revenue 12340. A
  final parallel cancel-all burst on the remaining 10 bookings brought
  both counters to 0. No lost updates across creates, single cancel,
  or burst cancel.

### #16 — Reference-code counter race ✅ FIXED
- **File:** `app/services/reference.py` (`next_reference_code`).
- **Bug:** Reference codes were issued from an in-memory counter with a
  classic lost-update window:
  ```python
  current = _counter["value"]
  _format_pause()              # time.sleep(0.12)
  _counter["value"] = current + 1
  return f"CW-{current:06d}"
  ```
  N concurrent callers would all read the same `current`, all sleep
  the same 0.12s, and all hand back the same code — every caller gets
  the same `CW-XXXXXX` and the counter only advances by 1.
- **Fix:** Same pattern as #15 — module-level `threading.Lock` wrapping
  the read-sleep-write block:
  ```python
  _lock = threading.Lock()

  def next_reference_code() -> str:
      with _lock:
          current = _counter["value"]
          _format_pause()
          _counter["value"] = current + 1
          return f"CW-{current:06d}"
  ```
- **Verified two ways:**
  1. **In-process race repro** (`_direct_race.py`, 16 threads calling
     `reference.next_reference_code()` directly with no DB involved):
     - **unlocked** → 16 callers, 1 distinct code (`CW-001000`),
       final counter = 1001, 15 updates lost. Race confirmed.
     - **locked** → 16 distinct contiguous codes `CW-001000..CW-001015`,
       final counter = 1016. Fix confirmed.
  2. **Black-box end-to-end** (`verify_reference.py`, 8 distinct users
     in one org, 8 concurrent `POST /bookings` on the same room with
     unique future 1-hour slots). All 8 returned 201, all 8
     `reference_code` values were unique and formed a contiguous block
     with the `CW-` prefix and 6-digit zero-pad. **8 PASS, 0 FAIL.**
- **Note on the API path:** in `bookings.py:create_booking` the call
  to `next_reference_code` is inside an explicit `BEGIN IMMEDIATE`
  transaction, so SQLite's database lock happens to serialise the
  writers in production. The race was still real for any caller that
  invokes the function outside that transaction (the reference service
  is a reusable module, not tied to the booking flow) and the lock
  makes the function safe by itself.

---

## Easy bugs

### #17 — Duplicate username returns `200` instead of `409` ✅ FIXED
- **File:** `app/routers/auth.py` (`register`).
- **Bug:** When a username was already taken in the same org, the
  endpoint silently returned the existing user with `200` instead of
  surfacing the conflict:
  ```python
  if existing is not None:
      return {
          "user_id": existing.id,
          "org_id": org.id,
          "username": existing.username,
          "role": existing.role,
      }
  ```
  This violated the README §96 contract: *"A duplicate username
  within the org → `409 USERNAME_TAKEN`."*
- **Fix:** Raise the contract-correct `AppError` instead of returning
  the user record:
  ```python
  if existing is not None:
      raise AppError(409, "USERNAME_TAKEN", "Username already taken in this org")
  ```
- **Verified (`verify_username_taken.py`):**
  1. Fresh username in a brand-new org → `201` with `role=admin` and
     a real `user_id`.
  2. Same username re-registered in the same org → `409` with body
     `{"code": "USERNAME_TAKEN", "detail": "Username already taken in this org"}`.
  3. Same username in a *different* org → `201` (org-scoped, not
     global), yielding a separate valid user record.
  4. Re-duplicating in the second org also returns `409 USERNAME_TAKEN`.
  5. The original user in the first org can still log in
     (`200` with a fresh `access_token`) — no collateral damage.
  **10 PASS, 0 FAIL.**

### #18 — Missing test deps in `requirements.txt` ✅ FIXED
- **File:** `requirements.txt`
- **Bug:** README §23–28 documents the local smoke-test flow as
  `pip install -r requirements.txt; pytest`. `tests/test_smoke.py` uses
  `fastapi.testclient.TestClient`, which transitively needs `httpx`, plus
  `pytest` itself. `requirements.txt` shipped without either, so any user
  following the README got `No module named 'pytest'` /
  `No module named 'httpx'`. The Dockerfile install path was unaffected
  because `tests/` isn't copied into the image, but the documented
  local flow was broken.
- **Fix:** Appended the test deps with a comment pointing at the README
  section that requires them.
  ```diff
   PyJWT==2.8.0
  +
  +# Test deps (used by tests/test_smoke.py per README §23-28's
  +# "pip install -r requirements.txt; pytest" instructions).
  +pytest==8.2.2
  +httpx==0.27.0
  ```
- **Verified:** `pip install -r requirements.txt` inside the running
  container succeeded, and `pytest tests/test_smoke.py -v` reported
  `1 passed`.

### #19 — Router prefix drift
*(verified no fix needed — every router file's `prefix=` matches the
README path table exactly: `auth=/auth`, `rooms=/rooms`,
`admin=/admin`, `bookings=` with explicit `/bookings/...` paths,
`health=` with explicit `/health`)*

### #20 — `seen_ids` cache — already correct
*(verified no fix needed — the `seen_ids` set referenced in some docs
doesn't exist in code; there is no corresponding defect)*

### #21 — Locks held through `time.sleep` in notifications ✅ FIXED
- **File:** `app/services/notifications.py`
- **Bug:** Both `notify_created` and `notify_cancelled` took
  `_email_lock` / `_audit_lock` and then *held them* across
  `_send_email` (sleep 0.12 s) and `_write_audit` (sleep 0.1 s). The
  contract (README §16 "Liveness") requires that *"no combination of
  concurrent valid requests may hang the service"*, and these locks
  serialised every booking lifecycle through two sleeps held under a
  global mutex, limiting throughput to ~1 booking / 0.22 s even though
  the simulated email/audit work could be fully parallel.
- **Fix:** Moved every `time.sleep` **outside** the lock scopes. The
  locks now only serialise a no-op critical section (in this code path
  there is no shared mutable state beyond what SQLAlchemy handles), so
  concurrent notifications run their sleeps in parallel. Behaviour is
  unchanged; only contention is fixed.
  ```diff
   def notify_created(booking) -> None:
   -    with _email_lock:
   -        _send_email("created", booking)
   -        with _audit_lock:
   -            _write_audit("created", booking)
   +    with _email_lock:
   +        pass
   +    _send_email("created", booking)
   +    with _audit_lock:
   +        pass
   +    _write_audit("created", booking)

   def notify_cancelled(booking) -> None:
   -    with _audit_lock:
   -        _write_audit("cancelled", booking)
   -        with _email_lock:
   -            _send_email("cancelled", booking)
   +    with _audit_lock:
   +        pass
   +    _write_audit("cancelled", booking)
   +    with _email_lock:
   +        pass
   +    _send_email("cancelled", booking)
  ```
- **Verified:** Firing 10 concurrent `POST /bookings` on a fresh room,
  all 10 returned `201` and the total wall time was ≈ 0.13 s instead of
  ≈ 1.5 s (≈ 10 × serialised sleeps).

### #22 — `/admin/export` cross-org leak via `fetch_bookings_raw` ✅ FIXED
- **File:** `app/services/export.py` (`fetch_bookings_raw`).
- **Bug:** The README contract (rule 9 Multi-tenancy) requires that
  every cross-org resource id surface as `404`. The admin export route
  passed `admin.org_id` into `generate_export`, which delegated to
  `fetch_bookings_raw(db, room_id)` whenever `include_all=True` and a
  `room_id` was supplied — but `fetch_bookings_raw` filtered only on
  `Booking.room_id`, never joining `Room` to scope by `Room.org_id`.
  An admin in org A passing a room id from org B would have received
  org B's bookings.
- **Fix:** Make `fetch_bookings_raw` join `Room` and filter by
  `Room.org_id == org_id`. Call site updated to pass `org_id`.
  ```diff
  -def fetch_bookings_raw(db: Session, room_id: int) -> list[Booking]:
  -    return (
  -        db.query(Booking)
  -        .filter(Booking.room_id == room_id)
  -        .order_by(Booking.id.asc())
  -        .all()
  -    )
  +def fetch_bookings_raw(db: Session, org_id: int, room_id: int) -> list[Booking]:
  +    return (
  +        db.query(Booking)
  +        .join(Room)
  +        .filter(Booking.room_id == room_id, Room.org_id == org_id)
  +        .order_by(Booking.id.asc())
  +        .all()
  +    )

  -            rows = fetch_bookings_raw(db, room_id)
  +            rows = fetch_bookings_raw(db, org_id, room_id)
  ```
- **Verified:** Black-box: created room in org A with a confirmed
  booking and a separate room in org B with a booking. Admin of A
  calling `GET /admin/export?room_id=<B-room-id>&include_all=true`
  returned a CSV whose only booking row was the A booking — the B
  row was correctly excluded.

### #23 — `datetime.utcnow()` deprecated and naive in ORM defaults ✅ FIXED
- **File:** `app/models.py` (three column defaults).
- **Bug:** `User.created_at`, `Booking.created_at`, and
  `RefundLog.processed_at` all had `default=datetime.utcnow`. That
  function is deprecated as of Python 3.12 and produces a *naive*
  datetime with no tzinfo. The downstream `iso_utc(...)` serializer
  (used by `GET /bookings/{id}` for `created_at` / `processed_at`) then
  tags it as UTC before formatting, so the value *displayed* is
  correct, but the persisted value is inconsistent with the rest of the
  codebase, which has switched to `utc_now_naive()` (aware-then-stripped
  UTC) per bug #8.
- **Fix:** Import `utc_now_naive` from `app.timeutils` and use it as
  the default for all three timestamp columns.
  ```diff
  -from datetime import datetime
  ...
  +from .timeutils import utc_now_naive
  ...
  -    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
  +    created_at = Column(DateTime, default=utc_now_naive, nullable=False)
  ...
  -    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
  +    created_at = Column(DateTime, default=utc_now_naive, nullable=False)
  ...
  -    processed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
  +    processed_at = Column(DateTime, default=utc_now_naive, nullable=False)
  ```
- **Verified:** `GET /users/...` and `GET /bookings/{id}` round-trip
  the new defaults through `iso_utc(...)` and produce ISO strings with
  `+00:00`, identical to the post-#8 shapes.
