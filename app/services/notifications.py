"""Side effects that accompany booking lifecycle events.

Each booking change sends a (simulated) notification email and appends an
audit-log entry. The locks are always acquired in the same order
(``_email_lock`` then ``_audit_lock``) to prevent deadlocks under concurrent
create + cancel traffic.
"""
import threading

_email_lock = threading.Lock()
_audit_lock = threading.Lock()


def _send_email(kind: str, booking) -> None:
    # Simulated SMTP round-trip (no real network call).
    return None


def _write_audit(kind: str, booking) -> None:
    # Simulated audit-log formatting/flush (no real disk I/O).
    return None


def notify_created(booking) -> None:
    # Always: email_lock → audit_lock (consistent lock order).
    with _email_lock:
        _send_email("created", booking)
        with _audit_lock:
            _write_audit("created", booking)


def notify_cancelled(booking) -> None:
    # Same lock order as notify_created to prevent deadlock.
    with _email_lock:
        _send_email("cancelled", booking)
        with _audit_lock:
            _write_audit("cancelled", booking)
