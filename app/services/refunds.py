"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents. The caller is responsible for
committing the transaction.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Booking, RefundLog


def compute_refund_cents(price_cents: int, percent: int) -> int:
    """Compute the refund amount in cents, rounding half-cents up.

    Example: 50% of 1001 cents = 500.5 → 501 cents (half-cent rounds up).
    The integer-only round-half-up formula is ``(n + d/2) // d`` where d=100.
    """
    numerator = price_cents * percent
    return (numerator + 50) // 100


def log_refund(db: Session, booking: Booking, amount_cents: int) -> RefundLog:
    """Create a RefundLog entry. Does not commit; the caller commits."""
    entry = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.utcnow(),
    )
    db.add(entry)
    db.flush()
    db.refresh(entry)
    return entry
