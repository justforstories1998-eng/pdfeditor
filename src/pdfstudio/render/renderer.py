"""High-performance page rasteriser with caching, tiling and prefetch.

Design
------
* **Qt-free.**  Produces :class:`RenderedPage` objects holding raw RGB(A) bytes;
  the UI wraps them in ``QImage`` without copying.
* **Cached.**  Memory LRU (bytes-bounded) plus an optional disk cache keyed by
  document fingerprint, so re-opening a file is instant.
* **Tiled.**  At high zoom only the visible tiles are rasterised, keeping
  memory flat for very large pages and 100 000-page documents.
* **Asynchronous.**  :meth:`PageRenderer.request` returns immediately and calls
  back on completion; requests are de-duplicated and cancellable.
* **Correct.**  Rendering holds the document lock because MuPDF is not
  thread-safe; the lock is released between pages so the UI stays responsive.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import pymupdf as fitz

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.exceptions import RenderError
from pdfstudio.core.jobs import Job, JobManager, jobs
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.settings import settings
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import Rect
from pdfstudio.render.cache import DiskCache, GenerationTracker, MemoryCache

log = get_logger("renderer")

ColorMode = Literal["rgb", "rgba", "gray"]


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """Immutable description of what to rasterise."""

    document_id: str
    page: int
    zoom: float = 1.0
    rotation: int = 0
    dpi: int = 72
    clip: Rect | None = None
    alpha: bool = False
    annotations: bool = True
    invert: bool = False
    gray: bool = False
    generation: int = 0
    priority: int = 0  # lower runs first

    def cache_key(self, fingerprint: str = "") -> str:
        clip = (
            f"{self.clip.x0:.1f},{self.clip.y0:.1f},{self.clip.x1:.1f},{self.clip.y1:.1f}"
            if self.clip
            else "full"
        )
        return (
            f"{fingerprint or self.document_id}|{self.page}|{self.zoom:.4f}|"
            f"{self.rotation}|{self.dpi}|{clip}|{int(self.alpha)}{int(self.annotations)}"
            f"{int(self.invert)}{int(self.gray)}|g{self.generation}"
        )


@dataclass(slots=True)
class RenderedPage:
    """A rasterised page (or tile) ready to be shown."""

    page: int
    width: int
    height: int
    stride: int
    samples: bytes
    channels: int
    zoom: float
    rotation: int
    clip: Rect | None = None
    duration_ms: float = 0.0
    from_cache: bool = False

    @property
    def nbytes(self) -> int:
        return len(self.samples)

    @property
    def has_alpha(self) -> bool:
        return self.channels == 4


class PageRenderer:
    """Renders pages of one document with caching and background execution."""

    def __init__(
        self,
        document: PdfDocument,
        *,
        job_manager: JobManager | None = None,
        memory_cache: MemoryCache[str, RenderedPage] | None = None,
        disk_cache: DiskCache | None = None,
        generations: GenerationTracker | None = None,
    ) -> None:
        perf = settings().data.performance
        self.doc = document
        self._explicit_jobs = job_manager
        # ``or`` cannot be used here: MemoryCache defines __len__, so an empty
        # (freshly injected) cache is falsy and would be silently replaced.
        self._cache = (
            memory_cache
            if memory_cache is not None
            else MemoryCache(perf.page_cache_mb * 1024 * 1024, name="pages")
        )
        self._disk = (
            disk_cache if disk_cache is not None else (DiskCache() if perf.disk_cache else None)
        )
        self._generations = generations if generations is not None else GenerationTracker()
        self._inflight: dict[str, Job[RenderedPage]] = {}
        self._lock = threading.RLock()
        self._fingerprint = ""
        self.tile_size = perf.tile_size
        self.prefetch_count = perf.prefetch_pages

    @property
    def _jobs(self) -> JobManager:
        """Resolve the job manager lazily so a restarted pool is picked up."""
        return self._explicit_jobs or jobs()

    # -- keys and invalidation ---------------------------------------------- #
    @property
    def fingerprint(self) -> str:
        """Content hash used for disk-cache keys (computed lazily, once)."""
        if not self._fingerprint:
            try:
                self._fingerprint = self.doc.fingerprint()
            except Exception:
                self._fingerprint = self.doc.id
        return self._fingerprint

    @property
    def generation(self) -> int:
        return self._generations.current(self.doc.id)

    def invalidate(self, page: int | None = None) -> None:
        """Discard cached renders after an edit.

        The generation counter is always bumped, which is what keeps the
        *disk* cache honest: its keys embed the generation, so a stale PNG can
        never be served after a page has changed. Clearing only the in-memory
        entries (as an earlier version did) meant an edit re-loaded the old
        image straight back off disk.
        """
        self._generations.bump(self.doc.id)
        if page is None:
            self._cache.invalidate(lambda k: k.startswith(f"{self.fingerprint}|"))
            self._fingerprint = ""
        else:
            prefix = f"{self.fingerprint}|{page}|"
            self._cache.invalidate(lambda k: k.startswith(prefix))
        log.debug("Invalidated render cache (page={})", page)

    # -- synchronous rendering ---------------------------------------------- #
    def render(self, request: RenderRequest) -> RenderedPage:
        """Rasterise synchronously, consulting the caches first."""
        key = request.cache_key(self.fingerprint)
        cached = self._cache.get(key)
        if cached is not None:
            cached.from_cache = True
            return cached

        if self._disk is not None and request.clip is None:
            blob = self._disk.get(key)
            if blob is not None:
                page = _decode_png(blob, request)
                if page is not None:
                    page.from_cache = True
                    self._cache.put(key, page, page.nbytes)
                    return page

        started = time.perf_counter()
        try:
            with self.doc.locked() as handle:
                page = handle[request.page]
                scale = request.zoom * (request.dpi / 72.0)
                matrix = fitz.Matrix(scale, scale)
                if request.rotation:
                    matrix = matrix.prerotate(request.rotation)
                colorspace = fitz.csGRAY if request.gray else fitz.csRGB
                pix = page.get_pixmap(
                    matrix=matrix,
                    clip=fitz.Rect(*request.clip) if request.clip else None,
                    alpha=request.alpha,
                    colorspace=colorspace,
                    annots=request.annotations,
                )
                if request.invert:
                    pix.invert_irect(pix.irect)
                result = RenderedPage(
                    page=request.page,
                    width=pix.width,
                    height=pix.height,
                    stride=pix.stride,
                    samples=pix.samples,
                    channels=pix.n,
                    zoom=request.zoom,
                    rotation=request.rotation,
                    clip=request.clip,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
        except Exception as exc:
            raise RenderError(
                f"Could not render page {request.page + 1}", detail=str(exc)
            ) from exc

        self._cache.put(key, result, result.nbytes)
        if self._disk is not None and request.clip is None and result.nbytes < 8 << 20:
            try:
                with self.doc.locked() as handle:
                    pix = fitz.Pixmap(
                        fitz.csRGB if result.channels >= 3 else fitz.csGRAY,
                        fitz.IRect(0, 0, result.width, result.height),
                        result.has_alpha,
                    )
                    pix.samples_mv[:] = result.samples
                    self._disk.put(key, pix.tobytes("png"))
            except Exception:
                log.debug("Skipped disk cache for page {}", request.page)

        bus().publish(
            Topic.RENDER_COMPLETED,
            {
                "document_id": self.doc.id,
                "page": request.page,
                "ms": result.duration_ms,
            },
            source="renderer",
        )
        return result

    # -- asynchronous rendering --------------------------------------------- #
    def request(
        self,
        request: RenderRequest,
        callback: Callable[[RenderedPage], None] | None = None,
        *,
        error_callback: Callable[[BaseException], None] | None = None,
    ) -> Job[RenderedPage] | RenderedPage:
        """Render in the background.

        Returns the :class:`RenderedPage` immediately when it is already cached,
        otherwise a :class:`Job` whose completion invokes ``callback``.
        """
        key = request.cache_key(self.fingerprint)
        cached = self._cache.get(key)
        if cached is not None:
            cached.from_cache = True
            if callback:
                callback(cached)
            return cached

        with self._lock:
            existing = self._inflight.get(key)
            if existing is not None and not existing.is_done:
                if callback:
                    existing.add_done_callback(
                        lambda job: _dispatch(job, callback, error_callback)
                    )
                return existing

            job = self._jobs.submit(
                f"Render page {request.page + 1}",
                self.render,
                request,
                tags=("render", self.doc.id),
            )
            self._inflight[key] = job

        def _cleanup(finished: Job[RenderedPage]) -> None:
            with self._lock:
                self._inflight.pop(key, None)
            _dispatch(finished, callback, error_callback)

        job.add_done_callback(_cleanup)
        return job

    def cancel_all(self) -> int:
        """Cancel outstanding render jobs (called when the view scrolls away)."""
        return self._jobs.cancel_all(tag=self.doc.id)

    # -- tiling -------------------------------------------------------------- #
    def tiles_for(
        self, page: int, zoom: float, viewport: Rect, *, rotation: int = 0
    ) -> list[RenderRequest]:
        """Split the visible area into tile-sized render requests."""
        width, height = self.doc.page_size(page)
        tile = self.tile_size / max(zoom, 0.01)
        requests: list[RenderRequest] = []
        x = max(0.0, (viewport.x0 // tile) * tile)
        while x < min(width, viewport.x1):
            y = max(0.0, (viewport.y0 // tile) * tile)
            while y < min(height, viewport.y1):
                clip = Rect(x, y, min(x + tile, width), min(y + tile, height))
                if clip.intersects(viewport) and not clip.is_empty:
                    requests.append(
                        RenderRequest(
                            document_id=self.doc.id,
                            page=page,
                            zoom=zoom,
                            rotation=rotation,
                            clip=clip,
                            generation=self.generation,
                        )
                    )
                y += tile
            x += tile
        return requests

    def should_tile(self, page: int, zoom: float) -> bool:
        """Tile when the full raster would exceed roughly 24 MB."""
        width, height = self.doc.page_size(page)
        return (width * zoom) * (height * zoom) * 3 > 24 * 1024 * 1024

    # -- prefetch ------------------------------------------------------------ #
    def prefetch(
        self, around: int, zoom: float, *, rotation: int = 0, count: int | None = None
    ) -> list[Job[RenderedPage]]:
        """Warm the cache for pages surrounding ``around``."""
        span = count if count is not None else self.prefetch_count
        started: list[Job[RenderedPage]] = []
        for offset in range(1, span + 1):
            for page in (around + offset, around - offset):
                if not 0 <= page < self.doc.page_count:
                    continue
                request = RenderRequest(
                    document_id=self.doc.id,
                    page=page,
                    zoom=zoom,
                    rotation=rotation,
                    generation=self.generation,
                    priority=offset,
                )
                result = self.request(request)
                if isinstance(result, Job):
                    started.append(result)
        return started

    # -- thumbnails ----------------------------------------------------------- #
    def thumbnail(self, page: int, size: int = 160) -> RenderedPage:
        """Render a fitted thumbnail (cached like any other render)."""
        width, height = self.doc.page_size(page)
        zoom = size / max(width, height, 1)
        return self.render(
            RenderRequest(
                document_id=self.doc.id,
                page=page,
                zoom=zoom,
                annotations=True,
                generation=self.generation,
            )
        )

    # -- export helpers -------------------------------------------------------- #
    def to_png(self, page: int, dpi: int = 150, *, alpha: bool = False) -> bytes:
        """Rasterise a page directly to PNG bytes."""
        with self.doc.locked() as handle:
            pix = handle[page].get_pixmap(dpi=dpi, alpha=alpha)
            return pix.tobytes("png")

    def statistics(self) -> dict[str, Any]:
        stats = self._cache.stats.as_dict()
        stats["disk_mb"] = (
            round(self._disk.size_bytes() / (1024 * 1024), 1) if self._disk else 0
        )
        stats["inflight"] = len(self._inflight)
        return stats


def _dispatch(
    job: Job[RenderedPage],
    callback: Callable[[RenderedPage], None] | None,
    error_callback: Callable[[BaseException], None] | None,
) -> None:
    """Deliver a finished job to the right callback, swallowing cancellations."""
    if job.future.cancelled():
        return
    error = job.future.exception()
    if error is not None:
        if error_callback:
            error_callback(error)
        return
    if callback:
        callback(job.future.result())


def _decode_png(blob: bytes, request: RenderRequest) -> RenderedPage | None:
    """Rehydrate a cached PNG into a :class:`RenderedPage`."""
    try:
        pix = fitz.Pixmap(blob)
        return RenderedPage(
            page=request.page,
            width=pix.width,
            height=pix.height,
            stride=pix.stride,
            samples=pix.samples,
            channels=pix.n,
            zoom=request.zoom,
            rotation=request.rotation,
        )
    except Exception:
        return None
