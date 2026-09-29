"""Race-condition tests for bridge/server.py busy handling.

The current implementation uses an atomic `_pi_busy` flag: the check and the
set happen back-to-back with no `await` in between, so in asyncio's
single-threaded event loop two concurrent `pi_ask` calls can never both pass
the busy check. These tests lock in that guarantee.
"""
import asyncio
from unittest.mock import patch

import pytest


@pytest.fixture
def reset_server_state():
    """Reset server module-level state between tests."""
    import bridge.server as server

    server._completed.clear()
    server._pi_busy = False
    yield
    server._pi_busy = False


@pytest.mark.asyncio
async def test_concurrent_pi_ask_only_one_executes(reset_server_state):
    """Three simultaneous pi_ask calls -> exactly one execution, two busy replies."""
    import bridge.server as server

    execution_count = 0
    concurrent = 0
    max_concurrent = 0

    async def fake_run_pi_sync(prompt, timeout, gen):
        nonlocal execution_count, concurrent, max_concurrent
        execution_count += 1
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return True, f"result for {prompt}", None

    with patch.object(server, "_run_pi_sync", side_effect=fake_run_pi_sync):
        results = await asyncio.gather(
            server.pi_ask("task 1"),
            server.pi_ask("task 2"),
            server.pi_ask("task 3"),
            return_exceptions=True,
        )

    assert execution_count == 1, f"expected 1 execution, got {execution_count}"
    assert max_concurrent <= 1, f"expected no overlap, got max_concurrent={max_concurrent}"

    busy = [r for r in results if isinstance(r, str) and "忙" in r]
    assert len(busy) >= 2, f"expected >=2 busy replies, got {results}"


@pytest.mark.asyncio
async def test_pi_busy_flag_is_reset_after_completion(reset_server_state):
    """After a task completes, a follow-up pi_ask must be allowed to run."""
    import bridge.server as server

    calls = []

    async def fake_run_pi_sync(prompt, timeout, gen):
        calls.append(prompt)
        return True, f"output:{prompt}", None

    with patch.object(server, "_run_pi_sync", side_effect=fake_run_pi_sync):
        first = await server.pi_ask("first")
        second = await server.pi_ask("second")

    assert "output:first" in first
    assert "output:second" in second
    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_pi_busy_flag_reset_even_on_error(reset_server_state):
    """The busy flag must be cleared even when _run_pi_sync raises."""
    import bridge.server as server

    async def failing_run(prompt, timeout, gen):
        raise RuntimeError("boom")

    with patch.object(server, "_run_pi_sync", side_effect=failing_run):
        with pytest.raises(RuntimeError):
            await server.pi_ask("failing")

    assert server._pi_busy is False
