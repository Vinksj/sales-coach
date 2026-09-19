"""The workflow worker: drains wf_events one at a time in a background thread.

Heavy post-call work (final transcription, agents) waits while a live capture
is running, so a new call never competes with the previous call's analysis
for the M2's memory and GPU.
"""
import logging
import threading
import time

from ..store import stores
from . import bus, workflow

log = logging.getLogger("salescoach.worker")


class Worker(threading.Thread):
    def __init__(self, live_busy=lambda: False, poll_s: float = 1.0, db_path=None):
        super().__init__(name="salescoach-worker", daemon=True)
        self.live_busy = live_busy
        self.poll_s = poll_s
        self.db_path = db_path
        self._stop_event = threading.Event()
        self.current = None

    def stop(self):
        self._stop_event.set()

    def run(self):
        conn = None
        while not self._stop_event.is_set():
            try:
                if conn is None:
                    conn = stores.sales(self.db_path)
                    bus.recover_running(conn)
                if self.live_busy():
                    time.sleep(self.poll_s)
                    continue
                event = bus.claim_next(conn)
                if event is None:
                    time.sleep(self.poll_s)
                    continue
                self.current = event
                try:
                    workflow.handle(conn, event)
                    bus.complete(conn, event.event_id)
                except Exception as exc:
                    log.exception("event %s %s failed", event.type, event.entity_id)
                    _settle_failure(conn, event, exc)
                finally:
                    self.current = None
            except Exception:
                # A locked database or a broken connection must not end the worker thread: the
                # event stays pending (or running, recovered on the next reconnect) and we retry.
                log.exception("worker loop error; reconnecting")
                self.current = None
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                time.sleep(self.poll_s)


RATE_LIMIT_DEFER_S = 15 * 60


def _settle_failure(conn, event, exc):
    """A quota refusal waits without spending an attempt; everything else counts toward MAX_ATTEMPTS.

    The failed attempt's partial writes are discarded first, so a retry starts from a clean state."""
    try:
        conn.rollback()
    except Exception:
        pass
    if getattr(exc, "rate_limited", False) or getattr(exc.__cause__, "rate_limited", False):
        bus.defer(conn, event.event_id, RATE_LIMIT_DEFER_S, f"rate limited, retrying later: {exc}")
    else:
        bus.fail(conn, event.event_id, f"{type(exc).__name__}: {exc}")


def drain(conn, max_events: int = 100) -> int:
    """Process pending events synchronously (CLI / tests). Returns the count handled."""
    handled = 0
    while handled < max_events:
        event = bus.claim_next(conn)
        if event is None:
            break
        try:
            workflow.handle(conn, event)
            bus.complete(conn, event.event_id)
        except Exception as exc:
            _settle_failure(conn, event, exc)
        handled += 1
    return handled
