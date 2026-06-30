"""OpenCode server process manager.

Handles starting, stopping, and restarting the `opencode serve` process
deterministically — when the user switches projects via /project, the server
is restarted from the new project directory so the AI agent is fully scoped.
"""

import asyncio
import logging
import os
import signal
import subprocess
import platform
import shutil

logger = logging.getLogger(__name__)

_server_process: subprocess.Popen | None = None
_server_port: int = 8080  # Default port is 8080 from .env


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its descendants (process tree).

    On Unix: sends SIGTERM to the process group, then SIGKILL if needed.
    On Windows: uses taskkill /T /F for tree kill.
    """
    if platform.system() == "Windows":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return

    import time
    time.sleep(1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass

def get_opencode_binary() -> str:
    """Find the opencode binary on the system."""
    path = os.environ.get("OPENCODE_BINARY", "")
    if path and os.path.isfile(path):
        return path
    found = shutil.which("opencode")
    if found:
        return found
    return "opencode"


async def restart_server(directory: str, port: int = 8080, hostname: str = "127.0.0.1") -> bool:
    """Stop the running opencode serve process and restart it from *directory*.

    Returns True if the server came up successfully, False otherwise.
    """
    global _server_process, _server_port
    _server_port = port

    # 1. Stop the existing server
    await stop_server()
    await asyncio.sleep(1)

    binary = get_opencode_binary()
    cmd = [binary, "serve", "--port", str(port), "--hostname", hostname]

    creationflags = 0
    if platform.system() == "Windows":
        creationflags = 0x00000200  # CREATE_NEW_PROCESS_GROUP

    for run_attempt in range(1, 3):
        logger.info(f"Starting opencode serve (attempt {run_attempt}/2) from {directory}: {' '.join(cmd)}")
        try:
            _server_process = subprocess.Popen(
                cmd,
                cwd=directory,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=(platform.system() != "Windows"),
            )
        except Exception as e:
            logger.error(f"Failed to start opencode serve on attempt {run_attempt}: {e}")
            if run_attempt == 2:
                return False
            await asyncio.sleep(2)
            continue

        # 3. Wait until the server is reachable (max 15 attempts, 1s sleep + 1s timeout)
        import aiohttp
        for attempt in range(15):
            # Check if the process exited prematurely
            poll_code = _server_process.poll()
            if poll_code is not None:
                logger.warning(f"opencode serve process exited prematurely with code {poll_code} on attempt {run_attempt}")
                break

            await asyncio.sleep(1)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"http://{hostname}:{port}/session", timeout=aiohttp.ClientTimeout(total=1)) as resp:
                        if resp.status < 500:
                            logger.info(f"opencode serve is up after {attempt + 1}s (pid={_server_process.pid})")
                            return True
            except Exception:
                pass

        # Cleanup failed process
        if _server_process:
            try:
                _server_process.terminate()
                _server_process.wait(timeout=2)
            except Exception:
                pass
            _server_process = None

        if run_attempt == 1:
            logger.warning("First startup attempt failed or port was busy. Retrying in 2 seconds...")
            await asyncio.sleep(2)

    logger.error("opencode serve did not become reachable after 2 attempts")
    return False


async def stop_server() -> None:
    """Stop the running opencode serve process (if any).

    Kills the entire process tree (parent + child LSP processes) to prevent
    orphan language-server processes from lingering after workspace switches.
    """
    global _server_process

    if _server_process is not None and _server_process.poll() is None:
        pid = _server_process.pid
        logger.info(f"Stopping opencode serve (pid={pid}) and its child processes")
        try:
            _kill_process_tree(pid)
            try:
                _server_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                _server_process.kill()
                _server_process.wait(timeout=3)
        except Exception as e:
            logger.warning(f"Error stopping opencode serve: {e}")
        _server_process = None
