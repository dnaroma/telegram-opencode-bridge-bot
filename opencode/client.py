"""Async HTTP client for the OpenCode serve API.

The OpenCode CLI exposes a REST API via ``opencode serve`` (default
``http://localhost:4096``).  This module wraps every documented endpoint
with an ergonomic, fully-async interface built on :pymod:`aiohttp`.

Endpoints covered
-----------------
* ``GET  /session``                — list all sessions
* ``POST /session``                — create a new session
* ``POST /session/{id}/message``   — send a prompt / message
* ``POST /session/{id}/share``     — share a session (get public URL)

Authentication is HTTP Basic Auth (username + password).

Usage example::

    async with OpenCodeClient(username="u", password="p") as client:
        session = await client.create_session()
        reply = await client.send_message(session["id"], "Hello!")
        print(reply.content)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

import aiohttp

from config import normalize_response_timeout

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class OpenCodeMessage:
    """Structured representation of a response message from OpenCode.

    Attributes:
        role: The role of the message author (e.g. ``"assistant"``).
        content: The textual content of the response.
        session_id: The ID of the session this message belongs to.
        tool_calls: Optional list of tool-call descriptors returned by the
            model.  Each entry is a free-form dict whose shape depends on
            the underlying LLM provider.
    """

    role: str
    content: str
    session_id: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    error_name: str = ""
    error_message: str = ""


SessionStatus = Literal["busy", "retry", "idle"]


@dataclass(frozen=True, slots=True)
class SessionStatusMapResult:
    """Normalized status-map lookup with explicit endpoint availability."""

    available: bool
    statuses: Dict[str, SessionStatus]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OpenCodeError(Exception):
    """Base exception for OpenCode client errors."""


class OpenCodeConnectionError(OpenCodeError):
    """Raised when the server cannot be reached after all retries."""


class OpenCodeAPIError(OpenCodeError):
    """Raised when the API returns an unexpected error status."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenCodeClient:
    """Async HTTP client for the OpenCode serve API.

    Parameters:
        server_url: Base URL of the running ``opencode serve`` instance.
        username: HTTP Basic Auth username (leave empty to skip auth).
        password: HTTP Basic Auth password.
        timeout: Total request timeout in **seconds** (default 300 – five
            minutes, long enough for LLM responses).
        max_retries: Number of attempts for retryable failures (5xx, network
            errors, 429 rate limits).
    """

    def __init__(
        self,
        server_url: str = "http://localhost:4096",
        username: str = "",
        password: str = "",
        timeout: int = 300,
        max_retries: int = 3,
    ) -> None:
        self.server_url: str = server_url.rstrip("/")
        total_timeout = normalize_response_timeout(timeout)
        self.timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(
            total=None if total_timeout == 0 else total_timeout
        )
        self.max_retries: int = max_retries
        self._session: Optional[aiohttp.ClientSession] = None

        # Build Basic Auth header only when credentials are provided.
        self._auth: Optional[aiohttp.BasicAuth] = None
        if username and password:
            self._auth = aiohttp.BasicAuth(username, password)
        self.child_session_listing_available = True

    # -- async context-manager support --------------------------------------

    async def __aenter__(self) -> "OpenCodeClient":
        """Allow ``async with OpenCodeClient(...) as client:`` usage."""
        await self._get_session()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- internal helpers ---------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared :class:`aiohttp.ClientSession`, creating it lazily."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                auth=self._auth,
                read_bufsize=100 * 1024 * 1024,
            )
        return self._session

    async def close(self) -> None:
        """Gracefully close the underlying HTTP session.

        Safe to call multiple times.
        """
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("HTTP session closed.")

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Execute an HTTP request with automatic retries.

        Retries are performed for:
        * HTTP 429 (rate-limited) — honours ``Retry-After`` header.
        * HTTP 5xx (server errors) — exponential back-off.
        * Network-level ``aiohttp.ClientError`` — exponential back-off.

        Client errors (4xx other than 429) are raised immediately.

        Returns:
            Parsed JSON body (``dict`` or ``list``) when the response has a
            JSON content-type, otherwise a ``dict`` with ``content`` (text)
            and ``status`` keys.

        Raises:
            OpenCodeAPIError: For non-retryable HTTP errors.
            OpenCodeConnectionError: After all retries are exhausted.
        """
        url = f"{self.server_url}/{endpoint.lstrip('/')}"
        session = await self._get_session()

        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug("[Attempt %d/%d] %s %s", attempt, self.max_retries, method, url)

                async with session.request(
                    method,
                    url,
                    json=json_data,
                    params=params,
                ) as resp:
                    # --- rate limiting ------------------------------------
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        logger.warning(
                            "Rate-limited (429). Retrying in %ds (attempt %d/%d).",
                            retry_after,
                            attempt,
                            self.max_retries,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    # --- raise on error -----------------------------------
                    if resp.status >= 400:
                        body = await resp.text()
                        if resp.status < 500:
                            # Client error — do NOT retry.
                            raise OpenCodeAPIError(resp.status, body)
                        # Server error — let retry logic handle it.
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=body,
                        )

                    # --- parse success body --------------------------------
                    content_type = resp.content_type or ""
                    if "json" in content_type:
                        return await resp.json()
                    text = await resp.text()
                    return {"content": text, "status": resp.status}

            except OpenCodeAPIError:
                raise  # never retry 4xx

            except aiohttp.ClientResponseError as exc:
                last_error = exc
                logger.error("Server error %d: %s", exc.status, exc.message)

            except aiohttp.ClientError as exc:
                last_error = exc
                logger.error("Connection error: %s", exc)

            # Exponential back-off before next attempt.
            if attempt < self.max_retries:
                wait = min(2 ** attempt, 30)
                logger.info("Retrying in %ds …", wait)
                await asyncio.sleep(wait)

        raise OpenCodeConnectionError(
            f"Failed after {self.max_retries} retries: {last_error}"
        )

    # -- public API ---------------------------------------------------------

    async def is_available(self) -> bool:
        """Check whether the OpenCode server is reachable.

        Returns ``True`` if a request to the sessions endpoint succeeds
        (status < 500), ``False`` otherwise.  Does **not** raise.
        """
        try:
            session = await self._get_session()
            async with session.get(f"{self.server_url}/session") as resp:
                reachable = resp.status < 500
                logger.debug("Server availability check: %s (status %d)", reachable, resp.status)
                return reachable
        except Exception as exc:  # noqa: BLE001
            logger.debug("Server unreachable: %s", exc)
            return False

    async def list_sessions(self) -> List[Dict[str, Any]]:
        """List all existing OpenCode sessions.

        Returns:
            A list of session dicts.  The exact keys depend on the OpenCode
            version, but ``id`` is always present.
        """
        result = await self._request("GET", "/session")
        if isinstance(result, list):
            return result
        # Some API versions wrap the list in an object.
        if isinstance(result, dict):
            return result.get("sessions", result.get("data", []))
        return []

    async def list_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """List all messages in an OpenCode session.

        Parameters:
            session_id: The session identifier.

        Returns:
            A list of message dictionaries.
        """
        result = await self._request("GET", f"/session/{session_id}/message")
        if isinstance(result, list):
            return result
        return []

    async def create_session(self, directory: Optional[str] = None) -> Dict[str, Any]:
        """Create a new OpenCode session.

        Parameters:
            directory: Optional workspace/working directory for the session.

        Returns:
            A dict describing the newly created session.  The ``id`` key
            contains the session identifier needed for subsequent calls.
        """
        params = {}
        if directory:
            params["directory"] = directory

        result = await self._request("POST", "/session", params=params)
        logger.info("Created new session: %s", result.get("id", "<unknown>") if isinstance(result, dict) else result)
        return result

    async def send_message(
        self,
        session_id: str,
        content: str,
        model: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> Optional[OpenCodeMessage]:
        """Send a prompt to an OpenCode session and return the response.

        Parameters:
            session_id: The target session identifier.
            content: The user-facing prompt text.
            model: Optional model identifier (e.g. "provider/model").
            agent: Optional agent identifier (e.g. "build", "plan", "pentester").

        Returns:
            An :class:`OpenCodeMessage` containing the assistant's reply.
        """
        payload = {
            "parts": [
                {
                    "type": "text",
                    "text": content,
                }
            ]
        }

        if agent:
            payload["agent"] = agent.strip()

        if model:
            # Support format: provider/model or provider/model/variant
            parts = model.split("/")
            if len(parts) >= 2:
                provider_id = parts[0]
                model_id = parts[1]
                variant_id = parts[2] if len(parts) >= 3 else None
                
                payload["model"] = {
                    "providerID": provider_id.strip(),
                    "modelID": model_id.strip(),
                }
                if variant_id:
                    payload["model"]["variant"] = variant_id.strip()
            else:
                payload["model"] = {
                    "modelID": model.strip(),
                }

        result = await self._request(
            "POST",
            f"/session/{session_id}/message",
            json_data=payload,
        )

        logger.info("Raw OpenCode API response: %s", result)

        if result is None:
            return None

        if isinstance(result, dict):
            info = result.get("info", {})
            finish_reason = ""
            if isinstance(info, dict):
                finish_reason = info.get("finish", "")
            if not finish_reason:
                finish_reason = result.get("finish", "")

            if str(finish_reason).lower() in ("abort", "aborted", "cancel", "cancelled", "interrupt", "interrupted"):
                return OpenCodeMessage(
                    role="assistant",
                    content="ABORTED",
                    session_id=session_id,
                )

            # Check for error info embedded in the response
            if isinstance(info, dict):
                error_info = info.get("error")
                if isinstance(error_info, dict):
                    error_name = error_info.get("name", "")
                    error_message = error_info.get("message", str(error_info))
                    if error_name == "MessageAbortedError":
                        return OpenCodeMessage(
                            role="assistant",
                            content="ABORTED",
                            session_id=session_id,
                        )
                    # Propagate non-abort errors (rate limit, auth, model errors, etc.)
                    return OpenCodeMessage(
                        role="assistant",
                        content="",
                        session_id=session_id,
                        error_name=error_name,
                        error_message=error_message,
                    )

            # Extract content from the returned parts list if available
            parts = result.get("parts", [])
            content_text = ""
            if isinstance(parts, list):
                text_parts = [
                    p.get("text", "")
                    for p in parts
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                content_text = "".join(text_parts)

            # Fallback to standard response text fields if parts are absent/empty
            if not content_text:
                content_text = result.get(
                    "content",
                    result.get("text", result.get("message", "")),
                )

            return OpenCodeMessage(
                role=result.get("role", "assistant"),
                content=content_text,
                session_id=session_id,
                tool_calls=result.get("tool_calls", result.get("toolCalls", [])),
            )

        # Fallback for unexpected shapes.
        return OpenCodeMessage(
            role="assistant",
            content="",
            session_id=session_id,
        )

    async def share_session(self, session_id: str) -> str:
        """Share a session and retrieve its public URL.

        Parameters:
            session_id: The session to share.

        Returns:
            The publicly accessible URL for the shared session.
        """
        result = await self._request(
            "POST",
            f"/session/{session_id}/share",
        )
        url: str = result.get("url", result.get("share_url", str(result)))
        logger.info("Session %s shared: %s", session_id, url)
        return url

    async def get_available_models(self) -> Dict[str, Any]:
        """Fetch all available models and providers from the server."""
        result = await self._request("GET", "/provider")
        if isinstance(result, dict):
            return result
        return {}

    async def get_available_agents(self) -> List[Dict[str, Any]]:
        """Fetch all available agents from the server."""
        result = await self._request("GET", "/agent")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("agents", result.get("data", []))
        return []

    async def list_session_children(self, session_id: str) -> List[Dict[str, Any]]:
        """List child sessions for a parent OpenCode session when supported."""
        try:
            result = await self._request("GET", f"/session/{session_id}/children")
        except OpenCodeAPIError as exc:
            if exc.status == 404:
                self.child_session_listing_available = False
                logger.warning("OpenCode child-session endpoint is unavailable: %s", exc)
                return []
            raise
        self.child_session_listing_available = True
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("children", "sessions", "data"):
                value = result.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def get_session_status_map(self) -> SessionStatusMapResult:
        """Return normalized session statuses and endpoint availability."""
        try:
            result = await self._request("GET", "/session/status")
        except OpenCodeError as exc:
            logger.warning("OpenCode session-status endpoint is unavailable: %s", exc)
            return SessionStatusMapResult(available=False, statuses={})

        status_map: Dict[str, Any] = {}
        if isinstance(result, dict):
            wrapped = result.get("statuses", result.get("data"))
            status_map = wrapped if isinstance(wrapped, dict) else result

        statuses: Dict[str, SessionStatus] = {}
        for session_id, value in status_map.items():
            status = value.get("type") if isinstance(value, dict) else value
            if status in ("busy", "retry", "idle"):
                statuses[session_id] = status
        return SessionStatusMapResult(available=True, statuses=statuses)

    async def abort_session(self, session_id: str) -> bool:
        """Send an abort signal to stop active model processing in a session."""
        try:
            result = await self._request("POST", f"/session/{session_id}/abort")
            # If the response indicates success, or we get a successful status code, return True
            if isinstance(result, dict):
                return result.get("success", True)
            return True
        except OpenCodeAPIError as e:
            # If we get a 400/404, it might mean there is nothing active to abort or session is not found
            logger.warning(f"Abort request returned an API error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error aborting session {session_id}: {e}")
            raise

    async def respond_to_permission(
        self,
        session_id: str,
        permission_id: str,
        response: str,
        remember: bool = False,
    ) -> bool:
        """Respond to a pending tool/file permission request in a session.

        Parameters:
            session_id: The session identifier.
            permission_id: The permission request identifier.
            response: "once" | "always" | "reject".
            remember: Whether to remember this decision for future operations in this session.

        Returns:
            True if the server successfully recorded the response, False otherwise.
        """
        normalized = response.lower().strip()
        if normalized not in ("once", "always", "reject"):
            raise ValueError(f"Unsupported permission response: {response}")

        payload = {
            "reply": normalized,
        }
        logger.info(
            f"Sending permission response: session={session_id[:8]}... perm={permission_id} action={normalized}"
        )
        try:
            result = await self._request(
                "POST",
                f"/permission/{permission_id}/reply",
                json_data=payload,
            )
            if isinstance(result, dict):
                return result.get("success", True)
            return True
        except Exception as e:
            logger.error(
                f"Failed to respond to permission {permission_id} in session {session_id}: {e}"
            )
            raise

    async def respond_to_question(
        self,
        session_id: str,
        question_id: str,
        answers: list[list[str]],
    ) -> bool:
        """Respond to a question asked by the agent.

        OpenCode API: POST /api/session/{sessionID}/question/{requestID}/reply
        Payload: { answers: [[label1, label2], [label3]] }
          - Each inner list is the selected labels for ONE question in the request.
          - For a single-question request with one selected option: [[selected_label]]

        Parameters:
            session_id: The session identifier.
            question_id: The question request identifier (e.g. que_f06482ccf001...).
            answers: List of answer rows. Each row is a list of selected label strings.

        Returns:
            True if the server successfully recorded the response, False otherwise.
        """
        payload = {
            "answers": answers,
        }
        logger.info(
            f"Sending question response: session={session_id[:8]}... question={question_id} answers={answers}"
        )
        try:
            result = await self._request(
                "POST",
                f"/api/session/{session_id}/question/{question_id}/reply",
                json_data=payload,
            )
            logger.info(
                f"Question response result: question={question_id} result={result}"
            )
            if isinstance(result, dict):
                # API returns boolean true on success
                return bool(result.get("data", result.get("success", True)))
            return True
        except Exception as e:
            logger.error(
                f"Failed to respond to question {question_id}: {e}"
            )
            raise

    async def delete_session(self, session_id: str) -> bool:
        """Permanently delete a session and all its context on the OpenCode server."""
        try:
            result = await self._request("DELETE", f"/session/{session_id}")
            if isinstance(result, dict):
                return result.get("success", True)
            return True
        except Exception as e:
            logger.error(f"Failed to delete session {session_id} on server: {e}")
            raise

    async def get_mcp_status(self) -> Any:
        """Fetch status and connectivity details of registered MCP servers from the server."""
        result = await self._request("GET", "/mcp")
        if isinstance(result, dict):
            return result
        return {}
