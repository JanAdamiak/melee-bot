"""Priority scheduling for melee.gg fetches.

melee.gg's crawl-delay throttle (inside melee_tiebreakers.melee_client) is a
single shared resource: every fetch across every /watch and /topcut session
serializes through it, one request every 5 seconds, in whatever order they're
called. That gives FIFO ordering only — nothing can "jump the queue" once a
coroutine is already waiting on the throttle. FetchScheduler sits above it:
every melee.gg-touching fetch in the bot goes through `submit()` instead of
being called directly, so a bubble-mode /topcut session's fetch can be placed
ahead of routine /watch polling that's still waiting to start.

Priority applies at "one whole fetch operation" granularity, not per HTTP
call — a submitted fetch's own internal sequence of calls (e.g. get_matches
then get_standings) stays intact once it starts; only the START order of
separate fetch operations is reordered.
"""

import asyncio
import heapq
import itertools
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

PRIORITY_TOPCUT_BUBBLE = 0
PRIORITY_TOPCUT_NORMAL = 5
PRIORITY_WATCH = 10
PRIORITY_DISCOVERY = 20   # background roster scans: never ahead of live tracking


class FetchScheduler:
    """Sole entry point for melee.gg calls across the whole bot process."""

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, Callable[[], Awaitable], asyncio.Future]] = []
        self._counter = itertools.count()
        self._wakeup = asyncio.Event()
        self._drain_task: asyncio.Task | None = None
        self._bubble_tournaments: set[int] = set()

    async def start(self) -> None:
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain())

    async def stop(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            self._drain_task = None

    async def submit(self, priority: int, fn: Callable[[], Awaitable[T]]) -> T:
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        heapq.heappush(self._heap, (priority, next(self._counter), fn, future))
        self._wakeup.set()
        return await future

    def set_bubble(self, tournament_id: int, active: bool) -> None:
        if active:
            self._bubble_tournaments.add(tournament_id)
        else:
            self._bubble_tournaments.discard(tournament_id)

    @property
    def bubble_active(self) -> bool:
        return bool(self._bubble_tournaments)

    async def _drain(self) -> None:
        while True:
            if not self._heap:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            _, _, fn, future = heapq.heappop(self._heap)
            try:
                result = await fn()
                if not future.done():
                    future.set_result(result)
            except Exception as e:  # noqa: BLE001 — propagate to the caller awaiting submit()
                if not future.done():
                    future.set_exception(e)
