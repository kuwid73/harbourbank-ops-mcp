"""What every tool result passes through before it reaches the model: masking, a size cap, an audit record.

Masking is defence in depth. The bank's app already masks personal data in its own logs, but
error messages, other components' logs and GitHub text are outside its control, so the server
masks everything it returns, in one place that no tool can bypass.
"""
import functools
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("harbourbank_ops_mcp")

# Same formats as accounts-api's own log masking, so masked values look alike everywhere
_ACCOUNT_NUMBER = re.compile(r"\bHB\d{6}(\d{4})\b")
_EMAIL = re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# Credentials that could appear in an error message or a log line
_SECRETS = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                       # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),                     # GitHub fine-grained tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWTs (k8s tokens)
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{16,}=*"),
]
_DB_PASSWORD = re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/@\s]+:)[^@\s]+@")

MAX_RESULT_CHARS = 40_000   # about 10k tokens: enough to reason over, small enough to stay focused


def mask_text(text: str) -> str:
    text = _ACCOUNT_NUMBER.sub(lambda m: f"HB******{m.group(1)}", text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    text = _DB_PASSWORD.sub(r"\1[redacted]@", text)
    for pattern in _SECRETS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", text)
    return text


def mask(value: Any) -> Any:
    """Mask every string inside a result, however deeply nested (keys included)."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {mask(k): mask(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask(v) for v in value]
    return value


def bound(result: dict) -> dict:
    """Hard cap on result size. Tools limit themselves first; this catches anything they missed."""
    text = json.dumps(result, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return result
    return {
        "truncated": True,
        "note": f"result was {len(text)} characters, over the {MAX_RESULT_CHARS} limit; narrow the request "
                "(shorter time window, a filter, or fewer lines)",
        "partial_json": text[:MAX_RESULT_CHARS],
    }


class AuditLog:
    """One JSON line per tool call: what the AI looked at, when, and how much it got back."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, entry: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def guarded(audit: AuditLog) -> Callable:
    """Decorator for tools: errors become a readable result, then mask, bound and audit it."""
    def decorate(fn: Callable[..., dict]) -> Callable[..., dict]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> dict:
            started = time.monotonic()
            try:
                result, outcome = fn(*args, **kwargs), "ok"
            except Exception as exc:  # noqa: BLE001 - the model gets the reason, not a dropped connection
                log.exception("tool %s failed", fn.__name__)
                result, outcome = {"error": f"{type(exc).__name__}: {exc}"}, "error"
            result = bound(mask(result))
            audit.write({
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "tool": fn.__name__,
                "arguments": mask(kwargs),
                "outcome": outcome,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "result_chars": len(json.dumps(result, default=str)),
                "truncated": bool(result.get("truncated")),
            })
            return result
        return wrapper
    return decorate


def configure_logging() -> None:
    # stdout carries the MCP protocol; logs must go to stderr, which MCP clients show as server logs
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
