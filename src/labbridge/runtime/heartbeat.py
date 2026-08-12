"""Proving a worker is still alive, from a connection the work itself is not using.

A lease says "this job is mine until then". A heartbeat is what moves *then* forward while the work
is still running, and it is the only reason a long adapter call does not have to be covered by a
lease long enough to strand the job for that long after a crash.

Two properties decide whether this is worth anything:

* **It runs on its own connection.** The worker's connection is inside a transaction for the whole
  finalisation, and a heartbeat issued on it would be invisible to every other session until that
  transaction committed — which is exactly when the heartbeat no longer matters. Worse, a rollback
  would take the heartbeats with it. A separate connection makes each beat its own committed fact.
* **Its failure reaches the worker.** A heartbeat that quietly stops is the dangerous case: the
  worker keeps computing, its lease lapses, another worker takes the job, and the first one arrives
  at finalisation believing it still owns the work. So a refused beat is latched here and the worker
  reads it before finalising (F-008).

The thread is not the source of truth about ownership — the database is. This only asks, on a
schedule; every answer comes from the `_held` predicate evaluated server-side.
"""

from __future__ import annotations

import threading
from types import TracebackType
from typing import Final

from sqlalchemy import Engine

from labbridge.runtime.jobs import DEFAULT_LEASE_SECONDS, Lease, LeaseLostError, heartbeat

#: How often to beat, when the caller does not say. A third of the lease leaves room for two missed
#: beats before the lease actually lapses, which is the usual margin for a transient database blip.
DEFAULT_HEARTBEAT_SECONDS: Final = DEFAULT_LEASE_SECONDS / 3


class Heartbeat:
    """Extends one lease on a schedule until stopped, and remembers if it ever could not.

    Used as a context manager so the thread cannot outlive the work it vouches for::

        with Heartbeat(engine, lease) as beating:
            result = await adapter.execute(candidate)
            beating.raise_if_lost()
    """

    def __init__(
        self,
        engine: Engine,
        lease: Lease,
        *,
        interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._engine = engine
        self._lease = lease
        self._interval = interval_seconds
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lost: LeaseLostError | None = None
        self._beats = 0
        self._guard = threading.Lock()

    @property
    def beats(self) -> int:
        """How many beats were accepted by the database. Read by tests, not by the protocol."""
        with self._guard:
            return self._beats

    @property
    def lost(self) -> LeaseLostError | None:
        """The refusal that ended this heartbeat, if one did."""
        with self._guard:
            return self._lost

    def raise_if_lost(self) -> None:
        """Re-raise the refusal on the worker's thread, so it fails where it can be handled."""
        failure = self.lost
        if failure is not None:
            raise failure

    def __enter__(self) -> Heartbeat:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.stop()

    def start(self) -> None:
        if self._thread is not None:  # pragma: no cover - guarded by the context manager
            raise RuntimeError("this heartbeat is already running")
        # A daemon thread: if the worker process is dying, a heartbeat that kept it alive would be
        # claiming liveness on behalf of something that is not.
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{self._lease.job_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if not self._beat():
                return

    def _beat(self) -> bool:
        """One beat on its own connection. Returns False once the lease is gone for good."""
        try:
            with self._engine.begin() as connection:
                heartbeat(connection, self._lease, lease_seconds=self._lease_seconds)
        except LeaseLostError as error:
            # Terminal, and latched. The lease is not coming back: something else now holds this
            # job under a newer generation, and continuing to ask would only produce more refusals.
            with self._guard:
                self._lost = error
            return False
        except Exception:
            # A transient database failure is not proof the lease was lost, so it is not latched as
            # such — the lease will simply lapse if the outage outlasts it, and the finalisation
            # fence is what refuses the work then. Swallowed rather than raised because this runs on
            # a thread with nobody to catch it.
            return True
        with self._guard:
            self._beats += 1
        return True


__all__ = ["DEFAULT_HEARTBEAT_SECONDS", "Heartbeat"]
