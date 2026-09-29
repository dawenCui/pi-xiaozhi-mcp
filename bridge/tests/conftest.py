"""Test fixtures for pi-xiaozhi-mcp bridge tests."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_pi_process():
    """Mock subprocess for pi command.

    Returns a configured mock process that can be customized per test.
    """
    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"test output", b""))
    return process


@pytest.fixture
def mock_subprocess_exec(mock_pi_process):
    """Patch asyncio.create_subprocess_exec to return mock process."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.return_value = mock_pi_process
        yield mock_exec


@pytest.fixture
def reset_server_state():
    """Reset server module-level state between tests."""
    import bridge.server as server

    # Clear completed deque and reset runtime state
    server._completed.clear()
    server._pi_busy = False
    server._current_proc = None
    server._current_task = ""
    server._generation = 0
    server._stop_requested_gen = 0
    server._background_tasks.clear()
    yield
