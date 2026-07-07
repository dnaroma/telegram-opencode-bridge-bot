import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from handlers.messages import ensure_server_running
from opencode import server


class _StartupMessage:
    def __init__(self) -> None:
        self.edit_text = AsyncMock()
        self.delete = AsyncMock()


class _TelegramMessage:
    def __init__(self) -> None:
        self.startup_message = _StartupMessage()
        self.reply_text = AsyncMock(return_value=self.startup_message)


class _Process:
    pid = 12345

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: int | None = None) -> int:
        return 0

    def kill(self) -> None:
        return None


class _ExitingAfterReadinessProcess:
    pid = 12346

    def __init__(self) -> None:
        self._poll_count = 0

    def poll(self) -> int | None:
        self._poll_count += 1
        return None if self._poll_count < 3 else 1

    def terminate(self) -> None:
        return None

    def wait(self, timeout: int | None = None) -> int:
        return 1

    def kill(self) -> None:
        return None


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _ClientSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def get(self, *args, **kwargs):
        return _Response()


class _PreflightClientSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def get(self, *args, **kwargs):
        raise OSError("port is free")


class _ReadinessClientSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def get(self, *args, **kwargs):
        return _Response()


class ServerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        with patch("opencode.server._kill_process_tree"):
            await server.stop_server()

    async def test_ensure_server_running_fails_safe_for_reachable_unmanaged_server(self) -> None:
        message = _TelegramMessage()
        update = SimpleNamespace(effective_message=message, message=message)
        session_mgr = Mock()
        session_mgr.get_user_work_dir = AsyncMock(return_value="/tmp/opencode-work")
        oc_client = Mock()
        oc_client.is_available = AsyncMock(return_value=True)
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(
                    opencode_work_dir="/tmp/default-work",
                    opencode_server_url="http://127.0.0.1:4096",
                ),
                "opencode_client": oc_client,
                "session_manager": session_mgr,
                "server_started": False,
            }
        )
        ensure_managed_server = AsyncMock(return_value=False)

        with patch("opencode.server.is_managed_server_running", return_value=False, create=True):
            with patch("opencode.server.ensure_managed_server", new=ensure_managed_server):
                started = await ensure_server_running(update, context, user_id=42)

        self.assertFalse(started)
        ensure_managed_server.assert_awaited_once_with(
            "/tmp/opencode-work",
            port=4096,
            hostname="127.0.0.1",
        )
        self.assertFalse(context.bot_data["server_started"])
        message.startup_message.edit_text.assert_awaited_once()

    async def test_ensure_server_running_ignores_stale_cache_for_wrong_directory(self) -> None:
        message = _TelegramMessage()
        update = SimpleNamespace(effective_message=message, message=message)
        session_mgr = Mock()
        session_mgr.get_user_work_dir = AsyncMock(return_value="/tmp/new-work")
        oc_client = Mock()
        oc_client.is_available = AsyncMock(return_value=True)
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(
                    opencode_work_dir="/tmp/default-work",
                    opencode_server_url="http://localhost:4096",
                ),
                "opencode_client": oc_client,
                "session_manager": session_mgr,
                "server_started": True,
                "server_last_check": time.monotonic(),
            }
        )
        ensure_managed_server = AsyncMock(return_value=True)

        with patch("opencode.server.is_managed_server_running", return_value=False, create=True):
            with patch("opencode.server.ensure_managed_server", new=ensure_managed_server):
                started = await ensure_server_running(update, context, user_id=42)

        self.assertTrue(started)
        ensure_managed_server.assert_awaited_once_with(
            "/tmp/new-work",
            port=4096,
            hostname="localhost",
        )

    async def test_restart_server_records_managed_server_identity(self) -> None:
        process = _Process()
        aiohttp_module = SimpleNamespace(
            ClientSession=Mock(side_effect=[_PreflightClientSession(), _ReadinessClientSession()]),
            ClientTimeout=lambda total: SimpleNamespace(total=total),
        )

        with patch("opencode.server.subprocess.Popen", return_value=process):
            with patch.dict(sys.modules, {"aiohttp": aiohttp_module}):
                with patch("opencode.server.asyncio.sleep", new=AsyncMock()):
                    started = await server.restart_server(
                        "/tmp/opencode-work",
                        port=4096,
                        hostname="127.0.0.1",
                    )

        self.assertTrue(started)
        self.assertTrue(
            server.is_managed_server_running(
                "/tmp/opencode-work",
                port=4096,
                hostname="127.0.0.1",
            )
        )

    async def test_restart_server_rejects_preexisting_unmanaged_listener(self) -> None:
        aiohttp_module = SimpleNamespace(
            ClientSession=Mock(return_value=_ReadinessClientSession()),
            ClientTimeout=lambda total: SimpleNamespace(total=total),
        )
        popen = Mock(return_value=_Process())

        with patch("opencode.server.subprocess.Popen", new=popen):
            with patch.dict(sys.modules, {"aiohttp": aiohttp_module}):
                with patch("opencode.server.asyncio.sleep", new=AsyncMock()):
                    started = await server.restart_server(
                        "/tmp/opencode-work",
                        port=4096,
                        hostname="127.0.0.1",
                    )

        self.assertFalse(started)
        popen.assert_not_called()
        self.assertFalse(
            server.is_managed_server_running(
                "/tmp/opencode-work",
                port=4096,
                hostname="127.0.0.1",
            )
        )

    async def test_ensure_managed_server_reuses_existing_managed_server(self) -> None:
        with patch("opencode.server._restart_server_unlocked", new=AsyncMock(return_value=True)) as restart:
            first_started = await server.ensure_managed_server(
                "/tmp/opencode-work",
                port=4096,
                hostname="127.0.0.1",
            )
        self.assertTrue(first_started)
        restart.assert_awaited_once()

        server._server_process = _Process()
        server._server_identity = server._server_identity_for(
            "/tmp/opencode-work",
            4096,
            "127.0.0.1",
        )
        with patch("opencode.server._restart_server_unlocked", new=AsyncMock(return_value=True)) as restart_again:
            second_started = await server.ensure_managed_server(
                "/tmp/opencode-work",
                port=4096,
                hostname="127.0.0.1",
                verify_reachable=False,
            )

        self.assertTrue(second_started)
        restart_again.assert_not_awaited()

    async def test_restart_server_rejects_health_check_when_launched_process_exits(self) -> None:
        process = _ExitingAfterReadinessProcess()
        aiohttp_module = SimpleNamespace(
            ClientSession=Mock(side_effect=[_PreflightClientSession(), _ReadinessClientSession(), _ReadinessClientSession()]),
            ClientTimeout=lambda total: SimpleNamespace(total=total),
        )

        with patch("opencode.server.subprocess.Popen", return_value=process):
            with patch.dict(sys.modules, {"aiohttp": aiohttp_module}):
                with patch("opencode.server.asyncio.sleep", new=AsyncMock()):
                    started = await server.restart_server(
                        "/tmp/opencode-work",
                        port=4096,
                        hostname="127.0.0.1",
                    )

        self.assertFalse(started)
        self.assertFalse(
            server.is_managed_server_running(
                "/tmp/opencode-work",
                port=4096,
                hostname="127.0.0.1",
            )
        )
        self.assertFalse(
            server.is_managed_server_running(
                "/tmp/other-work",
                port=4096,
                hostname="127.0.0.1",
            )
        )


if __name__ == "__main__":
    unittest.main()
