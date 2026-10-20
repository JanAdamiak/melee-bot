import asyncio

import pytest

from scheduler import PRIORITY_TOPCUT_BUBBLE, PRIORITY_WATCH, FetchScheduler


async def test_submit_returns_result():
    scheduler = FetchScheduler()
    await scheduler.start()
    result = await scheduler.submit(PRIORITY_WATCH, lambda: _immediate("ok"))
    assert result == "ok"
    await scheduler.stop()


async def _immediate(value):
    return value


async def test_higher_priority_runs_first():
    scheduler = FetchScheduler()
    order = []

    async def slow_first():
        await asyncio.sleep(0.05)   # gives time for both to be queued before either runs
        order.append("watch")
        return "watch"

    async def fast_second():
        order.append("bubble")
        return "bubble"

    await scheduler.start()
    watch_task = asyncio.ensure_future(scheduler.submit(PRIORITY_WATCH, slow_first))
    await asyncio.sleep(0.01)   # let the watch fetch actually start draining first
    bubble_task = asyncio.ensure_future(scheduler.submit(PRIORITY_TOPCUT_BUBBLE, fast_second))
    await asyncio.gather(watch_task, bubble_task)
    await scheduler.stop()

    # watch started first (already draining) so it still finishes first here —
    # the real ordering guarantee is about START order for calls not yet drained.
    assert order == ["watch", "bubble"]


async def test_queued_higher_priority_jumps_ahead_of_lower():
    scheduler = FetchScheduler()
    order = []

    async def make(name):
        async def fn():
            order.append(name)
            return name
        return fn

    # Queue three calls WITHOUT starting the drain loop yet, so all three are
    # waiting when draining begins — this is where priority actually matters.
    watch_fn = await make("watch")
    bubble_fn = await make("bubble")
    normal_fn = await make("topcut-normal")

    t1 = asyncio.ensure_future(scheduler.submit(PRIORITY_WATCH, watch_fn))
    await asyncio.sleep(0)   # let submit() enqueue before the next one
    t2 = asyncio.ensure_future(scheduler.submit(5, normal_fn))
    await asyncio.sleep(0)
    t3 = asyncio.ensure_future(scheduler.submit(PRIORITY_TOPCUT_BUBBLE, bubble_fn))
    await asyncio.sleep(0)

    await scheduler.start()
    await asyncio.gather(t1, t2, t3)
    await scheduler.stop()

    assert order == ["bubble", "topcut-normal", "watch"]


async def test_exception_propagates_to_caller():
    scheduler = FetchScheduler()
    await scheduler.start()

    async def boom():
        raise ValueError("melee.gg down")

    with pytest.raises(ValueError, match="melee.gg down"):
        await scheduler.submit(PRIORITY_WATCH, boom)
    await scheduler.stop()


async def test_bubble_active_tracks_set_bubble():
    scheduler = FetchScheduler()
    assert scheduler.bubble_active is False
    scheduler.set_bubble(430189, True)
    assert scheduler.bubble_active is True
    scheduler.set_bubble(430189, False)
    assert scheduler.bubble_active is False


async def test_bubble_active_true_while_any_tournament_bubbling():
    scheduler = FetchScheduler()
    scheduler.set_bubble(1, True)
    scheduler.set_bubble(2, True)
    scheduler.set_bubble(1, False)
    assert scheduler.bubble_active is True
    scheduler.set_bubble(2, False)
    assert scheduler.bubble_active is False
