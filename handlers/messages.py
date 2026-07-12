"""
Core message handler — bridges Telegram text messages to OpenCode.

This is the heart of the bot. Every non-command text message is routed
through here to OpenCode's HTTP API (or subprocess fallback).
"""

import logging
import asyncio
import os
import time
import html

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from utils.formatting import format_opencode_response, split_message, format_error, format_tool_output, IMPORTANT_TOOLS
from utils.security import sanitize_input
from config import normalize_response_timeout
from handlers.task_progress import parse_task_progress, upsert_task_progress_card
from opencode.client import OpenCodeAPIError, OpenCodeConnectionError

logger = logging.getLogger(__name__)


_SERVER_CHECK_TTL = 60  # seconds — skip redundant health pings within this window

async def ensure_server_running(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Ensure the OpenCode serve process is running in the correct directory.

    Uses a TTL-cached in-memory flag inside bot_data to avoid redundant local HTTP pings.
    If the server is offline, it dynamically boots it scoped to the correct directory.

    Returns True if the server is running, False otherwise.
    """
    bot_data = context.bot_data
    config = bot_data["config"]
    oc_client = bot_data["opencode_client"]
    session_mgr = bot_data["session_manager"]

    work_dir = await session_mgr.get_user_work_dir(user_id, config.opencode_work_dir)

    from urllib.parse import urlparse
    try:
        url_parsed = urlparse(config.opencode_server_url)
        hostname = url_parsed.hostname or "127.0.0.1"
        port = url_parsed.port or 8080
    except Exception:
        hostname = "127.0.0.1"
        port = 8080

    from opencode.server import ensure_managed_server, is_managed_server_running

    # 1. Check in-memory flag with TTL cache — skip HTTP ping if recently verified
    last_check = bot_data.get("server_last_check", 0.0)
    if (
        bot_data.get("server_started")
        and (time.monotonic() - last_check) < _SERVER_CHECK_TTL
        and is_managed_server_running(work_dir, port=port, hostname=hostname)
    ):
        return True

    # 3. Server is offline - lazy launch it scoped to the user's active folder
    import html
    startup_notice = await update.effective_message.reply_text(
        "⏳ <b>Initializing OpenCode server...</b>\n"
        "This happens once on first startup to physically mount your workspace.",
        parse_mode="HTML"
    )

    async def update_startup_status(text):
        try:
            await startup_notice.edit_text(text, parse_mode="HTML")
        except Exception:
            await update.effective_message.reply_text(text, parse_mode="HTML")

    logger.info(f"Lazy launching OpenCode server inside: {work_dir} on port {port}")

    started = await ensure_managed_server(work_dir, port=port, hostname=hostname)

    if not started:
        await update_startup_status(
            "❌ <b>Failed to start OpenCode server automatically.</b>\n\n"
            "Please make sure <code>opencode</code> is installed on your system or check the bot logs."
        )
        return False

    try:
        await startup_notice.delete()
    except Exception:
        pass

    bot_data["server_started"] = True
    bot_data["server_last_check"] = time.monotonic()
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages by routing them to OpenCode.

    Flow:
        1. Ensure the OpenCode server is running dynamically
        2. Get or create an OpenCode session for this user
        3. Send typing indicator
        4. Forward prompt to OpenCode (HTTP API → subprocess fallback)
        5. Format and split the response
        6. Send back to Telegram
    """
    user = update.effective_user
    user_id = user.id
    message_text = sanitize_input(update.message.text or "")

    # ── Check if user is in the middle of adding an MCP server ───────
    mcp_state = context.user_data.get("mcp_state")
    if mcp_state:
        await handle_mcp_input(update, context, mcp_state)
        return

    # ── Check if user is answering a question ───────
    awaiting_question = context.user_data.get("awaiting_question_answer")
    if awaiting_question:
        short_key = awaiting_question
        pending_questions = context.bot_data.get("pending_questions", {})
        pending = pending_questions.get(short_key)
        
        if pending:
            session_id = pending["session_id"]
            question_id = pending["question_id"]
            oc_client = context.bot_data.get("opencode_client")
            
            try:
                if not oc_client:
                    raise RuntimeError("OpenCode client not available")
                await oc_client.respond_to_question(
                    session_id=session_id,
                    question_id=question_id,
                    answers=[[message_text]]
                )
                
                pending_questions.pop(short_key, None)
                context.user_data.pop("awaiting_question_answer", None)
                
                await update.message.reply_text(
                    f"✅ Answer submitted: <code>{html.escape(message_text)}</code>",
                    parse_mode="HTML"
                )
                return
            except Exception as e:
                logger.error(f"Failed to submit question answer: {e}", exc_info=True)
                await update.message.reply_text(
                    f"⚠️ Failed to submit answer: {e}",
                    parse_mode="HTML"
                )
                return
        else:
            context.user_data.pop("awaiting_question_answer", None)

    # ── Check if user is in the middle of adding an agent skill ──────
    skill_state = context.user_data.get("skill_state")
    if skill_state:
        await handle_skill_input(update, context, skill_state)
        return

    if not message_text or not message_text.strip():
        return

    # ── Prevent concurrent message processing per user ───────
    # If a previous prompt is still being processed, queue the new one
    # instead of running it concurrently (which causes replay bugs).
    bot_data = context.bot_data
    session_mgr = bot_data["session_manager"]

    # Per-user message queue to serialize processing
    user_queue: asyncio.Queue = context.user_data.setdefault("message_queue", asyncio.Queue())
    is_processing = context.user_data.get("message_processing", False)

    if is_processing:
        # Enqueue the message WITH its message_id so replies can quote the original
        await user_queue.put((message_text, update.message.message_id))
        await update.message.reply_text(
            "📥 <i>Message queued — will be processed after the current task completes.</i>",
            parse_mode="HTML"
        )
        return

    status_msg = None
    status_msg_holder = None
    sse_task = None
    typing_task = None

    context.user_data["message_processing"] = True

    config = bot_data["config"]
    oc_client = bot_data["opencode_client"]

    try:
        # Process the current message, then drain the queue
        messages_to_process = [(message_text, update.message.message_id)]
        while messages_to_process:
            current_message, reply_to_msg_id = messages_to_process.pop(0)

    # ── 1. Ensure OpenCode server is running ────────────────
            if not await ensure_server_running(update, context, user_id):
                continue

        # ── 2. Send typing indicator ──────────────────────────
            await update.message.chat.send_action(ChatAction.TYPING)

        # ── 3. Get or create session ──────────────────────────
            session_id = await session_mgr.get_active_session(user_id)

            if not session_id:
                # Create a new OpenCode session
                try:
                    session_id = await _create_session(oc_client, user_id, session_mgr, config)
                except Exception as e:
                    logger.error(f"Failed to create session: {e}", exc_info=True)
                    await update.message.reply_text(
                        format_error(f"Failed to create session: {e}"),
                        parse_mode="HTML",
                        reply_to_message_id=reply_to_msg_id,
                    )
                    continue

        # ── 4. Send prompt to OpenCode ────────────────────────
            # Check if streaming is enabled
            is_streaming = await session_mgr.get_user_streaming(user_id, 0)

            # Send a premium dynamic phase status message to keep user informed in real-time
            status_msg = await update.message.reply_text(
                "🧠 <b>Thinking...</b>\n<i>Analyzing request and preparing a plan...</i>",
                parse_mode="HTML",
                reply_to_message_id=reply_to_msg_id,
            )

            status_msg_holder = [status_msg]

            # Always spawn the SSE event stream listener so we can handle interactive permission prompts
            # (e.g. for sensitive files like .env) even if the user has disabled regular tool-call progress.
            sse_task = asyncio.create_task(
                _listen_and_stream_events(
                    update=update,
                    context=context,
                    session_id=session_id,
                    server_url=config.opencode_server_url,
                    is_streaming=bool(is_streaming == 1),
                    status_msg_holder=status_msg_holder,
                    reply_to_message_id=reply_to_msg_id,
                )
            )

            typing_task = asyncio.create_task(
                _keep_typing(update, config.response_timeout)
            )
            # DON'T clear sent_message_ids — this causes concurrent handlers to re-send old messages.
            # Only ADD to it; stale IDs from previous requests are harmless (they just prevent re-sending).
            sent_message_ids = context.user_data.setdefault("sent_message_ids", set())
            session_mgr.set_session_running(user_id, True)

            # Resolve model and mode: if user hasn't set a model, pass None to let oh-my-openagent plugin decide
            session_model = await session_mgr.get_effective_model(user_id)
            session_mode = await session_mgr.get_user_preferred_mode(user_id, "build")

            # Snapshot existing message IDs before sending — list_messages() returns
            # the full session history, so we must diff to avoid replaying old replies.
            # Include sent_message_ids from previous requests to prevent re-sending.
            before_msg_ids: set = set(sent_message_ids)
            try:
                existing_msgs = await oc_client.list_messages(session_id)
                before_msg_ids.update(
                    m.get("info", {}).get("id")
                    for m in existing_msgs
                    if m.get("info", {}).get("id")
                )
            except Exception as e:
                logger.warning(f"Failed to snapshot pre-prompt message IDs: {e}")

            sent_message_ids.update(before_msg_ids)

            from opencode.session_delivery import register_session_delivery
            chat_id = update.effective_chat.id if update.effective_chat else update.message.chat.id
            register_session_delivery(
                context,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session_id,
                reply_to_message_id=reply_to_msg_id,
                sent_message_ids=sent_message_ids,
            )

            try:
                response_text = await _send_to_opencode(
                    oc_client=oc_client,
                    session_id=session_id,
                    prompt=current_message,
                    model=session_model,
                    agent=session_mode,
                )

                if response_text is None:
                    logger.warning(f"Session {session_id[:8]}... not found on server (returned null). Creating a new session and retrying...")
                    session_id = await _create_session(oc_client, user_id, session_mgr, config)
                    session_model = await session_mgr.get_effective_model(user_id)
                    session_mode = await session_mgr.get_user_preferred_mode(user_id, "build")
                    response_text = await _send_to_opencode(
                        oc_client=oc_client,
                        session_id=session_id,
                        prompt=current_message,
                        model=session_model,
                        agent=session_mode,
                    )
            except OpenCodeConnectionError as conn_err:
                logger.warning(f"Connection lost to OpenCode server: {conn_err}. Attempting to recover...")
                bot_data["server_started"] = False
                
                await update.message.reply_text(
                    "⚠️ <i>Connection to OpenCode server was lost. Attempting to restart server and retry...</i>",
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_msg_id,
                )
                
                if await ensure_server_running(update, context, user_id):
                    session_id = await _create_session(oc_client, user_id, session_mgr, config)
                    session_model = await session_mgr.get_effective_model(user_id)
                    session_mode = await session_mgr.get_user_preferred_mode(user_id, "build")
                    
                    response_text = await _send_to_opencode(
                        oc_client=oc_client,
                        session_id=session_id,
                        prompt=current_message,
                        model=session_model,
                        agent=session_mode,
                    )
                else:
                    raise conn_err
            except OpenCodeAPIError as e:
                if e.status == 404:
                    logger.warning(f"Session {session_id[:8]}... not found on server (HTTP 404). Starting a new one...")
                    try:
                        await session_mgr._db.execute(
                            "DELETE FROM sessions WHERE user_id = ? AND opencode_session_id = ?",
                            (user_id, session_id)
                        )
                        await session_mgr._db.commit()
                    except Exception:
                        pass
                    
                    if user_id in session_mgr._active_sessions:
                        del session_mgr._active_sessions[user_id]
                    
                    session_id = await _create_session(oc_client, user_id, session_mgr, config)
                    session_model = await session_mgr.get_effective_model(user_id)
                    session_mode = await session_mgr.get_user_preferred_mode(user_id, "build")
                    
                    await update.message.reply_text(
                        "⚠️ <i>Active session was deleted or expired on the server. Starting a fresh session...</i>",
                        parse_mode="HTML",
                        reply_to_message_id=reply_to_msg_id,
                    )
                    
                    response_text = await _send_to_opencode(
                        oc_client=oc_client,
                        session_id=session_id,
                        prompt=current_message,
                        model=session_model,
                        agent=session_mode,
                    )
                else:
                    raise

            except asyncio.TimeoutError:
                await update.message.reply_text(
                    "⏰ <b>Request timed out.</b>\n\n"
                    "OpenCode took too long to respond. Try a simpler prompt or check the server.",
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_msg_id,
                )
                continue
            except OpenCodeAPIError as e:
                logger.error(f"OpenCode API error: {e}", exc_info=True)
                await update.message.reply_text(
                    format_error(str(e)),
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_msg_id,
                )
                continue
            except Exception as e:
                logger.error(f"OpenCode error: {e}", exc_info=True)
                await update.message.reply_text(
                    format_error(str(e)),
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_msg_id,
                )
                continue
            finally:
                session_mgr.set_session_running(user_id, False)
                if typing_task:
                    typing_task.cancel()
                if sse_task:
                    sse_task.cancel()
                if status_msg_holder and status_msg_holder[0]:
                    try:
                        await status_msg_holder[0].delete()
                    except Exception:
                        pass

            # Track the message
            await session_mgr.increment_message_count(user_id, prompt=current_message)

            # Fetch messages after prompt completes to get all multi-step assistant messages
            response_texts = []
            try:
                after_messages = await oc_client.list_messages(session_id)
                new_messages = [
                    m for m in after_messages
                    if m.get("info", {}).get("id") not in before_msg_ids
                    and m.get("info", {}).get("id") not in sent_message_ids
                    and m.get("info", {}).get("role") == "assistant"
                ]
                for m in new_messages:
                    msg_id = m.get("info", {}).get("id")
                    if msg_id in sent_message_ids:
                        continue
                    if msg_id:
                        sent_message_ids.add(msg_id)
                    parts = m.get("parts", [])
                    content_text = ""
                    error_msg = _extract_error_from_message(m)
                    if isinstance(parts, list):
                        text_parts = [
                            p.get("text", "")
                            for p in parts
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        content_text = "".join(text_parts)
                    if error_msg:
                        response_texts.append(f"❌ <b>Error:</b> {html.escape(error_msg)}")
                    elif content_text.strip():
                        response_texts.append(content_text)
            except Exception as e:
                logger.warning(f"Failed to fetch messages after prompt: {e}")

            all_responses = response_texts if response_texts else ([response_text] if response_text else [])

            if not all_responses:
                if response_text and response_text.startswith("__ERROR__"):
                    _, _, err_detail = response_text.partition("__ERROR__")
                    err_parts = err_detail.split("__", 1)
                    err_name = err_parts[0] if len(err_parts) > 0 else "Unknown"
                    err_msg = err_parts[1] if len(err_parts) > 1 else err_detail
                    await update.message.reply_text(
                        format_error(f"{err_name}: {err_msg}"),
                        parse_mode="HTML",
                        reply_to_message_id=reply_to_msg_id,
                    )
                elif response_text == "ABORTED":
                    pass
                else:
                    await update.message.reply_text(
                        "ℹ️ <b>OpenCode finished execution.</b>\n<i>(No conversational text response was returned)</i>",
                        parse_mode="HTML",
                        reply_to_message_id=reply_to_msg_id,
                    )
                while not user_queue.empty():
                    try:
                        messages_to_process.append(user_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                continue

            for resp in all_responses:
                if not resp or resp == "ABORTED":
                    continue
                if resp.startswith("__ERROR__"):
                    _, _, err_detail = resp.partition("__ERROR__")
                    err_parts = err_detail.split("__", 1)
                    err_name = err_parts[0] if len(err_parts) > 0 else "Unknown"
                    err_msg = err_parts[1] if len(err_parts) > 1 else err_detail
                    await update.message.reply_text(
                        format_error(f"{err_name}: {err_msg}"),
                        parse_mode="HTML",
                        reply_to_message_id=reply_to_msg_id,
                    )
                    continue

                formatted = format_opencode_response(resp)
                chunks = split_message(formatted, config.max_message_length)

                for i, chunk in enumerate(chunks):
                    try:
                        await update.message.reply_text(
                            chunk,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_to_message_id=reply_to_msg_id,
                        )
                    except Exception as e:
                        logger.warning(f"HTML parse failed for chunk {i+1}, falling back to plain text: {e}")
                        try:
                            import re
                            plain = re.sub(r'<[^>]+>', '', chunk)
                            await update.message.reply_text(
                                plain,
                                disable_web_page_preview=True,
                                reply_to_message_id=reply_to_msg_id,
                            )
                        except Exception as e2:
                            logger.error(f"Failed to send chunk {i+1} even as plain text: {e2}")

                    if i < len(chunks) - 1:
                        await asyncio.sleep(0.5)

            # Drain the queue for the next iteration
            while not user_queue.empty():
                try:
                    messages_to_process.append(user_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

    except Exception as outer_e:
        logger.error(f"Unexpected error in message processing loop: {outer_e}", exc_info=True)
        try:
            await update.message.reply_text(
                format_error(str(outer_e)),
                parse_mode="HTML",
                reply_to_message_id=reply_to_msg_id,
            )
        except Exception:
            pass
    finally:
        context.user_data["message_processing"] = False


async def _create_session(oc_client, user_id, session_mgr, config):
    """Create a new OpenCode session and register it."""
    # Fetch preferred user workspace directory, falling back to base configuration path
    work_dir = await session_mgr.get_user_work_dir(user_id, config.opencode_work_dir)
    
    result = await oc_client.create_session(directory=work_dir)
    if not isinstance(result, dict):
        raise ValueError(f"Invalid session response from OpenCode server: {result}")
    
    session_id = (
        result.get("id")
        or result.get("session_id")
        or result.get("sessionId")
    )
    if not session_id:
        raise ValueError(f"OpenCode server response did not contain a session ID: {result}")

    # Use user's preferred model if set; don't store the server-returned model
    # because that's the server's default (e.g. OPENCODE_MODEL env var),
    # not the user's choice. Storing it would cause get_effective_model() to
    # return a non-None value, sending model param and overriding plugin defaults.
    user_model = await session_mgr.get_effective_model(user_id) or ""
    preferred_mode = await session_mgr.get_user_preferred_mode(user_id, "build")

    await session_mgr.set_active_session(
        user_id, session_id, user_model, work_dir=work_dir, mode=preferred_mode
    )
    return session_id


async def _send_to_opencode(oc_client, session_id, prompt, model, agent):
    """Send a prompt to OpenCode HTTP API.

    Returns:
        The response text from OpenCode, or None if the session does not exist.
    """
    logger.info(f"Sending to OpenCode API: session={session_id[:8]}... model={model} agent={agent}")
    response = await oc_client.send_message(session_id, prompt, model=model, agent=agent)
    if response is None:
        return None
    if response.error_message:
        logger.warning(f"OpenCode returned error in response: {response.error_name}: {response.error_message}")
        return f"__ERROR__{response.error_name}__{response.error_message}"
    return response.content


def _extract_error_from_message(msg_data: dict) -> str | None:
    """Extract error info from a message dict returned by list_messages, if present."""
    info = msg_data.get("info", {})
    if not isinstance(info, dict):
        return None
    error_info = info.get("error")
    if not isinstance(error_info, dict):
        return None
    name = error_info.get("name", "")
    message = error_info.get("message", "")
    if name == "MessageAbortedError":
        return None
    if name or message:
        return f"{name}: {message}" if name else message
    return None


async def _keep_typing(update: Update, max_seconds: int = 3600) -> None:
    """Keep sending typing indicators while we wait for OpenCode.

    Telegram typing indicator expires after ~5 seconds, so we
    refresh it every 4 seconds.
    """
    limit = normalize_response_timeout(max_seconds)
    try:
        elapsed = 0
        while limit == 0 or elapsed < limit:
            await update.message.chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(4)
            elapsed += 4
    except asyncio.CancelledError:
        pass  # Expected when response arrives
    except Exception:
        pass  # Don't crash on typing indicator failures


async def _listen_and_stream_events(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: str,
    server_url: str,
    is_streaming: bool,
    status_msg_holder = None,
    reply_to_message_id: int | None = None,
):
    """Listens to global OpenCode events via SSE and handles tool progress/permission requests.
    Includes an automatic reconnect loop with exponential back-off to prevent getting stuck.
    """
    import aiohttp
    import json
    import html
    import uuid
    import os

    url = f"{server_url.rstrip('/')}/global/event"
    notified_calls = set()
    completed_calls = set()
    notified_permissions = set()
    watched_session_ids = {session_id}
    # OpenCode can emit an initial pending tool part before the part contains
    # its `tool` field. Keep the name from the newer tool lifecycle events so
    # that early progress updates do not regress to a user-visible "unknown".
    tool_names_by_call_id = {}
    last_update_time = [0.0]
    last_status_text = ["🧠 <b>Thinking...</b>\n<i>Analyzing request and preparing a plan...</i>"]

    def truncate(text, max_len=500):
        if not text:
            return ""
        text = str(text)
        if len(text) > max_len:
            return text[:max_len] + "\n... (truncated)"
        return text

    async def update_status(text: str):
        now = time.time()
        last_status_text[0] = text
        # Throttling to respect Telegram API rate limits (minimum 1.5 seconds between message edits)
        if status_msg_holder and status_msg_holder[0] and (now - last_update_time[0] >= 1.5):
            try:
                await status_msg_holder[0].edit_text(text, parse_mode="HTML")
                last_update_time[0] = now
            except Exception as e:
                logger.debug(f"Failed to update status message: {e}")

    async def refresh_watched_child_sessions():
        """Discover child sessions before their first permission event arrives."""
        try:
            oc_client = context.bot_data.get("opencode_client")
            list_children = getattr(oc_client, "list_session_children", None)
            if not callable(list_children):
                return
            children = await list_children(session_id)
            if not isinstance(children, list):
                return
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_id = (
                    child.get("id")
                    or child.get("sessionID")
                    or child.get("sessionId")
                )
                if isinstance(child_id, str) and child_id:
                    watched_session_ids.add(child_id)
        except Exception as exc:
            logger.debug("Unable to discover child sessions for permission routing: %s", exc)

    retry_delay = 1.0
    while True:
        try:
            async with aiohttp.ClientSession(
                read_bufsize=100 * 1024 * 1024,
                timeout=aiohttp.ClientTimeout(total=None),
            ) as sse_session:
                async with sse_session.get(url, headers={"Accept": "text/event-stream"}) as resp:
                    # Connection successful, reset retry delay
                    retry_delay = 1.0
                    await refresh_watched_child_sessions()
                    
                    async for line in resp.content:
                        line_str = line.decode('utf-8').strip()
                        if not line_str or not line_str.startswith("data:"):
                            continue
                        
                        data_content = line_str[5:].strip()
                        try:
                            event_obj = json.loads(data_content)
                            payload = event_obj.get("payload", {})
                            if not isinstance(payload, dict):
                                continue
                            
                            properties = payload.get("properties", {})
                            if not isinstance(properties, dict):
                                continue
                            
                            event_type = payload.get("type", "")
                            tool_properties = properties.get("tool", {})
                            tool_session_id = ""
                            if isinstance(tool_properties, dict):
                                tool_session_id = (
                                    tool_properties.get("sessionID")
                                    or tool_properties.get("sessionId")
                                    or tool_properties.get("session_id")
                                    or ""
                                )
                            
                            event_session_id = (
                                properties.get("sessionID")
                                or properties.get("sessionId")
                                or properties.get("session_id")
                                or tool_session_id
                                or payload.get("sessionID")
                                or payload.get("sessionId")
                                or payload.get("session_id")
                                or ""
                            )

                            if event_type in ("question.asked", "permission.asked", "permission.updated", "message.part.updated", "message.updated"):
                                logger.info(f"SSE event: type={event_type} session={event_session_id[:12] if event_session_id else 'NONE'} expected={session_id[:12]} props_keys={list(properties.keys())[:8]}")

                            if event_session_id and event_session_id not in watched_session_ids:
                                continue

                            event_type = payload.get("type", "")

                            # The session.next tool events carry the name before
                            # the corresponding message part is fully populated.
                            if event_type == "session.next.tool.input.started":
                                call_id = properties.get("callID", "")
                                tool_name = properties.get("name", "")
                                if call_id and tool_name:
                                    tool_names_by_call_id[call_id] = str(tool_name)
                                continue

                            if event_type == "session.next.tool.called":
                                call_id = properties.get("callID", "")
                                tool_name = properties.get("tool", "")
                                if call_id and tool_name:
                                    tool_names_by_call_id[call_id] = str(tool_name)
                                continue

                            # A. Handle Intermediate Assistant Message Completion (Real-time Streaming)
                            if event_type == "message.updated":
                                info = properties.get("info", {})
                                msg_id = info.get("id")
                                role = info.get("role")
                                completed = info.get("time", {}).get("completed")
                                error_info = info.get("error") if isinstance(info.get("error"), dict) else None

                                # Message aborted — expire any pending questions from this message
                                if error_info and error_info.get("name") == "MessageAbortedError":
                                    pending_questions = context.bot_data.get("pending_questions", {})
                                    chat_id = update.effective_chat.id if update.effective_chat else None
                                    expired_keys = [k for k, v in pending_questions.items() if v.get("session_id") == session_id]
                                    for ek in expired_keys:
                                        tmsg_id = pending_questions[ek].get("telegram_msg_id")
                                        if tmsg_id and chat_id:
                                            try:
                                                await context.bot.edit_message_text(
                                                    chat_id=chat_id,
                                                    message_id=tmsg_id,
                                                    text="❓ <i>Question expired</i> ⏱️ — the agent timed out waiting for your answer.",
                                                    parse_mode="HTML"
                                                )
                                            except Exception:
                                                pass
                                        del pending_questions[ek]
                                    if expired_keys:
                                        logger.info(f"Expired {len(expired_keys)} pending questions for aborted message {msg_id}")

                                elif error_info and role == "assistant" and completed:
                                    error_name = error_info.get("name", "Error")
                                    error_message = error_info.get("message", str(error_info))
                                    sent_message_ids = context.user_data.setdefault("sent_message_ids", set())
                                    if msg_id not in sent_message_ids:
                                        sent_message_ids.add(msg_id)
                                        if status_msg_holder and status_msg_holder[0]:
                                            try:
                                                await status_msg_holder[0].delete()
                                            except Exception:
                                                pass
                                            status_msg_holder[0] = None
                                        await update.message.reply_text(
                                            format_error(f"{error_name}: {error_message}"),
                                            parse_mode="HTML",
                                            reply_to_message_id=reply_to_message_id,
                                        )

                                if role == "assistant" and completed:
                                    sent_message_ids = context.user_data.setdefault("sent_message_ids", set())
                                    if msg_id not in sent_message_ids:
                                        sent_message_ids.add(msg_id)
                                        try:
                                            oc_client = context.bot_data["opencode_client"]
                                            messages = await oc_client.list_messages(session_id)
                                            target_msg = next((m for m in messages if m.get("info", {}).get("id") == msg_id), None)
                                            if target_msg:
                                                parts = target_msg.get("parts", [])
                                                content_text = ""
                                                if isinstance(parts, list):
                                                    text_parts = [
                                                        p.get("text", "")
                                                        for p in parts
                                                        if isinstance(p, dict) and p.get("type") == "text"
                                                    ]
                                                    content_text = "".join(text_parts)
                                                
                                                if content_text.strip():
                                                    # Delete the old status message at the top
                                                    if status_msg_holder and status_msg_holder[0]:
                                                        try:
                                                            await status_msg_holder[0].delete()
                                                        except Exception:
                                                            pass
                                                        status_msg_holder[0] = None

                                                    formatted = format_opencode_response(content_text)
                                                    chunks = split_message(formatted, context.bot_data["config"].max_message_length)
                                                    for i, chunk in enumerate(chunks):
                                                        await update.message.reply_text(
                                                            chunk,
                                                            parse_mode="HTML",
                                                            disable_web_page_preview=True,
                                                            reply_to_message_id=reply_to_message_id,
                                                        )
                                                        if i < len(chunks) - 1:
                                                            await asyncio.sleep(0.5)

                                                    # Recreate the status indicator at the very bottom
                                                    if status_msg_holder:
                                                        try:
                                                            status_msg_holder[0] = await update.message.reply_text(
                                                                last_status_text[0],
                                                                parse_mode="HTML"
                                                            )
                                                            last_update_time[0] = time.time()
                                                        except Exception as e:
                                                            logger.warning(f"Failed to recreate status message at bottom: {e}")
                                        except Exception as e:
                                            logger.warning(f"Failed to stream intermediate message {msg_id}: {e}")

                            # B. Handle Permission Requested Popup (Always Enabled)
                            elif event_type in ("permission.asked", "permission.updated"):
                                perm_id = properties.get("id") or properties.get("permissionID") or payload.get("id")
                                perm_type = properties.get("permission") or properties.get("type") or "execute"
                                patterns = properties.get("patterns", [])
                                if not patterns:
                                    pattern = properties.get("pattern")
                                    if isinstance(pattern, list):
                                        patterns = pattern
                                    elif pattern:
                                        patterns = [pattern]

                                if not perm_id:
                                    logger.warning("Received permission.asked event but no permission ID was found.")
                                    continue

                                # OpenCode may emit both permission.asked and
                                # permission.updated for one request. Only send
                                # one Telegram prompt for a permission ID.
                                if perm_id in notified_permissions:
                                    continue
                                notified_permissions.add(perm_id)

                                # Register pending permission in-memory lookup to avoid Telegram 64-char callback limit
                                if "pending_permissions" not in context.bot_data:
                                    context.bot_data["pending_permissions"] = {}

                                short_key = uuid.uuid4().hex[:8]
                                permission_session_id = event_session_id or session_id
                                context.bot_data["pending_permissions"][short_key] = {
                                    "session_id": permission_session_id,
                                    "permission_id": perm_id
                                }

                                patterns_text = ""
                                if patterns:
                                    pat_list = "\n".join([f"• <code>{html.escape(str(p))}</code>" for p in patterns])
                                    patterns_text = f"\n<b>Target Resource(s):</b>\n{pat_list}"

                                tool_name = ""
                                tool_info = properties.get("tool", {})
                                if isinstance(tool_info, dict):
                                    tool_name = tool_info.get("name", "")
                                if not tool_name:
                                    tool_name = properties.get("title", "")
                                if not tool_name:
                                    tool_name = perm_type

                                msg = (
                                    f"🛡️ <b>OpenCode Permission Requested</b>\n\n"
                                    f"The agent is asking for confirmation to use the tool <code>{html.escape(tool_name)}</code>.\n"
                                    f"{patterns_text}\n\n"
                                    f"Do you want to allow this operation?"
                                )
                                context.bot_data["pending_permissions"][short_key]["prompt_text"] = msg

                                keyboard = [
                                    [
                                        InlineKeyboardButton("✅ Allow once", callback_data=f"perm:once:{short_key}"),
                                        InlineKeyboardButton("♾️ Allow always", callback_data=f"perm:always:{short_key}"),
                                        InlineKeyboardButton("❌ Reject", callback_data=f"perm:reject:{short_key}")
                                    ]
                                ]

                                await update.message.reply_text(
                                    msg,
                                    parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(keyboard)
                                )

                            elif event_type == "question.asked":
                                question_id = properties.get("id") or payload.get("id")
                                event_session_id = properties.get("sessionID") or properties.get("sessionId") or ""
                                questions_list = properties.get("questions", [])
                                tool_info = properties.get("tool", {}) if isinstance(properties.get("tool"), dict) else {}

                                if not question_id:
                                    logger.warning("Received question.asked event but no question ID was found.")
                                    continue

                                if not isinstance(questions_list, list) or not questions_list:
                                    logger.warning("Received question.asked event but no questions array found in properties.")
                                    continue

                                if "pending_questions" not in context.bot_data:
                                    context.bot_data["pending_questions"] = {}

                                for q_idx, q_item in enumerate(questions_list):
                                    q_header = q_item.get("header", "")
                                    q_text = q_item.get("question", "")
                                    q_options = q_item.get("options", [])
                                    q_multiple = q_item.get("multiple", False)
                                    q_custom = q_item.get("custom", True)

                                    if not q_text:
                                        continue

                                    short_key = uuid.uuid4().hex[:8]
                                    context.bot_data["pending_questions"][short_key] = {
                                        "session_id": session_id,
                                        "question_id": question_id,
                                        "chat_id": update.effective_chat.id if update.effective_chat else None,
                                    }

                                    header_prefix = f"<b>{html.escape(q_header)}</b>\n\n" if q_header else ""
                                    msg = f"❓ {header_prefix}{html.escape(q_text)}"
                                    if q_multiple:
                                        msg += "\n\n<i>You may select multiple options.</i>"
                                    msg += "\n\n⚠️ <i>Please answer quickly — the question times out after ~30 seconds.</i>"

                                    keyboard = []
                                    MAX_Q_OPTIONS = 10
                                    display_options = q_options[:MAX_Q_OPTIONS] if isinstance(q_options, list) else []

                                    for idx, opt in enumerate(display_options):
                                        if isinstance(opt, dict):
                                            label = opt.get("label", f"Option {idx+1}")
                                            desc = opt.get("description", "")
                                            display_label = label[:50]
                                            if desc and len(label) + len(desc) < 55:
                                                display_label = f"{label[:30]} — {desc[:20]}"
                                            cb_value = label[:40]
                                        else:
                                            display_label = str(opt)[:50]
                                            cb_value = display_label

                                        keyboard.append([InlineKeyboardButton(
                                            display_label,
                                            callback_data=f"question:{short_key}:{cb_value}"
                                        )])

                                    if isinstance(q_options, list) and len(q_options) > MAX_Q_OPTIONS:
                                        msg += f"\n\n<i>(Showing {MAX_Q_OPTIONS} of {len(q_options)} options)</i>"

                                    if q_custom:
                                        keyboard.append([InlineKeyboardButton(
                                            "✏️ Type custom answer",
                                            callback_data=f"question:{short_key}:__custom__"
                                        )])

                                    try:
                                        chunks = split_message(msg, context.bot_data["config"].max_message_length)
                                        if keyboard:
                                            for chunk in chunks[:-1]:
                                                await update.message.reply_text(chunk, parse_mode="HTML")
                                            sent_msg = await update.message.reply_text(
                                                chunks[-1],
                                                parse_mode="HTML",
                                                reply_markup=InlineKeyboardMarkup(keyboard)
                                            )
                                        else:
                                            for chunk in chunks:
                                                await update.message.reply_text(chunk, parse_mode="HTML")
                                            context.user_data["awaiting_question_answer"] = short_key
                                            sent_msg = None

                                        if sent_msg:
                                            context.bot_data["pending_questions"][short_key]["telegram_msg_id"] = sent_msg.message_id
                                    except Exception as e:
                                        logger.error(f"Failed to send question prompt: {e}")
                                        fallback_msg = f"❓ Question:\n\n{html.escape(q_text)}\n\n<i>Please reply with your answer.</i>"
                                        for chunk in split_message(fallback_msg, context.bot_data["config"].max_message_length):
                                            await update.message.reply_text(chunk, parse_mode="HTML")
                                        context.user_data["awaiting_question_answer"] = short_key

                            # B. Handle Tool Execution Progress
                            elif event_type == "message.part.updated":
                                part = properties.get("part", {})
                                if not isinstance(part, dict):
                                    continue
                                
                                part_type = part.get("type", "")
                                if part_type == "tool":
                                    call_id = part.get("callID", "")
                                    part_tool_name = part.get("tool", "")
                                    if call_id and part_tool_name:
                                        tool_names_by_call_id[call_id] = str(part_tool_name)
                                    tool_name = str(
                                        part_tool_name
                                        or tool_names_by_call_id.get(call_id, "")
                                    )
                                    state = part.get("state", {})
                                    if not isinstance(state, dict):
                                        continue
                                    
                                    status = state.get("status", "")
                                    input_data = state.get("input", {})
                                    output_data = state.get("output", "")
                                    metadata = state.get("metadata", {})
                                    if not isinstance(metadata, dict):
                                        metadata = {}

                                    # A task tool creates a child session. Add it
                                    # immediately so its permission events are
                                    # accepted even before /children is refreshed.
                                    if tool_name == "task":
                                        child_session_id = (
                                            metadata.get("sessionId")
                                            or metadata.get("sessionID")
                                        )
                                        if isinstance(child_session_id, str) and child_session_id:
                                            watched_session_ids.add(child_session_id)

                                    if tool_name == "task" and not is_streaming:
                                        task_event = parse_task_progress(part, session_id)
                                        if task_event is not None:
                                            await upsert_task_progress_card(
                                                update.message,
                                                context,
                                                task_event,
                                                reply_to_message_id,
                                            )

                                    # ── 1. Update In-Place Status Message (Always Active) ──
                                    if status in ("pending", "running") and status_msg_holder and status_msg_holder[0]:
                                        if status == "pending" and not (isinstance(input_data, dict) and input_data):
                                            if tool_name:
                                                status_text = f"⚙️ <b>Preparing tool <code>{html.escape(tool_name)}</code>...</b>"
                                            else:
                                                status_text = "⚙️ <b>Preparing tool...</b>"
                                        else:
                                            status_text = ""
                                            if tool_name == "bash":
                                                cmd = input_data.get("command") or input_data.get("content") or ""
                                                cmd_truncated = truncate(cmd, 60)
                                                status_text = f"💻 <b>Running shell command...</b>\n<code>{html.escape(cmd_truncated)}</code>"
                                            elif tool_name in ("edit", "write", "save"):
                                                path = input_data.get("path") or input_data.get("target") or input_data.get("filePath") or input_data.get("filepath") or ""
                                                path_truncated = truncate(os.path.basename(path) if path else "", 60)
                                                status_text = f"📝 <b>Modifying file...</b>\n<code>{html.escape(path_truncated)}</code>"
                                            elif tool_name in ("read", "view", "show"):
                                                path = input_data.get("filePath") or input_data.get("path") or input_data.get("target") or input_data.get("filepath") or ""
                                                path_truncated = truncate(os.path.basename(path) if path else "", 60)
                                                status_text = f"🔍 <b>Reading file...</b>\n<code>{html.escape(path_truncated)}</code>"
                                            elif tool_name in ("webfetch", "websearch", "search"):
                                                query = input_data.get("query") or input_data.get("url") or ""
                                                query_truncated = truncate(query, 60)
                                                status_text = f"🌐 <b>Searching web...</b>\n<code>{html.escape(query_truncated)}</code>"
                                            elif tool_name == "question":
                                                status_text = "❓ <b>Waiting for your answer...</b>"
                                            else:
                                                if isinstance(input_data, dict) and input_data:
                                                    param_parts = []
                                                    for k, v in input_data.items():
                                                        v_str = str(v)
                                                        if len(v_str) > 80:
                                                            v_str = v_str[:77] + "..."
                                                        param_parts.append(f"<code>{html.escape(k)}</code>: {html.escape(v_str)}")
                                                        if len(param_parts) >= 3:
                                                            break
                                                    params_summary = " | ".join(param_parts)
                                                    status_text = f"⚙️ <b>Executing tool <code>{html.escape(tool_name)}</code>...</b>\n{params_summary}"
                                                else:
                                                    status_text = f"⚙️ <b>Executing tool <code>{html.escape(tool_name)}</code>...</b>"
                                        
                                        await update_status(status_text)

                                    # ── 1b. Handle Question Tool (Always Active) ──
                                    if tool_name == "question":
                                        # Question expired / aborted — update Telegram message
                                        if status == "error" and call_id in completed_calls:
                                            pending_questions = context.bot_data.get("pending_questions", {})
                                            expired_keys = [
                                                k for k, v in pending_questions.items()
                                                if v.get("call_id") == call_id or v.get("question_id") == call_id
                                            ]
                                            chat_id = update.effective_chat.id if update.effective_chat else None
                                            for ek in expired_keys:
                                                if ek in pending_questions:
                                                    tmsg_id = pending_questions[ek].get("telegram_msg_id")
                                                    if tmsg_id and chat_id:
                                                        try:
                                                            await context.bot.edit_message_text(
                                                                chat_id=chat_id,
                                                                message_id=tmsg_id,
                                                                text="❓ <i>Question expired</i> ⏱️ — the agent timed out waiting for your answer.",
                                                                parse_mode="HTML"
                                                            )
                                                        except Exception:
                                                            pass
                                                    del pending_questions[ek]

                                        # New question — render with buttons
                                        if status in ("pending", "running") and call_id not in notified_calls:
                                            notified_calls.add(call_id)
                                            completed_calls.add(call_id)

                                            if not isinstance(input_data, dict):
                                                continue

                                            questions_list = input_data.get("questions", [])
                                            if not isinstance(questions_list, list) or not questions_list:
                                                continue

                                            for q_item in questions_list:
                                                q_header = q_item.get("header", "")
                                                q_text = q_item.get("question", "")
                                                q_options = q_item.get("options", [])
                                                q_multiple = q_item.get("multiple", False)

                                                if not q_text:
                                                    continue

                                                if "pending_questions" not in context.bot_data:
                                                    context.bot_data["pending_questions"] = {}

                                                # Try to find existing question_id from a prior question.asked event
                                                existing_qid = None
                                                for v in context.bot_data["pending_questions"].values():
                                                    if v.get("call_id") == call_id and v.get("question_id"):
                                                        existing_qid = v["question_id"]
                                                        break

                                                question_key = uuid.uuid4().hex[:8]
                                                context.bot_data["pending_questions"][question_key] = {
                                                    "session_id": session_id,
                                                    "call_id": call_id,
                                                    "question_id": existing_qid or call_id,
                                                    "chat_id": update.effective_chat.id if update.effective_chat else None,
                                                }

                                                header_prefix = f"<b>{html.escape(q_header)}</b>\n\n" if q_header else ""
                                                msg = f"❓ {header_prefix}{html.escape(q_text)}"
                                                if q_multiple:
                                                    msg += "\n\n<i>You may select multiple options.</i>"
                                                msg += "\n\n⚠️ <i>Please answer quickly — the question times out after ~30 seconds.</i>"

                                                keyboard = []
                                                MAX_Q_OPTIONS = 10
                                                display_options = q_options[:MAX_Q_OPTIONS] if isinstance(q_options, list) else []

                                                for idx, opt in enumerate(display_options):
                                                    if isinstance(opt, dict):
                                                        label = opt.get("label", f"Option {idx+1}")
                                                        value = opt.get("value", label)
                                                        desc = opt.get("description", "")
                                                        display_label = f"{label[:50]}"
                                                        if desc and len(label) + len(desc) < 55:
                                                            display_label = f"{label[:30]} — {desc[:20]}"
                                                        cb_value = value[:40]
                                                    else:
                                                        display_label = str(opt)[:50]
                                                        cb_value = display_label

                                                    keyboard.append([InlineKeyboardButton(
                                                        display_label,
                                                        callback_data=f"question:{question_key}:{cb_value}"
                                                    )])

                                                if isinstance(q_options, list) and len(q_options) > MAX_Q_OPTIONS:
                                                    msg += f"\n\n<i>(Showing {MAX_Q_OPTIONS} of {len(q_options)} options)</i>"

                                                keyboard.append([InlineKeyboardButton(
                                                    "✏️ Type custom answer",
                                                    callback_data=f"question:{question_key}:__custom__"
                                                )])

                                                try:
                                                    chunks = split_message(msg, context.bot_data["config"].max_message_length)
                                                    for chunk in chunks[:-1]:
                                                        await update.message.reply_text(chunk, parse_mode="HTML")
                                                    sent_msg = await update.message.reply_text(
                                                        chunks[-1],
                                                        parse_mode="HTML",
                                                        reply_markup=InlineKeyboardMarkup(keyboard)
                                                    )
                                                    context.bot_data["pending_questions"][question_key]["telegram_msg_id"] = sent_msg.message_id
                                                except Exception as e:
                                                    logger.error(f"Failed to send question prompt: {e}")
                                                    try:
                                                        fallback = f"❓ {html.escape(q_text)}\n\n<i>Please reply with your answer.</i>"
                                                        await update.message.reply_text(fallback, parse_mode="HTML")
                                                        context.user_data["awaiting_question_answer"] = question_key
                                                    except Exception:
                                                        pass

                                    # ── 2. Tool Output Handling ──

                                    # 2a. Always show custom-formatted output for important tools
                                    if status == "completed" and call_id not in completed_calls:
                                        if tool_name in IMPORTANT_TOOLS:
                                            formatted = format_tool_output(tool_name, input_data, output_data)
                                            if formatted:
                                                completed_calls.add(call_id)
                                                await update.message.reply_text(formatted, parse_mode="HTML", reply_to_message_id=reply_to_message_id)

                                    # 2b. Stream Full Tool Logs (Only if is_streaming is True)
                                    if is_streaming:
                                        # 1. Tool Call Started / Running
                                        if status in ("pending", "running") and call_id not in notified_calls:
                                            if status == "pending" and not (isinstance(input_data, dict) and input_data):
                                                pass
                                            else:
                                                notified_calls.add(call_id)
                                                
                                                if tool_name == "bash":
                                                    cmd = input_data.get("command") or input_data.get("content") or ""
                                                    msg = f"💻 <b>Running shell command...</b>\n<code>{html.escape(truncate(cmd, 60))}</code>"
                                                elif tool_name in ("edit", "write", "save"):
                                                    path = input_data.get("path") or input_data.get("target") or input_data.get("filePath") or input_data.get("filepath") or ""
                                                    msg = f"📝 <b>Modifying file...</b> <code>{html.escape(truncate(os.path.basename(path) if path else '', 60))}</code>"
                                                elif tool_name in ("read", "view", "show"):
                                                    path = input_data.get("filePath") or input_data.get("path") or input_data.get("target") or input_data.get("filepath") or ""
                                                    msg = f"🔍 <b>Reading file...</b> <code>{html.escape(truncate(os.path.basename(path) if path else '', 60))}</code>"
                                                elif tool_name in ("webfetch", "websearch", "search"):
                                                    query = input_data.get("query") or input_data.get("url") or ""
                                                    msg = f"🌐 <b>Searching web...</b> <code>{html.escape(truncate(query, 60))}</code>"
                                                elif tool_name == "question":
                                                    msg = "❓ <b>Asking a question...</b>"
                                                else:
                                                    msg = f"🛠️ <b>Calling Tool <code>{html.escape(tool_name)}</code></b>"
                                                
                                                if isinstance(input_data, dict):
                                                    params = {k: v for k, v in input_data.items() if k != "description"}
                                                    
                                                    if params:
                                                        msg += "\n\n<b>Parameters:</b>\n"
                                                        for k, v in params.items():
                                                            v_str = str(v)
                                                            if len(v_str) > 100:
                                                                v_display = f"{v_str[:100]}... ({len(v_str)} chars)"
                                                            else:
                                                                v_display = v_str
                                                            msg += f"  • <code>{html.escape(k)}</code>: {html.escape(v_display)}\n"
                                                
                                                await update.message.reply_text(msg, parse_mode="HTML", reply_to_message_id=reply_to_message_id)

                                        # 2. Tool Completed
                                        elif status == "completed" and call_id not in completed_calls:
                                            completed_calls.add(call_id)

                                            formatted = format_tool_output(tool_name, input_data, output_data)
                                            if formatted:
                                                await update.message.reply_text(formatted, parse_mode="HTML", reply_to_message_id=reply_to_message_id)
                                            else:
                                                exit_code = metadata.get("exit", 0)
                                                output_cleaned = truncate(str(output_data))

                                                msg = (
                                                    f"✅ <b>Tool <code>{html.escape(tool_name)}</code> Completed</b> (Exit <code>{exit_code}</code>)\n"
                                                )
                                                if output_cleaned.strip():
                                                    msg += f"<pre>{html.escape(output_cleaned)}</pre>"
                                                else:
                                                    msg += f"<i>(No output returned)</i>"
                                                    
                                                await update.message.reply_text(msg, parse_mode="HTML", reply_to_message_id=reply_to_message_id)

                                        # 3. Tool Failed
                                        elif status in ("failed", "error") and call_id not in completed_calls:
                                            completed_calls.add(call_id)
                                            
                                            output_cleaned = truncate(str(output_data))
                                            
                                            msg = (
                                                f"❌ <b>Tool <code>{html.escape(tool_name)}</code> Failed</b>\n"
                                            )
                                            if output_cleaned.strip():
                                                msg += f"<pre>{html.escape(output_cleaned)}</pre>"
                                            else:
                                                msg += f"<i>(No error description returned)</i>"
                                                
                                            await update.message.reply_text(msg, parse_mode="HTML", reply_to_message_id=reply_to_message_id)

                        except Exception as e:
                            logger.debug(f"Error parsing SSE event in listener: {e}")

        except asyncio.CancelledError:
            logger.debug("SSE streaming task listener cancelled by parent task.")
            break
        except Exception as e:
            err_name = str(e) or type(e).__name__
            logger.warning(f"Error in SSE streaming task listener: {err_name}. Reconnecting in {retry_delay}s...")

            # Poll for missed question/abort events while SSE was down
            try:
                oc_client = context.bot_data["opencode_client"]
                messages = await oc_client.list_messages(session_id)
                for msg_data in messages:
                    info = msg_data.get("info", {}) if isinstance(msg_data, dict) else {}
                    if not isinstance(info, dict):
                        continue
                    msg_error = info.get("error")
                    if isinstance(msg_error, dict) and msg_error.get("name") == "MessageAbortedError":
                        # This message was aborted — expire any related pending questions
                        pending_questions = context.bot_data.get("pending_questions", {})
                        chat_id = update.effective_chat.id if update.effective_chat else None
                        expired_keys = [k for k, v in pending_questions.items() if v.get("session_id") == session_id]
                        for ek in expired_keys:
                            tmsg_id = pending_questions[ek].get("telegram_msg_id")
                            if tmsg_id and chat_id:
                                try:
                                    await context.bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=tmsg_id,
                                        text="❓ <i>Question expired</i> ⏱️ — the agent timed out while SSE was disconnected.",
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
                            del pending_questions[ek]

                    # Check for still-pending question tools that SSE might have missed
                    parts = msg_data.get("parts", []) if isinstance(msg_data, dict) else []
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "tool" and part.get("tool") == "question":
                            state = part.get("state", {})
                            if not isinstance(state, dict):
                                continue
                            q_call_id = part.get("callID", "")
                            q_status = state.get("status", "")
                            if q_status in ("pending", "running") and q_call_id not in notified_calls:
                                # Found a pending question that SSE missed — render it
                                input_data = state.get("input", {})
                                if isinstance(input_data, dict):
                                    questions_list = input_data.get("questions", [])
                                    if isinstance(questions_list, list):
                                        for q_item in questions_list:
                                            q_header = q_item.get("header", "")
                                            q_text = q_item.get("question", "")
                                            q_options = q_item.get("options", [])
                                            q_multiple = q_item.get("multiple", False)
                                            if q_text:
                                                notified_calls.add(q_call_id)
                                                completed_calls.add(q_call_id)
                                                if "pending_questions" not in context.bot_data:
                                                    context.bot_data["pending_questions"] = {}
                                                qkey = uuid.uuid4().hex[:8]
                                                context.bot_data["pending_questions"][qkey] = {
                                                    "session_id": session_id,
                                                    "call_id": q_call_id,
                                                    "question_id": q_call_id,
                                                    "chat_id": update.effective_chat.id if update.effective_chat else None,
                                                }
                                                header_prefix = f"<b>{html.escape(q_header)}</b>\n\n" if q_header else ""
                                                qmsg = f"❓ {header_prefix}{html.escape(q_text)}"
                                                if q_multiple:
                                                    qmsg += "\n\n<i>You may select multiple options.</i>"
                                                qmsg += "\n\n⚠️ <i>Please answer quickly — the question times out after ~30 seconds.</i>"
                                                kb = []
                                                for idx, opt in enumerate(q_options[:10]):
                                                    if isinstance(opt, dict):
                                                        lbl = opt.get("label", f"Option {idx+1}")
                                                        val = opt.get("value", lbl)
                                                        desc = opt.get("description", "")
                                                        dl = lbl[:50]
                                                        if desc and len(lbl) + len(desc) < 55:
                                                            dl = f"{lbl[:30]} — {desc[:20]}"
                                                        cbv = val[:40]
                                                    else:
                                                        dl = str(opt)[:50]
                                                        cbv = dl
                                                    kb.append([InlineKeyboardButton(dl, callback_data=f"question:{qkey}:{cbv}")])
                                                kb.append([InlineKeyboardButton("✏️ Type custom answer", callback_data=f"question:{qkey}:__custom__")])
                                                try:
                                                    chunks = split_message(qmsg, context.bot_data["config"].max_message_length)
                                                    for chunk in chunks[:-1]:
                                                        await update.message.reply_text(chunk, parse_mode="HTML")
                                                    sent_msg = await update.message.reply_text(
                                                        chunks[-1], parse_mode="HTML",
                                                        reply_markup=InlineKeyboardMarkup(kb)
                                                    )
                                                    context.bot_data["pending_questions"][qkey]["telegram_msg_id"] = sent_msg.message_id
                                                except Exception as ex:
                                                    logger.error(f"Failed to render missed question from poll: {ex}")
            except Exception as poll_err:
                logger.warning(f"Failed to poll for missed events during SSE reconnect: {poll_err}")

            try:
                await asyncio.sleep(retry_delay)
            except asyncio.CancelledError:
                break
            retry_delay = min(retry_delay * 2, 10.0)


async def handle_skill_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save the uploaded file as SKILL.md in the skill directory, parsing the name dynamically from YAML frontmatter."""
    import os
    import html
    import re
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    config = context.bot_data["config"]
    
    document = update.message.document
    if not document:
        await update.message.reply_text("⚠️ Please upload a valid document file (markdown or text).")
        return
        
    file_id = document.file_id
    new_file = await context.bot.get_file(file_id)
    
    import tempfile
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = os.path.join(temp_dir, "temp_skill.md")
        await new_file.download_to_drive(temp_path)
        
        with open(temp_path, "r", encoding="utf-8") as f:
            content = f.read()
            
    scope = context.user_data["skill_temp"]["scope"]
    
    # Parse the frontmatter to extract the name
    from utils.skill_manager import parse_frontmatter, get_skills, get_skills_dir
    meta = parse_frontmatter(content)
    name = meta.get("name")
    
    keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="skill_cancel")]]
    
    if not name:
        await update.message.reply_text(
            "⚠️ <b>Missing name header:</b> The uploaded file must contain a <code>name:</code> header in its frontmatter headers block.\n\n"
            "Example at the top of your file:\n"
            "<pre>---\n"
            "name: my-cool-skill\n"
            "description: A short description\n"
            "---\n\n"
            "Please fix the headers in your file and upload it again:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return
        
    # Validate name format
    name = name.strip()
    if not re.match(r"^[a-zA-Z0-9\-_]+$", name):
        await update.message.reply_text(
            f"⚠️ <b>Invalid skill name in header:</b> <code>{html.escape(name)}</code>.\n"
            "Name must contain only alphanumeric characters, hyphens, or underscores.\n\n"
            "Please correct the <code>name:</code> header in your file and upload it again:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return
        
    base_dir = os.path.abspath(config.opencode_work_dir)
    current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
    current_dir = os.path.abspath(current_dir)
    
    # Check for duplicate names in scope
    existing_skills = get_skills(current_dir)
    is_duplicate = False
    for s in existing_skills:
        if s["name"].lower() == name.lower() and s["scope"] == scope:
            is_duplicate = True
            break
            
    if is_duplicate:
        await update.message.reply_text(
            f"⚠️ <b>Duplicate name:</b> A skill named <code>{html.escape(name)}</code> already exists in the <b>{scope}</b> scope.\n\n"
            "Please edit the <code>name:</code> header in your file and upload it again:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return
        
    s_dir = get_skills_dir(current_dir, scope)
    skill_folder = os.path.join(s_dir, name)
    os.makedirs(skill_folder, exist_ok=True)
    skill_path = os.path.join(skill_folder, "SKILL.md")
    
    # Write the uploaded content directly as SKILL.md
    try:
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ <b>Failed to save skill file:</b> {html.escape(str(e))}\n\nPlease try again:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return
        
    # Clean up state
    context.user_data.pop("skill_state", None)
    context.user_data.pop("skill_temp", None)
    
    status_msg = await update.message.reply_text(
        f"🚀 <b>Skill <code>{html.escape(name)}</code> uploaded and configured successfully!</b>\n"
        f"Restarting OpenCode serve to reload configuration...",
        parse_mode="HTML"
    )
    
    from handlers.commands import restart_opencode_serve, render_skill_detail
    await restart_opencode_serve(update, context, user_id, current_dir)
    
    try:
        await status_msg.delete()
    except Exception:
        pass
        
    await render_skill_detail(update, context, user_id, current_dir, scope, name)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming file uploads (documents and gallery photos) from Telegram,
    download them to the active workspace, and trigger the OpenCode agent for analysis.
    """
    user = update.effective_user
    user_id = user.id
    
    # Intercept file uploads if we are waiting for a skill file upload
    state = context.user_data.get("skill_state")
    if state == "waiting_for_skill_file":
        await handle_skill_file_upload(update, context)
        return
        
    document = update.message.document
    photo_list = update.message.photo

    if not document and not photo_list:
        return

    bot_data = context.bot_data
    session_mgr = bot_data["session_manager"]
    oc_client = bot_data["opencode_client"]
    config = bot_data["config"]

    import uuid

    # 1. Extract file metadata
    if document:
        filename = document.file_name or "uploaded_file"
        file_id = document.file_id
        file_size = document.file_size
    else:
        # It's a photo from gallery
        photo = photo_list[-1]  # Get largest resolution photo
        filename = f"photo_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"
        file_id = photo.file_id
        file_size = photo.file_size

    # 2. Path Traversal & Security Sanitization
    sanitized_filename = os.path.basename(filename)

    # 3. Get active workspace directory
    work_dir = await session_mgr.get_user_work_dir(user_id, config.opencode_work_dir)
    os.makedirs(work_dir, exist_ok=True)
    destination_path = os.path.join(work_dir, sanitized_filename)

    # 4. Check 20MB Telegram Bot API download limit
    max_size = 20 * 1024 * 1024  # 20MB in bytes
    if file_size > max_size:
        await update.message.reply_text(
            f"⚠️ <b>File Too Large!</b>\n\n"
            f"Telegram standard bots are restricted to downloads under 20MB. "
            f"Your file size is <code>{file_size / (1024*1024):.2f}MB</code>.\n\n"
            f"Please copy the file manually to your local project folder:\n"
            f"<code>{html.escape(work_dir)}</code>",
            parse_mode="HTML"
        )
        return

    # 5. Overwrite Protection (Backup existing files)
    if os.path.exists(destination_path):
        backup_path = destination_path + ".bak"
        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)  # Delete old backup if present
            os.rename(destination_path, backup_path)
            logger.info(f"Backed up existing file {sanitized_filename} to {sanitized_filename}.bak")
        except Exception as e:
            logger.warning(f"Failed to backup existing file {sanitized_filename}: {e}")

    import html

    # 6. Send progress / typing indicator
    status_notice = await update.message.reply_text(
        f"📥 <b>Downloading file...</b>\n"
        f"Saving <code>{html.escape(sanitized_filename)}</code> directly to your local project workspace.",
        parse_mode="HTML"
    )

    try:
        # Download file via Telegram API
        file_obj = await context.bot.get_file(file_id)
        await file_obj.download_to_drive(custom_path=destination_path)
        
        await status_notice.delete()
    except Exception as e:
        logger.error(f"Failed to download file from Telegram: {e}", exc_info=True)
        try:
            await status_notice.edit_text(
                f"❌ <b>Download Failed</b>\n\n"
                f"Failed to fetch file from Telegram: <code>{html.escape(str(e))}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # 7. Retrieve Caption / Prompt
    caption = sanitize_input(update.message.caption or "")

    if caption and caption.strip():
        # User uploaded a file AND wrote a caption/instruction (e.g. "Explain this code")
        # Format a unified prompt for the OpenCode agent
        prompt_text = (
            f"[System Notification: The user uploaded the file '{sanitized_filename}' "
            f"successfully into the active workspace directory. Please analyze it based on their prompt below.]\n\n"
            f"{caption}"
        )
        
        # Route to standard message pipeline!
        # First, ensure OpenCode server is running
        if not await ensure_server_running(update, context, user_id):
            return

        await update.message.chat.send_action(ChatAction.TYPING)

        # Get or create active session
        session_id = await session_mgr.get_active_session(user_id)
        if not session_id:
            try:
                session_id = await _create_session(oc_client, user_id, session_mgr, config)
            except Exception as se:
                logger.error(f"Failed to create session during document upload: {se}", exc_info=True)
                await update.message.reply_text(
                    format_error(f"Failed to create session: {se}"),
                    parse_mode="HTML"
                )
                return

        # Start dynamic phase status indicator and streaming
        is_streaming = await session_mgr.get_user_streaming(user_id, 0)
        status_msg = await update.message.reply_text(
            "🧠 <b>Thinking...</b>\n<i>Analyzing request and preparing a plan...</i>",
            parse_mode="HTML"
        )
        
        status_msg_holder = [status_msg]

        sse_task = asyncio.create_task(
            _listen_and_stream_events(
                update=update,
                context=context,
                session_id=session_id,
                server_url=config.opencode_server_url,
                is_streaming=bool(is_streaming == 1),
                status_msg_holder=status_msg_holder,
                reply_to_message_id=update.message.message_id,
            )
        )

        typing_task = asyncio.create_task(
            _keep_typing(update, config.response_timeout)
        )

        before_ids = set()
        sent_message_ids = context.user_data.setdefault("sent_message_ids", set())
        session_mgr.set_session_running(user_id, True)
        try:
            # Pass None if user hasn't set a model, letting oh-my-openagent plugin decide
            session_model = await session_mgr.get_effective_model(user_id)
            session_mode = await session_mgr.get_user_preferred_mode(user_id, "build")

            # Fetch message IDs before sending the prompt
            try:
                before_messages = await oc_client.list_messages(session_id)
                before_ids = {m.get("info", {}).get("id") for m in before_messages if m.get("info", {}).get("id")}
            except Exception as e:
                logger.warning(f"Failed to fetch messages before document prompt: {e}")

            sent_message_ids.update(before_ids)

            from opencode.session_delivery import register_session_delivery
            chat_id = update.effective_chat.id if update.effective_chat else update.message.chat.id
            register_session_delivery(
                context,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session_id,
                reply_to_message_id=update.message.message_id,
                sent_message_ids=sent_message_ids,
            )

            response_text = await _send_to_opencode(
                oc_client=oc_client,
                session_id=session_id,
                prompt=prompt_text,
                model=session_model,
                agent=session_mode,
            )

            # Increment count
            await session_mgr.increment_message_count(user_id, prompt=caption)

            # Send response back to user
            # Fetch messages after prompt completes to get all multi-step assistant messages
            response_texts = []
            try:
                after_messages = await oc_client.list_messages(session_id)
                new_messages = [
                    m for m in after_messages
                    if m.get("info", {}).get("id") not in before_ids 
                    and m.get("info", {}).get("id") not in sent_message_ids
                    and m.get("info", {}).get("role") == "assistant"
                ]
                for m in new_messages:
                    msg_id = m.get("info", {}).get("id")
                    if msg_id in sent_message_ids:
                        continue
                    if msg_id:
                        sent_message_ids.add(msg_id)
                    parts = m.get("parts", [])
                    content_text = ""
                    error_msg = _extract_error_from_message(m)
                    if isinstance(parts, list):
                        text_parts = [
                            p.get("text", "")
                            for p in parts
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        content_text = "".join(text_parts)
                    if error_msg:
                        response_texts.append(f"❌ <b>Error:</b> {html.escape(error_msg)}")
                    elif content_text.strip():
                        response_texts.append(content_text)
            except Exception as e:
                logger.warning(f"Failed to fetch messages after document prompt: {e}")

            # Fallback to standard response if no intermediate texts were retrieved
            all_responses = response_texts if response_texts else ([response_text] if response_text else [])

            if not all_responses:
                if response_text and response_text.startswith("__ERROR__"):
                    _, _, err_detail = response_text.partition("__ERROR__")
                    err_parts = err_detail.split("__", 1)
                    err_name = err_parts[0] if len(err_parts) > 0 else "Unknown"
                    err_msg = err_parts[1] if len(err_parts) > 1 else err_detail
                    await update.message.reply_text(
                        format_error(f"{err_name}: {err_msg}"),
                        parse_mode="HTML",
                        reply_to_message_id=update.message.message_id,
                    )
                elif response_text == "ABORTED":
                    pass
                else:
                    await update.message.reply_text(
                        "ℹ️ <b>OpenCode finished execution.</b>\n<i>(No conversational text response was returned)</i>",
                        parse_mode="HTML",
                        reply_to_message_id=update.message.message_id,
                    )
                return

            for resp in all_responses:
                if not resp or resp == "ABORTED":
                    continue
                if resp.startswith("__ERROR__"):
                    _, _, err_detail = resp.partition("__ERROR__")
                    err_parts = err_detail.split("__", 1)
                    err_name = err_parts[0] if len(err_parts) > 0 else "Unknown"
                    err_msg = err_parts[1] if len(err_parts) > 1 else err_detail
                    await update.message.reply_text(
                        format_error(f"{err_name}: {err_msg}"),
                        parse_mode="HTML",
                        reply_to_message_id=update.message.message_id,
                    )
                    continue

                formatted = format_opencode_response(resp)
                chunks = split_message(formatted, config.max_message_length)

                for i, chunk in enumerate(chunks):
                    try:
                        await update.message.reply_text(
                            chunk,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as he:
                        import re
                        plain = re.sub(r'<[^>]+>', '', chunk)
                        await update.message.reply_text(
                            plain,
                            disable_web_page_preview=True,
                            reply_to_message_id=update.message.message_id,
                        )
                    if i < len(chunks) - 1:
                        await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"Error analyzing uploaded file: {e}", exc_info=True)
            await update.message.reply_text(
                format_error(str(e)),
                parse_mode="HTML",
                reply_to_message_id=update.message.message_id,
            )
        finally:
            session_mgr.set_session_running(user_id, False)
            if typing_task:
                typing_task.cancel()
            if sse_task:
                sse_task.cancel()
            if status_msg_holder and status_msg_holder[0]:
                try:
                    await status_msg_holder[0].delete()
                except Exception:
                    pass

    else:
        # File uploaded with NO caption/prompt.
        # Just send a high-fidelity confirmation card!
        size_display = f"{file_size / 1024:.2f} KB" if file_size < 1024*1024 else f"{file_size / (1024*1024):.2f} MB"
        
        confirmation = (
            f"📥 <b>File Saved Successfully!</b>\n\n"
            f"• <b>Filename:</b> <code>{html.escape(sanitized_filename)}</code>\n"
            f"• <b>Size:</b> <code>{size_display}</code>\n"
            f"• <b>Destination:</b> <code>{html.escape(work_dir)}</code>\n\n"
            f"<i>OpenCode can now read and access this file locally. Ask me anything about it!</i>"
        )
        
        await update.message.reply_text(confirmation, parse_mode="HTML")


async def handle_mcp_input(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str) -> None:
    """Processes step-by-step text messages for adding an MCP server."""
    import re
    import html
    import shlex
    
    user_id = update.effective_user.id
    bot_data = context.bot_data
    session_mgr = bot_data["session_manager"]
    config = bot_data["config"]
    
    # Ensure mcp_temp dictionary exists
    context.user_data.setdefault("mcp_temp", {})
    
    if state == "waiting_for_name":
        name = (update.message.text or "").strip()
        if not re.match(r'^[a-zA-Z0-9_-]+$', name):
            keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="mcp_cancel")]]
            await update.message.reply_text(
                "⚠️ <b>Invalid name format.</b> Please use only letters, numbers, hyphens, and underscores (no spaces).\n\n"
                "Please enter the MCP server name again:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return
            
        # Check if already exists in configurations
        base_dir = os.path.abspath(config.opencode_work_dir)
        current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
        current_dir = os.path.abspath(current_dir)
        
        from utils.config_parser import get_mcp_servers
        existing_mcps = get_mcp_servers(current_dir)
        if name in existing_mcps:
            keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="mcp_cancel")]]
            await update.message.reply_text(
                f"⚠️ An MCP server named <code>{html.escape(name)}</code> already exists in this workspace config.\n\n"
                f"Please enter a different name:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return
            
        context.user_data["mcp_temp"]["name"] = name
        context.user_data["mcp_state"] = "waiting_for_type"
        
        keyboard = [
            [
                InlineKeyboardButton("💻 Stdio (Local Command)", callback_data="mcp_type:local"),
                InlineKeyboardButton("🌐 Remote (SSE Web URL)", callback_data="mcp_type:remote")
            ],
            [InlineKeyboardButton("↩️ Cancel", callback_data="mcp_cancel")]
        ]
        await update.message.reply_text(
            f"📝 <b>Add MCP Server (Step 2)</b>\n\n"
            f"• Name: <code>{html.escape(name)}</code>\n\n"
            f"Select the connection type for this MCP server:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        
    elif state == "waiting_for_type":
        # User is expected to tap an inline keyboard button. If they enter text, prompt them.
        keyboard = [
            [
                InlineKeyboardButton("💻 Stdio (Local Command)", callback_data="mcp_type:local"),
                InlineKeyboardButton("🌐 Remote (SSE Web URL)", callback_data="mcp_type:remote")
            ],
            [InlineKeyboardButton("↩️ Cancel", callback_data="mcp_cancel")]
        ]
        await update.message.reply_text(
            "⚠️ Please select the connection type using the buttons below, or tap Cancel:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        
    elif state == "waiting_for_command":
        cmd_str = (update.message.text or "").strip()
        try:
            cmd_args = shlex.split(cmd_str)
        except Exception as e:
            keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="mcp_cancel")]]
            await update.message.reply_text(
                f"⚠️ <b>Invalid command syntax:</b> {html.escape(str(e))}\n\n"
                f"Please enter the command again:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return
            
        if not cmd_args:
            keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="mcp_cancel")]]
            await update.message.reply_text(
                "⚠️ Command cannot be empty. Please enter a valid execution command:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return
            
        # Save command in memory & proceed to optional environment variables step
        context.user_data["mcp_temp"]["command"] = cmd_args
        context.user_data["mcp_state"] = "waiting_for_env"
        
        keyboard = [
            [InlineKeyboardButton("⏭️ Skip & Save", callback_data="mcp_skip_env")],
            [InlineKeyboardButton("↩️ Cancel", callback_data="mcp_cancel")]
        ]
        await update.message.reply_text(
            "🔑 <b>Optional: Environment Variables (Step 4/4)</b>\n\n"
            "If this MCP server requires API keys or custom credentials, please enter them in <code>KEY=VALUE</code> format.\n\n"
            "📋 <b>Tap to copy example</b>:\n"
            "<code>GITHUB_PERSONAL_ACCESS_TOKEN=ghp_abc123</code>\n\n"
            "<i>If entering multiple variables, separate them with spaces or newlines, e.g.</i>:\n"
            "<code>KEY1=VAL1 KEY2=VAL2</code>\n\n"
            "Tap <b>Skip & Save</b> below if no environment variables are needed.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        
    elif state == "waiting_for_env":
        env_str = (update.message.text or "").strip()
        
        # Parse environment variables securely
        env_dict = {}
        if env_str:
            try:
                tokens = shlex.split(env_str)
            except Exception:
                tokens = env_str.replace('\n', ' ').split()
                
            for token in tokens:
                if '=' in token:
                    k, v = token.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"\'')
                    if k:
                        env_dict[k] = v
                        
        name = context.user_data["mcp_temp"]["name"]
        cmd_args = context.user_data["mcp_temp"]["command"]
        
        mcp_config = {
            "type": "local",
            "command": cmd_args,
            "enabled": True
        }
        if env_dict:
            mcp_config["env"] = env_dict
            
        base_dir = os.path.abspath(config.opencode_work_dir)
        current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
        current_dir = os.path.abspath(current_dir)
        
        from utils.config_parser import add_mcp_server
        add_mcp_server(current_dir, name, mcp_config)
        
        # Reset state immediately to avoid double-processing
        context.user_data.pop("mcp_state", None)
        context.user_data.pop("mcp_temp", None)
        
        status_msg = await update.message.reply_text(
            f"🚀 <b>Adding MCP server <code>{html.escape(name)}</code>...</b>\n"
            f"Restarting OpenCode serve to reload configuration...",
            parse_mode="HTML"
        )
        
        from handlers.commands import restart_opencode_serve, render_mcps_list
        await restart_opencode_serve(update, context, user_id, current_dir)
        
        try:
            await status_msg.delete()
        except Exception:
            pass
            
        await render_mcps_list(update, context, user_id, current_dir)
        
    elif state == "waiting_for_url":
        url = (update.message.text or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="mcp_cancel")]]
            await update.message.reply_text(
                "⚠️ <b>Invalid URL format.</b> The remote SSE URL must start with <code>http://</code> or <code>https://</code>\n\n"
                "Please enter the URL again:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return
            
        name = context.user_data["mcp_temp"]["name"]
        
        mcp_config = {
            "type": "remote",
            "url": url,
            "enabled": True
        }
        
        base_dir = os.path.abspath(config.opencode_work_dir)
        current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
        current_dir = os.path.abspath(current_dir)
        
        from utils.config_parser import add_mcp_server
        add_mcp_server(current_dir, name, mcp_config)
        
        # Reset state immediately
        context.user_data.pop("mcp_state", None)
        context.user_data.pop("mcp_temp", None)
        
        status_msg = await update.message.reply_text(
            f"🚀 <b>Adding MCP server <code>{html.escape(name)}</code>...</b>\n"
            f"Restarting OpenCode serve to reload configuration...",
            parse_mode="HTML"
        )
        
        from handlers.commands import restart_opencode_serve, render_mcps_list
        await restart_opencode_serve(update, context, user_id, current_dir)
        
        try:
            await status_msg.delete()
        except Exception:
            pass
            
        await render_mcps_list(update, context, user_id, current_dir)


async def handle_skill_input(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str) -> None:
    """Handle message input for the new agent skill registration wizard."""
    import os
    import html
    import re
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from handlers.commands import render_skills_list, restart_opencode_serve
    
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    config = context.bot_data["config"]
    
    if state == "waiting_for_import_url":
        url = (update.message.text or "").strip()
        if not url:
            keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="skill_cancel")]]
            await update.message.reply_text(
                "⚠️ URL cannot be empty. Please enter a valid URL:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return
            
        scope = context.user_data["skill_temp"]["scope"]
        
        status_msg = await update.message.reply_text(
            f"📥 <b>Importing skill from URL...</b>\n"
            f"Please wait while we clone/fetch the repository files.",
            parse_mode="HTML"
        )
        
        base_dir = os.path.abspath(config.opencode_work_dir)
        current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
        current_dir = os.path.abspath(current_dir)
        
        from utils.skill_manager import import_skill_from_url
        try:
            imported = import_skill_from_url(url, scope, current_dir)
        except Exception as e:
            try:
                await status_msg.delete()
            except Exception:
                pass
            keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="skill_cancel")]]
            await update.message.reply_text(
                f"❌ <b>Import Failed:</b> {html.escape(str(e))}\n\nPlease enter the URL again:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return
            
        try:
            await status_msg.delete()
        except Exception:
            pass
            
        if not imported:
            keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="skill_cancel")]]
            await update.message.reply_text(
                "⚠️ No <code>SKILL.md</code> files were found in the provided repository.\n"
                "Please enter a different URL:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return
            
        # Reset state
        context.user_data.pop("skill_state", None)
        context.user_data.pop("skill_temp", None)
        
        # Trigger OpenCode reload
        reload_msg = await update.message.reply_text(
            f"🚀 <b>Imported skill(s):</b> {', '.join(imported)}\n"
            f"Restarting OpenCode serve to reload configuration...",
            parse_mode="HTML"
        )
        
        await restart_opencode_serve(update, context, user_id, current_dir)
        
        try:
            await reload_msg.delete()
        except Exception:
            pass
            
        # Send final success and render the skills list
        await update.message.reply_text(
            f"✅ <b>Successfully imported {len(imported)} skill(s)!</b>\n"
            f"• Scope: <code>{scope.capitalize()}</code>\n"
            f"• Skills: <code>{', '.join(imported)}</code>",
            parse_mode="HTML"
        )
        
        await render_skills_list(update, context, user_id, current_dir)
