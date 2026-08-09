"""Background job infrastructure (thread & process pools, progress, cancellation).

The UI must never block.  Every potentially slow operation — rendering, OCR,
saving a 10 000 page document, batch conversion — is submitted here and reports
progress through the :mod:`pdfstudio.core.events` bus.

Two executors are available:

``JobManager.submit``          CPU-light / IO bound work on a thread pool.
``JobManager.submit_process``  CPU-heavy work (OCR, image ops) on a process pool.

Jobs support cooperative cancellation: the callable receives a
:class:`JobContext` it should poll via :meth:`JobContext.raise_if_cancelled`.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Any, Generic, ParamSpec, TypeVar

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.logging_setup import get_logger

log = get_logger("jobs")

P = ParamSpec("P")
R = TypeVar("R")


class JobState(StrEnum):
    PENDING = auto()
    RUNNING = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELLED = auto()


class JobCancelled(Exception):
    """Raised inside a worker when cancellation has been requested."""


@dataclass(slots=True)
class JobProgress:
    """Progress snapshot published on :data:`Topic.JOB_PROGRESS`."""

    job_id: str
    name: str
    current: int = 0
    total: int = 0
    message: str = ""

    @property
    def fraction(self) -> float:
        return (self.current / self.total) if self.total else 0.0

    @property
    def percent(self) -> int:
        return round(self.fraction * 100)


class JobContext:
    """Handed to worker callables for progress reporting and cancellation."""

    def __init__(self, job_id: str, name: str, cancel_event: threading.Event) -> None:
        self.job_id = job_id
        self.name = name
        self._cancel = cancel_event
        self._total = 0
        self._current = 0

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def raise_if_cancelled(self) -> None:
        """Abort the worker if the user pressed Cancel."""
        if self._cancel.is_set():
            raise JobCancelled(self.name)

    def set_total(self, total: int) -> None:
        self._total = total

    def progress(self, current: int, message: str = "", total: int | None = None) -> None:
        """Publish progress; also checks for cancellation."""
        self.raise_if_cancelled()
        self._current = current
        if total is not None:
            self._total = total
        bus().publish(
            Topic.JOB_PROGRESS,
            {"progress": JobProgress(self.job_id, self.name, current, self._total, message)},
            source="jobs",
        )

    def step(self, message: str = "") -> None:
        self.progress(self._current + 1, message)


@dataclass
class Job(Generic[R]):
    """Handle to a submitted unit of work."""

    id: str
    name: str
    future: Future[R]
    cancel_event: threading.Event
    state: JobState = JobState.PENDING
    submitted: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    error: BaseException | None = None
    tags: tuple[str, ...] = ()

    def cancel(self) -> bool:
        """Request cancellation (cooperative for running jobs)."""
        self.cancel_event.set()
        if self.future.cancel():
            self.state = JobState.CANCELLED
            return True
        return False

    def result(self, timeout: float | None = None) -> R:
        """Block for the result (never call from the GUI thread)."""
        return self.future.result(timeout)

    def add_done_callback(self, fn: Callable[[Job[R]], None]) -> None:
        self.future.add_done_callback(lambda _f: fn(self))

    @property
    def duration(self) -> float:
        end = self.finished or time.time()
        return end - (self.started or self.submitted)

    @property
    def is_done(self) -> bool:
        return self.future.done()


def _pool_size(configured: int, *, reserve: int = 1) -> int:
    if configured > 0:
        return configured
    return max(1, (os.cpu_count() or 2) - reserve)


class JobManager:
    """Owns the executors and tracks every in-flight job."""

    def __init__(
        self,
        *,
        thread_workers: int = 0,
        process_workers: int = 0,
        enable_processes: bool = True,
    ) -> None:
        self._threads = ThreadPoolExecutor(
            max_workers=_pool_size(thread_workers, reserve=0),
            thread_name_prefix="pdfstudio-io",
        )
        self._processes: ProcessPoolExecutor | None = None
        self._process_workers = _pool_size(process_workers)
        self._enable_processes = enable_processes
        self._jobs: dict[str, Job[Any]] = {}
        self._lock = threading.RLock()
        self._shutdown = False
        log.debug(
            "JobManager ready (threads={}, processes={})",
            self._threads._max_workers,
            self._process_workers if enable_processes else 0,
        )

    # -- submission --------------------------------------------------------- #
    def submit(
        self,
        name: str,
        fn: Callable[..., R],
        *args: Any,
        tags: Iterable[str] = (),
        with_context: bool = False,
        **kwargs: Any,
    ) -> Job[R]:
        """Run ``fn`` on the thread pool.

        Args:
            name: Human readable job name shown in the status bar.
            with_context: When ``True`` a :class:`JobContext` is passed as the
                first positional argument so the worker can report progress.
        """
        return self._submit(self._threads, name, fn, args, kwargs, tags, with_context, "thread")

    def submit_process(
        self,
        name: str,
        fn: Callable[..., R],
        *args: Any,
        tags: Iterable[str] = (),
        **kwargs: Any,
    ) -> Job[R]:
        """Run ``fn`` in a separate process (must be importable & picklable)."""
        if not self._enable_processes:
            return self.submit(name, fn, *args, tags=tags, **kwargs)
        return self._submit(
            self._process_pool(), name, fn, args, kwargs, tags, False, "process"
        )

    def _process_pool(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._processes is None:
                self._processes = ProcessPoolExecutor(max_workers=self._process_workers)
            return self._processes

    def _submit(
        self,
        executor: Any,
        name: str,
        fn: Callable[..., R],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        tags: Iterable[str],
        with_context: bool,
        kind: str,
    ) -> Job[R]:
        if self._shutdown:
            raise RuntimeError("JobManager has been shut down")
        job_id = uuid.uuid4().hex[:12]
        cancel = threading.Event()
        ctx = JobContext(job_id, name, cancel)

        def _run() -> R:
            job = self._jobs.get(job_id)
            if job:
                job.state = JobState.RUNNING
                job.started = time.time()
            bus().publish(Topic.JOB_STARTED, {"job_id": job_id, "name": name}, source="jobs")
            if with_context:
                return fn(ctx, *args, **kwargs)
            return fn(*args, **kwargs)

        future: Future[R] = executor.submit(_run)
        job = Job(id=job_id, name=name, future=future, cancel_event=cancel, tags=tuple(tags))
        with self._lock:
            self._jobs[job_id] = job
        future.add_done_callback(lambda f: self._on_done(job, f))
        log.debug("Submitted {} job {!r} ({})", kind, name, job_id)
        return job

    def _on_done(self, job: Job[Any], future: Future[Any]) -> None:
        job.finished = time.time()
        if future.cancelled() or job.cancel_event.is_set():
            job.state = JobState.CANCELLED
            bus().publish(
                Topic.JOB_FINISHED,
                {"job_id": job.id, "name": job.name, "cancelled": True},
                source="jobs",
            )
        elif (exc := future.exception()) is not None:
            job.state = JobState.FAILED
            job.error = exc
            if not isinstance(exc, JobCancelled):
                log.opt(exception=exc).error("Job {!r} failed", job.name)
            bus().publish(
                Topic.JOB_FAILED,
                {"job_id": job.id, "name": job.name, "error": exc},
                source="jobs",
            )
        else:
            job.state = JobState.SUCCEEDED
            bus().publish(
                Topic.JOB_FINISHED,
                {
                    "job_id": job.id,
                    "name": job.name,
                    "cancelled": False,
                    "duration": job.duration,
                },
                source="jobs",
            )
        with self._lock:
            self._jobs.pop(job.id, None)

    # -- management --------------------------------------------------------- #
    def active_jobs(self) -> list[Job[Any]]:
        with self._lock:
            return list(self._jobs.values())

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        return job.cancel() if job else False

    def cancel_all(self, tag: str | None = None) -> int:
        count = 0
        for job in self.active_jobs():
            if tag is None or tag in job.tags:
                job.cancel()
                count += 1
        return count

    def shutdown(self, *, wait: bool = False) -> None:
        """Cancel outstanding work and dispose of the executors."""
        self._shutdown = True
        self.cancel_all()
        self._threads.shutdown(wait=wait, cancel_futures=True)
        if self._processes is not None:
            self._processes.shutdown(wait=wait, cancel_futures=True)
        log.debug("JobManager shut down")

    def __enter__(self) -> JobManager:
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.shutdown(wait=True)
        return False

    @property
    def is_shut_down(self) -> bool:
        return self._shutdown


_MANAGER: JobManager | None = None
_MANAGER_LOCK = threading.Lock()


def jobs() -> JobManager:
    """Process-wide :class:`JobManager` singleton.

    A manager that has been shut down (for example when the main window
    closed) is replaced transparently, so a second window — or the next test
    in a session — gets a working pool instead of ``RuntimeError``.
    """
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None or _MANAGER.is_shut_down:
            from pdfstudio.core.settings import settings

            perf = settings().data.performance
            _MANAGER = JobManager(thread_workers=perf.render_threads)
        return _MANAGER


def reset_jobs() -> None:
    """Dispose of the singleton (tests and clean shutdown)."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None and not _MANAGER.is_shut_down:
            _MANAGER.shutdown()
        _MANAGER = None
