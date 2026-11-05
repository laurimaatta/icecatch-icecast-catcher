"""Change notifications for live UI updates.

Every database write and every change in the recorder's in-memory state bumps a
revision number. The /api/events endpoint waits on it and tells browsers to reload.
"""
import threading

_cond = threading.Condition()
_revision = 0


def notify():
    global _revision
    with _cond:
        _revision += 1
        _cond.notify_all()


def revision():
    return _revision


def wait(since, timeout):
    """Blocks until the revision differs from `since` or the timeout passes.
    Returns the current revision."""
    with _cond:
        _cond.wait_for(lambda: _revision != since, timeout)
        return _revision
