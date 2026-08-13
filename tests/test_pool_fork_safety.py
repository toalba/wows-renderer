# tests/test_pool_fork_safety.py
"""RenderService._make_pool must use the "forkserver" multiprocessing start
method, not the default "fork".

The bot's parent process imports renderer.stats_board (cairo) for the
Statistics button and draws with it on an asyncio.to_thread worker thread.
fork() only duplicates the calling thread, so a fork landing while that
thread holds one of cairo's global font-cache mutexes leaves the mutex
locked forever in the child — a worker that wedges silently until
RENDER_TIMEOUT. forkserver forks from a clean, single-threaded helper
process instead, so it can never observe that lock held.
"""
from __future__ import annotations

from types import SimpleNamespace

from bot.render_service import RenderService


def _double(x: int) -> int:
    """Module-level so it is picklable across the process boundary."""
    return x * 2


def _stub_service(*, max_workers: int = 1, render_max_tasks_per_child: int | None = None):
    """A minimal object exposing just what _make_pool reads from self."""
    return SimpleNamespace(
        config=SimpleNamespace(
            max_workers=max_workers,
            render_max_tasks_per_child=render_max_tasks_per_child,
        ),
    )


def test_make_pool_uses_forkserver_start_method():
    pool = RenderService._make_pool(_stub_service())
    try:
        assert pool._mp_context.get_start_method() == "forkserver"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_make_pool_runs_a_trivial_task():
    """Not just configured correctly — actually usable end to end."""
    pool = RenderService._make_pool(_stub_service())
    try:
        future = pool.submit(_double, 21)
        assert future.result(timeout=60) == 42
    finally:
        pool.shutdown(wait=True)


def test_make_pool_with_recycling_still_uses_forkserver():
    """max_tasks_per_child is incompatible with an explicit "fork"
    mp_context (ProcessPoolExecutor raises ValueError) but not with
    "forkserver" — this must keep working once recycling is enabled."""
    pool = RenderService._make_pool(_stub_service(render_max_tasks_per_child=2))
    try:
        assert pool._mp_context.get_start_method() == "forkserver"
        future = pool.submit(_double, 10)
        assert future.result(timeout=60) == 20
    finally:
        pool.shutdown(wait=True)
