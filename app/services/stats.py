"""Live per-room booking statistics.

Confirmed-booking counts and revenue are derived directly from the bookings
table so the values are guaranteed to match the source of truth — even after
process restarts, cache loss, or in-memory counter drift.
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Booking


def get(db: Session, room_id: int) -> dict:
    count = (
        db.query(func.count(Booking.id))
        .filter(Booking.room_id == room_id, Booking.status == "confirmed")
        .scalar()
    ) or 0
    revenue = (
        db.query(func.coalesce(func.sum(Booking.price_cents), 0))
        .filter(Booking.room_id == room_id, Booking.status == "confirmed")
        .scalar()
    ) or 0
    return {"count": int(count), "revenue": int(revenue)}


# Kept as no-ops for backward compatibility with callers; the values are
# now derived from the database.
def record_create(room_id: int, price_cents: int) -> None:  # pragma: no cover
    return None


def record_cancel(room_id: int, price_cents: int) -> None:  # pragma: no cover
    return None
