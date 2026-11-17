"""Build a short readable preview from a Gmail message payload.

The ``label`` command shows a sender's name, address, and subjects. When that
isn't enough to recognise who is mailing you, ``e`` (expand) fetches the
message body and prints the opening lines.

This module is deliberately pure (no API calls) so the parsing can be tested
without a Gmail client: :func:`message_preview` takes a message ``payload``
exactly as the Gmail API returns it.
"""

from __future__ import annotations

import base64
import html
import re
from typing import Any

#: How many body lines to show by default.
DEFAULT_LINES = 4

#: Total character budget across the whole preview.
DEFAULT_CHARS = 400

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_SCRIPT_RE = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_BLOCK_RE = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h[1-6])\s*/?\s*>", re.I)
_QUOTED_RE = re.compile(r"^\s*>")

#: Boilerplate that never helps identify a sender.
_NOISE_RE = re.compile(
    r"^(view (this|it) (email|message|in (your )?browser)|having trouble viewing|"
    r"this email was sent to|manage (your )?(preferences|subscription))",
    re.I,
)


def _decode(data: str) -> str:
    """Decode a base64url body chunk from the Gmail API."""
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):  # pragma: no cover - malformed payload
        return ""
    return raw.decode("utf-8", errors="replace")


def collect_text(payload: dict[str, Any] | None, mime_type: str) -> str:
    """Return the concatenated bodies of every part with ``mime_type``."""
    if not payload:
        return ""

    chunks: list[str] = []
    if payload.get("mimeType") == mime_type:
        body_data = (payload.get("body") or {}).get("data")
        if body_data:
            chunks.append(_decode(body_data))
    for part in payload.get("parts") or []:
        nested = collect_text(part, mime_type)
        if nested:
            chunks.append(nested)

    return "\n".join(chunks)


def html_to_text(markup: str) -> str:
    """Crudely flatten HTML into text (good enough for a 4-line preview)."""
    if not markup:
        return ""
    text = _SCRIPT_RE.sub(" ", markup)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    return html.unescape(text)


def clean_lines(
    text: str,
    *,
    max_lines: int = DEFAULT_LINES,
    max_chars: int = DEFAULT_CHARS,
) -> list[str]:
    """Split ``text`` into trimmed, non-empty, non-quoted lines under a budget."""
    if not text:
        return []

    lines: list[str] = []
    for raw_line in text.splitlines():
        if _QUOTED_RE.match(raw_line):
            continue
        line = _WS_RE.sub(" ", raw_line).strip()
        if not line or _NOISE_RE.match(line):
            continue
        lines.append(line)
        if len(lines) >= max_lines:
            break

    budget = max_chars
    trimmed: list[str] = []
    for line in lines:
        if budget <= 0:
            break
        if len(line) > budget:
            line = line[: max(0, budget - 1)].rstrip() + "\u2026"
        trimmed.append(line)
        budget -= len(line)
    return trimmed


def message_preview(
    payload: dict[str, Any] | None,
    *,
    max_lines: int = DEFAULT_LINES,
    max_chars: int = DEFAULT_CHARS,
) -> list[str]:
    """Return the opening lines of a message, preferring the text/plain part."""
    text = collect_text(payload, "text/plain")
    if not text.strip():
        text = html_to_text(collect_text(payload, "text/html"))
    return clean_lines(text, max_lines=max_lines, max_chars=max_chars)
