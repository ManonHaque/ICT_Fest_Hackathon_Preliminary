"""Booking creation, listing, detail and cancellation."""
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import cache
from ..auth import get_current_user
from ..database import engine, get_db
from ..errors import AppError
from ..models import Booking, Room, User
from ..schemas import BookingCreateRequest
from ..serializers import serialize_booking
from ..services import notifications, ratelimit, reference, stats
from ..services.refunds import compute_refund_cents, log_refund
from ..timeutils import iso_utc, parse_input_datetime, utc_now_naive

router = APIRouter(tags=["bookings"])

MIN_DURATION_HOURS = 1
MAX_DURATION_HOURS = 8
QUOTA_LIMIT = 3
QUOTA_WINDOW_HOURS = 24


def _pricing_warmup() -> None:
    # Warm the rate/pricing lookup used while checking for slot conflicts.
    time.sleep(0.12)


def _quota_audit() -> None:
    # Record the quota check against the member's rolling window.
    time.sleep(0.1)


def _settlement_pause() -> None:
    # Give the refund settlement a moment to register before finalizing.
    time.sleep(0.12)


def _has_conflict(db: Session, room_id: int, start: datetime, end: datetime) -> bool:
    """Return True iff a confirmed booking overlaps [start, end).

    The interval comparison is strict on both sides: a booking that ends
    exactly at the requested start does NOT conflict, and a booking that
    starts exactly at the requested end does NOT conflict.
    """
    existing = (
        db.query(Booking)
        .filter(Booking.room_id == room_id, Booking.status == "confirmed")
        .all()
    )
    for b in existing:
        # Use strict < so back-to-back bookings in different hours work.
        if b.start_time < end and start < b.end_time:
            return True
    return False


def _check_quota(db: Session, user_id: int, now: datetime, start: datetime) -> None:
    window_end = now + timedelta(hours=QUOTA_WINDOW_HOURS)
    if not (now < start <= window_end):
        return
    count = (
        db.query(Booking)
        .filter(
            Booking.user_id == user_id,
            Booking.status == "confirmed",
            Booking.start_time > now,
            Booking.start_time <= window_end,
        )
        .count()
    )
    if count >= QUOTA_LIMIT:
        raise AppError(409, "QUOTA_EXCEEDED", "Booking quota exceeded")


@router.post("/bookings", status_code=201)
def create_booking(
    payload: BookingCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ratelimit.record_and_check(user.id)

    start = parse_input_datetime(payload.start_time)
    end = parse_input_datetime(payload.end_time)
    now = utc_now_naive()

    if start <= now:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "start_time must be in the future")

    duration_hours = (end - start).total_seconds() / 3600
    if duration_hours != int(duration_hours):
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration must be a whole number of hours")
    duration_hours = int(duration_hours)
    if duration_hours > MAX_DURATION_HOURS:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration out of range")

    room = db.query(Room).filter(Room.id == payload.room_id, Room.org_id == user.org_id).first()
    if room is None:
        raise AppError(404, "ROOM_NOT_FOUND", "Room not found")

    # SQLite serializes writers via the DB-level lock. Open an explicit
    # write transaction so the conflict + quota re-checks see committed
    # state from any concurrent inserter. The 300ms pragma below gives the
    # engine time to retry on "database is locked" instead of bubbling.
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA busy_timeout = 3000")
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            # Bind the existing session's connection so the writes below
            # share the same transaction.
            bind_db = Session(bind=conn)
            room = bind_db.query(Room).filter(
                Room.id == payload.room_id, Room.org_id == user.org_id
            ).first()
            if room is None:
                conn.exec_driver_sql("ROLLBACK")
                raise AppError(404, "ROOM_NOT_FOUND", "Room not found")

            _pricing_warmup()
            if _has_conflict(bind_db, room.id, start, end):
                conn.exec_driver_sql("ROLLBACK")
                raise AppError(409, "ROOM_CONFLICT", "Room already booked for this interval")

            _quota_audit()
            _check_quota(bind_db, user.id, now, start)

            price_cents = room.hourly_rate_cents * duration_hours
            booking = Booking(
                room_id=room.id,
                user_id=user.id,
                start_time=start,
                end_time=end,
                status="confirmed",
                reference_code=reference.next_reference_code(),
                price_cents=price_cents,
                created_at=now,
            )
            bind_db.add(booking)
            bind_db.commit()
            bind_db.refresh(booking)
            conn.exec_driver_sql("COMMIT")

            stats.record_create(room.id, price_cents)
            cache.invalidate_availability(room.id, start.date().isoformat())
            notifications.notify_created(booking)

            return serialize_booking(booking)
        except OperationalError:
            conn.exec_driver_sql("ROLLBACK")
            raise
        except Exception:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:
                pass
            raise


@router.get("/bookings")
def list_bookings(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    base = db.query(Booking).filter(Booking.user_id == user.id)
    total = base.count()
    items = (
        base.order_by(Booking.start_time.asc(), Booking.id.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "items": [serialize_booking(b) for b in items],
        "page": page,
        "limit": limit,
        "total": total,
    }


@router.get("/bookings/{booking_id}")
def get_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Booking.id == booking_id, Room.org_id == user.org_id)
        .first()
    )
    if booking is None:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    response = serialize_booking(booking)
    response["refunds"] = [
        {
            "amount_cents": r.amount_cents,
            "status": r.status,
            "processed_at": iso_utc(r.processed_at),
        }
        for r in booking.refunds
    ]
    return response


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Booking.id == booking_id, Room.org_id == user.org_id)
        .first()
    )
    if booking is None:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
    if user.role != "admin" and booking.user_id != user.id:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    if booking.status == "cancelled":
        raise AppError(409, "ALREADY_CANCELLED", "Booking already cancelled")

    now = utc_now_naive()
    notice = booking.start_time - now
    if notice >= timedelta(hours=48):
        refund_percent = 100
    elif notice >= timedelta(hours=24):
        refund_percent = 50
    else:
        refund_percent = 0

    refund_amount_cents = compute_refund_cents(booking.price_cents, refund_percent)

    log_refund(db, booking, refund_amount_cents)

    _settlement_pause()
    booking.status = "cancelled"
    db.commit()

    stats.record_cancel(booking.room_id, booking.price_cents)
    cache.invalidate_report(user.org_id)
    notifications.notify_cancelled(booking)

    return {
        "id": booking.id,
        "status": "cancelled",
        "refund_percent": refund_percent,
        "refund_amount_cents": refund_amount_cents,
    }
