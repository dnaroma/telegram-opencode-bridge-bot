import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.commands import restart_opencode_command, set_bot_commands


class _StatusMessage:
    def __init__(self) -> None:
        self.edit_text = AsyncMock()


class _Message:
    def __init__(self) -> None:
        self.status_message = _StatusMessage()
        self.reply_text = AsyncMock(return_value=self.status_message)


class _Bot:
    def __init__(self) -> None:
        self.set_my_commands = AsyncMock()


class RestartOpenCodeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_opencode_restarts_active_workspace(self) -> None:
        # Given: the user has an active project under the configured workspace.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_mgr = SimpleNamespace(
            get_user_work_dir=AsyncMock(return_value="/tmp/opencode-base/project-a"),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(opencode_work_dir="/tmp/opencode-base"),
                "session_manager": session_mgr,
                "server_started": True,
            }
        )

        # When: the command is invoked and the restart succeeds.
        with patch("handlers.commands.restart_opencode_serve", new=AsyncMock(return_value=True)) as restart:
            await restart_opencode_command(update, context)

        # Then: the active workspace is restarted and success is reported.
        restart.assert_awaited_once_with(
            update,
            context,
            42,
            os.path.abspath("/tmp/opencode-base/project-a"),
        )
        message.reply_text.assert_awaited_once()
        message.status_message.edit_text.assert_awaited_once()
        self.assertIn("restarted", message.status_message.edit_text.await_args.args[0].lower())
        self.assertIn("project-a", message.status_message.edit_text.await_args.args[0])

    async def test_restart_opencode_reports_failure_without_marking_server_started(self) -> None:
        # Given: the user has an active project but restart fails.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_mgr = SimpleNamespace(
            get_user_work_dir=AsyncMock(return_value="/tmp/opencode-base/project-a"),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(opencode_work_dir="/tmp/opencode-base"),
                "session_manager": session_mgr,
                "server_started": True,
            }
        )

        # When: the command is invoked and restart fails.
        with patch("handlers.commands.restart_opencode_serve", new=AsyncMock(return_value=False)):
            await restart_opencode_command(update, context)

        # Then: failure is reported and cache state is not left healthy.
        message.status_message.edit_text.assert_awaited_once()
        self.assertIn("failed", message.status_message.edit_text.await_args.args[0].lower())
        self.assertFalse(context.bot_data["server_started"])

    async def test_set_bot_commands_includes_restart_opencode(self) -> None:
        # Given: a bot application ready to register menu commands.
        app = SimpleNamespace(bot=_Bot())

        # When: commands are registered.
        await set_bot_commands(app)

        # Then: Telegram's command menu includes /restart_opencode.
        commands = app.bot.set_my_commands.await_args_list[0].args[0]
        self.assertIn("restart_opencode", {command.command for command in commands})


if __name__ == "__main__":
    unittest.main()
