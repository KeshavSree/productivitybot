"""Private, text-only phone capture endpoint; Siri/Shortcuts handles speech."""

import hmac
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass

from aiohttp import web

from .core import CaptureConflict, TaskService
from .parser import split_tasks

log = logging.getLogger(__name__)
MAX_TEXT = 4000
MAX_TASKS = 50
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True)
class CaptureConfig:
    token: str
    user_id: int

    def __post_init__(self):
        if len(self.token) < 32 or not self.token.isascii() or any(c.isspace() for c in self.token):
            raise ValueError("CAPTURE_TOKEN must contain at least 32 ASCII characters without whitespace")
        if self.user_id <= 0:
            raise ValueError("CAPTURE_USER_ID must be a positive Discord user ID")

    @classmethod
    def from_env(cls):
        token = os.getenv("CAPTURE_TOKEN", "").strip()
        user_id = os.getenv("CAPTURE_USER_ID", "").strip()
        if not token and not user_id:
            return None
        if not token or not user_id:
            raise ValueError("Set both CAPTURE_TOKEN and CAPTURE_USER_ID to enable phone capture")
        config = cls(token, int(user_id))
        allowed = {int(v) for v in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if v}
        if allowed and config.user_id not in allowed:
            raise ValueError("CAPTURE_USER_ID must be included in ALLOWED_USER_IDS")
        return config


def create_app(service: TaskService, config: CaptureConfig | None = None) -> web.Application:
    # Requests commit locally and return promptly; the runtime owns remote sync.
    app = web.Application(client_max_size=16 * 1024)
    recent_requests: deque[float] = deque()

    def error(message: str, status: int):
        return web.json_response({"error": message}, status=status, headers={"Cache-Control": "no-store"})

    async def health(request):
        return web.json_response({"status": "ok", "capture_enabled": config is not None,
                                  "notion_configured": service.notion is not None})

    async def capture(request):
        if config is None:
            return error("Phone capture is not configured", 503)
        actual = request.headers.get("Authorization", "").encode()
        expected = f"Bearer {config.token}".encode()
        if not hmac.compare_digest(actual, expected):
            return error("Unauthorized", 401)
        now = time.monotonic()
        while recent_requests and recent_requests[0] <= now - 60:
            recent_requests.popleft()
        if len(recent_requests) >= 30:
            response = error("Too many captures; try again in a minute", 429)
            response.headers["Retry-After"] = "60"
            return response
        recent_requests.append(now)
        if request.content_type != "application/json":
            return error("Use Content-Type: application/json", 415)
        try:
            body = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return error("Request body is too large", 413)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return error("Invalid JSON", 400)
        if not isinstance(body, dict) or set(body) - {"text", "request_id"}:
            return error("Send an object containing text and request_id only", 400)
        text, request_id = body.get("text"), body.get("request_id")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return error(f"text must contain 1–{MAX_TEXT} characters", 400)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            return error("request_id must contain 1–128 letters, digits, underscores or hyphens", 400)
        if len(split_tasks(text)) > MAX_TASKS:
            return error(f"Submit at most {MAX_TASKS} tasks per capture", 400)
        try:
            result = await service.handle_text(config.user_id, text, request_id, sync=False)
        except CaptureConflict as exc:
            return error(str(exc), 409)
        except Exception:
            log.exception("Capture failed")
            return error("Capture failed; retry using the same request_id", 500)
        status = service.sync_status(result.tasks)
        message = result.message
        if status == "pending":
            message += " Saved; waiting for Notion sync."
        elif status == "local_only":
            message += " Saved locally; Notion is not configured."
        return web.json_response({
            "request_id": request_id,
            "action": result.action,
            "message": message,
            "sync_status": status,
            "replayed": result.replayed,
            "tasks": [{"id": t.id, "content": t.content, "category": t.category, "done": t.done}
                      for t in result.tasks],
        }, headers={"Cache-Control": "no-store"})

    app.router.add_get("/health", health)
    app.router.add_post("/capture", capture)
    return app
