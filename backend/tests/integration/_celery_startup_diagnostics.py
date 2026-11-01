"""Issue #255 (A8): instrument-only diagnostics around `celery.contrib.testing.worker.start_worker`.

Emits single `A8DIAG {json}` stdout lines: per-queue broker depth immediately before the worker starts, how long
`start_worker(..., perform_ping_check=True)` took to return or raise, and which task NAMES the embedded worker
received / started / finished, so a reader can tell whether it consumed pre-existing messages before the startup ping.

Records no task ids, arguments, kwargs or message bodies. Never purges or modifies the broker, never changes
start_worker's arguments, and never swallows or replaces the exception start_worker (or the test body) raises: every
diagnostic step (signal connect/disconnect, signal handlers, Redis reads, JSON encoding, printing) is guarded, and a
diagnostic failure is reported only as text in the record or silently dropped."""

import contextlib
import json
import threading
import time

from celery.contrib.testing.worker import start_worker
from celery.signals import task_postrun, task_prerun, task_received

_PING_TASK = "celery.ping"
_KOMBU_PRIORITY_SEP = "\x06\x16"  # kombu redis transport: priority-suffixed list names
_KOMBU_PRIORITIES = (0, 3, 6, 9)
_MAX_SAMPLE_EVENTS = 200  # bounds only the raw event sample; the counters and ping timestamps below are never capped
_MAX_DISTINCT_NAMES = 64


def _emit(kind: str, **fields) -> None:
    try:
        print("A8DIAG " + json.dumps({"kind": kind, **fields}, sort_keys=True, default=str), flush=True)
    except Exception:  # noqa: BLE001 -- a diagnostic output failure must never alter the test result
        pass


def broker_depths(celery_app) -> dict:
    """Read-only list lengths of every declared queue (all kombu priority variants) plus the unacked hash size."""
    try:
        import redis

        client = redis.Redis.from_url(celery_app.conf.broker_url, socket_timeout=2, socket_connect_timeout=2)
        try:
            declared = celery_app.conf.task_queues or {}
            names = {getattr(q, "name", q) for q in declared}
            names.add(celery_app.conf.task_default_queue or "celery")
            depths = {}
            for name in sorted(str(n) for n in names):
                total = 0
                for priority in _KOMBU_PRIORITIES:
                    key = name if priority == 0 else f"{name}{_KOMBU_PRIORITY_SEP}{priority}"
                    total += int(client.llen(key))
                depths[name] = total
            return {"queues": depths, "total_queued": sum(depths.values()), "unacked": int(client.hlen("unacked"))}
        finally:
            with contextlib.suppress(Exception):
                client.close()
    except Exception as exc:  # noqa: BLE001
        return {"unavailable": type(exc).__name__}


class _WorkerEventLog:
    """Thread-safe. Counters and ping timestamps are independent of the bounded raw-event sample."""

    def __init__(self, t0: float) -> None:
        self._t0 = t0
        self._lock = threading.Lock()
        self._sample: list[list] = []  # [ms, phase, name], first _MAX_SAMPLE_EVENTS only
        self._sample_dropped = 0
        self._received: dict[str, int] = {}
        self._finished: dict[str, int] = {}
        self._non_ping_received_before_ping = 0
        self._ping_received_ms: int | None = None
        self._ping_finished_ms: int | None = None
        self._connected: list = []

    def _record(self, phase: str, name: str | None) -> None:
        name = name or "?"
        with self._lock:
            ms = round((time.monotonic() - self._t0) * 1000)
            if len(self._sample) < _MAX_SAMPLE_EVENTS:
                self._sample.append([ms, phase, name])
            else:
                self._sample_dropped += 1
            if phase == "received":
                self._count(self._received, name)
                if name == _PING_TASK:
                    if self._ping_received_ms is None:
                        self._ping_received_ms = ms
                elif self._ping_received_ms is None:
                    self._non_ping_received_before_ping += 1
            elif phase == "finished":
                self._count(self._finished, name)
                if name == _PING_TASK and self._ping_finished_ms is None:
                    self._ping_finished_ms = ms

    @staticmethod
    def _count(counter: dict, name: str) -> None:
        key = name if name in counter or len(counter) < _MAX_DISTINCT_NAMES else "<other>"
        counter[key] = counter.get(key, 0) + 1

    def on_received(self, sender=None, request=None, **_kw) -> None:
        with contextlib.suppress(Exception):
            self._record("received", getattr(request, "name", None))

    def on_prerun(self, sender=None, **_kw) -> None:
        with contextlib.suppress(Exception):
            self._record("started", getattr(sender, "name", None))

    def on_postrun(self, sender=None, **_kw) -> None:
        with contextlib.suppress(Exception):
            self._record("finished", getattr(sender, "name", None))

    def connect(self) -> None:
        for signal, handler in (
            (task_received, self.on_received),
            (task_prerun, self.on_prerun),
            (task_postrun, self.on_postrun),
        ):
            try:
                signal.connect(handler, weak=False)
                self._connected.append((signal, handler))
            except Exception:  # noqa: BLE001
                pass

    def disconnect(self) -> None:
        for signal, handler in self._connected:
            with contextlib.suppress(Exception):
                signal.disconnect(handler)
        self._connected = []

    def summary(self) -> dict:
        try:
            with self._lock:
                return {
                    "received_by_name": dict(self._received),
                    "finished_by_name": dict(self._finished),
                    "non_ping_received_before_ping_received": self._non_ping_received_before_ping,
                    "ping_received_ms": self._ping_received_ms,
                    "ping_finished_ms": self._ping_finished_ms,
                    "first_events": self._sample[:40],
                    "sample_events_dropped": self._sample_dropped,
                }
        except Exception as exc:  # noqa: BLE001
            return {"summary_unavailable": type(exc).__name__}


@contextlib.contextmanager
def instrumented_start_worker(celery_app, **kwargs):
    """Drop-in for `start_worker(celery_app, **kwargs)`: identical arguments, identical exceptions."""
    t0 = time.monotonic()
    log = _WorkerEventLog(t0)
    depth_before = broker_depths(celery_app)
    log.connect()
    _emit("before_start_worker", broker_depth_before=depth_before, start_worker_kwargs=sorted(kwargs))
    started = False
    try:
        with start_worker(celery_app, **kwargs) as worker:
            started = True
            _emit(
                "start_worker_returned",
                elapsed_ms=round((time.monotonic() - t0) * 1000),
                broker_depth_before=depth_before,
                broker_depth_now=broker_depths(celery_app),
                **log.summary(),
            )
            try:
                yield worker
            finally:
                _emit(
                    "test_body_done",
                    elapsed_ms=round((time.monotonic() - t0) * 1000),
                    broker_depth_now=broker_depths(celery_app),
                    **log.summary(),
                )
    except BaseException as exc:
        if not started:
            _emit(
                "start_worker_raised",
                exception=f"{type(exc).__module__}.{type(exc).__name__}",
                elapsed_ms=round((time.monotonic() - t0) * 1000),
                broker_depth_before=depth_before,
                broker_depth_now=broker_depths(celery_app),
                **log.summary(),
            )
        raise
    finally:
        log.disconnect()
