"""Tests for the message-body preview used by the `expand` triage key."""

import base64

from gmail_filter_bot import preview


def _b64(text: str) -> str:
    """Encode as Gmail does: base64url, stripped of padding."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _payload(mime_type: str, text: str) -> dict:
    return {"mimeType": mime_type, "body": {"data": _b64(text)}}


def test_plain_text_payload():
    payload = _payload("text/plain", "First line\nSecond line\nThird line")
    assert preview.message_preview(payload) == ["First line", "Second line", "Third line"]


def test_respects_max_lines():
    payload = _payload("text/plain", "\n".join(f"line {i}" for i in range(20)))
    lines = preview.message_preview(payload, max_lines=3)
    assert lines == ["line 0", "line 1", "line 2"]


def test_blank_and_quoted_lines_are_dropped():
    text = "\n\nHello there\n\n> quoted reply\n>\nThanks!"
    payload = _payload("text/plain", text)
    assert preview.message_preview(payload) == ["Hello there", "Thanks!"]


def test_boilerplate_is_dropped():
    text = "View this email in your browser\nActual content\nUnsubscribe here"
    payload = _payload("text/plain", text)
    assert preview.message_preview(payload) == ["Actual content", "Unsubscribe here"]


def test_truncates_to_character_budget():
    payload = _payload("text/plain", "x" * 500)
    lines = preview.message_preview(payload, max_chars=50)
    assert len(lines) == 1
    assert lines[0].endswith("\u2026")
    assert len(lines[0]) == 50


def test_html_body_is_flattened_when_no_plain_part():
    html = "<html><body><p>The Stories People Are Talking About</p><p>Don&rsquo;t miss out</p></body></html>"
    payload = _payload("text/html", html)
    lines = preview.message_preview(payload)
    assert lines[0] == "The Stories People Are Talking About"
    assert "Don\u2019t miss out" in lines[1]
    assert "<p>" not in " ".join(lines)


def test_prefers_plain_text_in_multipart():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            _payload("text/plain", "plain wins"),
            _payload("text/html", "<p>html loses</p>"),
        ],
    }
    assert preview.message_preview(payload) == ["plain wins"]


def test_nested_parts_are_walked():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [_payload("text/plain", "nested body")],
            }
        ],
    }
    assert preview.message_preview(payload) == ["nested body"]


def test_empty_payloads():
    assert preview.message_preview(None) == []
    assert preview.message_preview({}) == []
    assert preview.message_preview(_payload("text/plain", "")) == []


def test_script_and_style_are_stripped():
    html = "<style>.a{color:red}</style><script>alert(1)</script><p>Readable</p>"
    assert preview.html_to_text(html).strip() == "Readable"
