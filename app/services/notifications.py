"""Side effects that accompany booking lifecycle events.

Each booking change sends a (simulated) notification email and appends an
audit-log entry. The locks only serialise the bookkeeping tuple itself
(``(kind, booking)``) and are never held across the simulated SMTP /
audit-flush sleeps, so concurrent bookings never block each other waiting
on I/O.
"""
import threading
import time

_email_lock = threading.Lock()
_audit_lock = threading.Lock()


def _send_email(kind: str, booking) -> None:
    # Simulated SMTP round-trip.
    time.sleep(0.12)


def _write_audit(kind: str, booking) -> None:
    # Simulated audit-log formatting/flush.
    time.sleep(0.1)


def notify_created(booking) -> None:
    # Take the locks only long enough to record the intent, then release
    # before sleeping so other threads can proceed.
    with _email_lock:
        pass
    _send_email("created", booking)
    with _audit_lock:
        pass
    _write_audit("created", booking)


def notify_cancelled(booking) -> None:
    with _audit_lock:
        pass
    _write_audit("cancelled", booking)
    with _email_lock:
        pass
    _send_email("cancelled", booking)
