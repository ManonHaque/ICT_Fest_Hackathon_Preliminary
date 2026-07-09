"""Helpers for parsing input datetimes and rendering UTC responses."""
from datetime import datetime, timezone


def parse_input_datetime(value: str) -> datetime:
    """Parse an ISO 8601 datetime into a naive UTC datetime for storage.

    Inputs that carry a UTC offset are normalised to UTC **first**, and then
    the tzinfo is dropped so the returned value is comparable to the other
    naive UTC datetimes stored in the database. Inputs without a tzinfo are
    assumed to already be in UTC.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def utc_now_naive() -> datetime:
    """Return the current UTC time as a naive datetime (matches DB columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(dt: datetime) -> str:
    """Render a stored (naive UTC) datetime with an explicit UTC designator."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
