"""In-memory pub/sub for high-frequency live signals.

Levels, transcript segments and capture status arrive many times a second;
putting them on the persistent wf_events bus would turn a UI refresh into a
disk write. They go here instead, and only CALL_STARTED / CALL_ENDED go to the
durable bus.

Queues are bounded and drop the OLDEST message on overflow: a stalled browser
tab must never make the capture pipeline block, and for a live view the newest
level reading is the only one worth keeping.

Topic convention: f"call:{call_id}", messages {"type": "level" | "segment" |
"status" | "alert" | "ended", ...}.
"""
import queue
import threading
import time
from collections import defaultdict


def topic_for(call_id: str) -> str:
    return f"call:{call_id}"


class Hub:
    def __init__(self, maxsize: int = 500):
        self.maxsize = maxsize
        self._lock = threading.Lock()
        self._subs: dict[str, list[queue.Queue]] = defaultdict(list)
        self._latest: dict[str, dict[str, dict]] = defaultdict(dict)
        self.dropped = 0

    def subscribe(self, topic: str, maxsize: int | None = None) -> queue.Queue:
        q = queue.Queue(maxsize=maxsize or self.maxsize)
        with self._lock:
            self._subs[topic].append(q)
        return q

    def unsubscribe(self, topic: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subs.get(topic, [])
            if q in subs:
                subs.remove(q)
            if not subs:
                self._subs.pop(topic, None)

    def publish(self, topic: str, message: dict) -> int:
        """Deliver to every subscriber of `topic`; returns how many received it."""
        message = dict(message)
        message.setdefault("ts", time.time())
        with self._lock:
            if "type" in message:
                self._latest[topic][message["type"]] = message
            subs = list(self._subs.get(topic, ()))
            for q in subs:
                self._put_drop_oldest(q, message)
        return len(subs)

    def _put_drop_oldest(self, q: queue.Queue, message: dict) -> None:
        while True:
            try:
                q.put_nowait(message)
                return
            except queue.Full:
                try:
                    q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass

    def latest(self, topic: str) -> dict[str, dict]:
        """Most recent message of each type, for late subscribers and status()."""
        with self._lock:
            return dict(self._latest.get(topic, {}))

    def subscribers(self, topic: str) -> int:
        with self._lock:
            return len(self._subs.get(topic, ()))

    def clear(self, topic: str) -> None:
        with self._lock:
            self._latest.pop(topic, None)


hub = Hub()
publish = hub.publish
subscribe = hub.subscribe
unsubscribe = hub.unsubscribe
latest = hub.latest
