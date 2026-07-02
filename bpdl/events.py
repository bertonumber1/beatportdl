from __future__ import annotations

import queue
import threading


class EventBus:
    """Thread-safe pub/sub used to bridge download-worker threads (which run
    the existing blocking client/handlers code) to the web UI's async SSE
    stream. Each subscriber gets its own queue so multiple browser tabs can
    watch the same run independently."""

    def __init__(self) -> None:
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(event)
