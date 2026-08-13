"""Shared render pool, owned by neither consumer.

The Discord cog and the HTTP API both need to run replay renders in worker
processes, and they must share ONE pool: ``MAX_WORKERS`` is sized against the
host's CPU and memory budget, so two independent pools would oversubscribe it.
This module owns that pool plus the ``multiprocessing.Manager`` whose queues
carry progress back from the workers.

Deliberately free of any discord import — the API imports this module too.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import multiprocessing
import queue
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import Manager
from typing import Any

from bot import metrics
from bot.config import BotConfig

log = logging.getLogger(__name__)


class RenderService:
    """Owns the render ProcessPoolExecutor and the progress-queue Manager.

    Created once per process (in ``bot.main.setup_hook``) and handed to every
    consumer, so pool rebuilds and the in-flight metric stay global.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._pool = self._make_pool()
        self._pool_lock = asyncio.Lock()
        self._manager = Manager()
        self._shut_down = False

    @property
    def pool(self) -> ProcessPoolExecutor:
        """The current pool. Callers that need to survive a rebuild should
        hoist this once and pass it back to :meth:`replace_broken_pool`."""
        return self._pool

    def progress_queue(self) -> queue.Queue[Any]:
        """A fresh Manager queue for worker → parent progress messages.

        Messages are 2-tuples: ``("status", str)`` or ``(current, total)``.
        Drop the reference once the render is finalized — the manager-side
        queue lives as long as any proxy to it does.
        """
        return self._manager.Queue()

    def _make_pool(self) -> ProcessPoolExecutor:
        # forkserver, not the default "fork": this process imports
        # renderer.stats_board (cairo) for the Statistics button and draws
        # with it on an asyncio.to_thread worker thread. fork() only
        # duplicates the calling thread, so if a fork lands while that thread
        # holds one of cairo's global font-cache mutexes, the lock's owner
        # never gets copied into the child — the mutex is locked forever and
        # that worker wedges until RENDER_TIMEOUT with no log trace.
        # forkserver forks from a clean, single-threaded helper process
        # instead, so it can never observe that lock held, and — unlike
        # "spawn" — it does not re-import every module or reload the 15 MB
        # GameParams pickle, so it doesn't reintroduce the ~5-10s-per-worker
        # cost documented on BotConfig.render_max_tasks_per_child. Do not
        # "simplify" this back to the default fork context.
        return ProcessPoolExecutor(
            max_workers=self.config.max_workers,
            max_tasks_per_child=self.config.render_max_tasks_per_child,
            mp_context=multiprocessing.get_context("forkserver"),
        )

    async def replace_broken_pool(self, broken: ProcessPoolExecutor) -> ProcessPoolExecutor:
        async with self._pool_lock:
            if self._pool is broken:
                log.warning(
                    "ProcessPool broken, rebuilding (max_workers=%d, max_tasks_per_child=%s)",
                    self.config.max_workers,
                    self.config.render_max_tasks_per_child
                    if self.config.render_max_tasks_per_child is not None
                    else "unlimited",
                )
                broken.shutdown(wait=False, cancel_futures=True)
                self._pool = self._make_pool()
                metrics.record_pool_rebuild()
            return self._pool

    async def submit(
        self, render_call: functools.partial,
    ) -> tuple[ProcessPoolExecutor, asyncio.Future]:
        """Submit a render call to the pool, transparently rebuilding once if the pool is already broken.

        Returns the pool it actually submitted to, so the caller's own
        BrokenProcessPool handler rebuilds the right object.
        """
        loop = asyncio.get_running_loop()
        pool = self._pool
        try:
            future = loop.run_in_executor(pool, render_call)
        except BrokenProcessPool:
            pool = await self.replace_broken_pool(pool)
            future = loop.run_in_executor(pool, render_call)
        metrics.track_pool_future(future)
        return pool, future

    def shutdown(self) -> None:
        """Tear down pool and manager. Idempotent — there are two consumers
        and only one process-wide teardown path."""
        if self._shut_down:
            return
        self._shut_down = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._manager.shutdown()
