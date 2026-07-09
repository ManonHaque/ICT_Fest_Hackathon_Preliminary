"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents, rounded to the nearest cent with
half-cents rounding up (e.g. 50% of 1001 = 501).
"""
from decimal import ROUND_HALF_UP, Decimal
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Booking, RefundLog
from ..timeutils import utc_now_naive


def compute_refund_cents(price_cents: int, percent: int) -> int:
    """Return the refund in whole cents, half-up rounded.

    Uses Decimal arithmetic so half-cent cases (e.g. 50% of 1001) are exact.
    """
    amount = (Decimal(price_cents) * Decimal(percent) / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(amount)


def log_refund(db: Session, booking: Booking, amount_cents: int) -> RefundLog:
    """Record a refund entry for ``booking`` with the given amount in cents.

    The amount is taken from the caller (already rounded) so the value in the
    ledger always equals the value returned to the API client.
    """
    entry = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=utc_now_naive(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
