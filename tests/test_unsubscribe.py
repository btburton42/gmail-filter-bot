"""Tests for List-Unsubscribe parsing and unsubscribe requests."""

import urllib.error
from unittest.mock import MagicMock

import pytest

from gmail_filter_bot import unsubscribe as unsub


class FakeResponse:
    """Minimal stand-in for the object returned by urlopen()."""

    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def getcode(self):
        return self.status


class TestParseListUnsubscribe:
    """Header parsing."""

    def test_parses_url_and_mailto(self):
        targets = unsub.parse_list_unsubscribe(
            "<mailto:unsub@example.com>, <https://example.com/u?id=1>"
        )
        assert targets is not None
        assert targets.urls == ("https://example.com/u?id=1",)
        assert targets.mailtos == ("mailto:unsub@example.com",)
        assert not targets.one_click

    def test_one_click_requires_post_header(self):
        targets = unsub.parse_list_unsubscribe(
            "<https://example.com/u?id=1>", "List-Unsubscribe=One-Click"
        )
        assert targets is not None
        assert targets.one_click

    def test_one_click_is_case_and_space_insensitive(self):
        targets = unsub.parse_list_unsubscribe(
            "<https://example.com/u>", "list-unsubscribe = one-click"
        )
        assert targets is not None
        assert targets.one_click

    def test_one_click_ignored_without_https_url(self):
        targets = unsub.parse_list_unsubscribe(
            "<mailto:unsub@example.com>", "List-Unsubscribe=One-Click"
        )
        assert targets is not None
        assert not targets.one_click

    def test_https_urls_filters_insecure(self):
        targets = unsub.parse_list_unsubscribe(
            "<http://insecure.example.com/u>, <https://secure.example.com/u>"
        )
        assert targets is not None
        assert targets.https_urls == ("https://secure.example.com/u",)
        assert targets.insecure_urls == ("http://insecure.example.com/u",)

    def test_tolerates_missing_angle_brackets(self):
        targets = unsub.parse_list_unsubscribe("https://example.com/u, mailto:x@y.com")
        assert targets is not None
        assert targets.urls == ("https://example.com/u",)
        assert targets.mailtos == ("mailto:x@y.com",)

    @pytest.mark.parametrize("header", [None, "", "   "])
    def test_missing_header_returns_none(self, header):
        assert unsub.parse_list_unsubscribe(header) is None

    def test_unusable_header_returns_none(self):
        assert unsub.parse_list_unsubscribe("nonsense") is None


class TestUnsubscribe:
    """Performing the unsubscribe request."""

    def test_one_click_posts_and_succeeds(self, monkeypatch):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append((request, timeout))
            return FakeResponse(200)

        monkeypatch.setattr(unsub.urllib.request, "urlopen", fake_urlopen)
        targets = unsub.UnsubscribeTargets(
            urls=("https://example.com/u?id=1",), mailtos=(), one_click=True
        )

        result = unsub.unsubscribe(targets)

        assert result.status == "one_click"
        assert result.ok
        assert "example.com" in result.message
        request, timeout = calls[0]
        assert request.data == unsub.ONE_CLICK_BODY
        assert request.get_method() == "POST"
        assert timeout == unsub.TIMEOUT

    def test_one_click_failure_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            unsub.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse(400)
        )
        targets = unsub.UnsubscribeTargets(
            urls=("https://example.com/u",), mailtos=(), one_click=True
        )

        result = unsub.unsubscribe(targets)

        assert result.status == "failed"
        assert not result.ok
        assert "400" in result.message

    def test_plain_url_does_a_get(self, monkeypatch):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["method"] = request.get_method()
            return FakeResponse(200)

        monkeypatch.setattr(unsub.urllib.request, "urlopen", fake_urlopen)
        targets = unsub.UnsubscribeTargets(
            urls=("https://example.com/u",), mailtos=(), one_click=False
        )

        result = unsub.unsubscribe(targets)

        assert seen["method"] == "GET"
        assert result.status == "url"
        assert result.ok
        assert "confirm" in result.message

    def test_prefers_https_over_insecure_url(self, monkeypatch):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            return FakeResponse(200)

        monkeypatch.setattr(unsub.urllib.request, "urlopen", fake_urlopen)
        targets = unsub.UnsubscribeTargets(
            urls=("http://insecure.example.com/u", "https://secure.example.com/u"),
            mailtos=(),
            one_click=False,
        )

        result = unsub.unsubscribe(targets)

        assert result.ok
        assert seen["url"] == "https://secure.example.com/u"

    def test_refuses_insecure_only_url(self, monkeypatch):
        def boom(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("should not contact an http:// endpoint")

        monkeypatch.setattr(unsub.urllib.request, "urlopen", boom)
        targets = unsub.UnsubscribeTargets(
            urls=("http://insecure.example.com/u",), mailtos=(), one_click=False
        )

        result = unsub.unsubscribe(targets)

        assert result.status == "failed"
        assert "non-https" in result.message

    def test_mailto_only_is_manual(self, monkeypatch):
        def boom(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("should not make a request")

        monkeypatch.setattr(unsub.urllib.request, "urlopen", boom)
        targets = unsub.parse_list_unsubscribe("<mailto:unsub@example.com?subject=unsubscribe>")

        result = unsub.unsubscribe(targets)

        assert result.status == "manual"
        assert not result.ok
        assert "unsub@example.com" in result.message
        assert "subject" not in result.message

    def test_none_targets_is_unsupported(self):
        result = unsub.unsubscribe(None)
        assert result.status == "unsupported"
        assert not result.ok

    def test_url_error_is_reported(self, monkeypatch):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("dns failure")

        monkeypatch.setattr(unsub.urllib.request, "urlopen", fake_urlopen)
        targets = unsub.UnsubscribeTargets(
            urls=("https://example.com/u",), mailtos=(), one_click=True
        )

        result = unsub.unsubscribe(targets)

        assert result.status == "failed"
        assert "dns failure" in result.message

    def test_http_error_is_reported(self, monkeypatch):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError("https://example.com/u", 403, "Forbidden", {}, None)

        monkeypatch.setattr(unsub.urllib.request, "urlopen", fake_urlopen)
        targets = unsub.UnsubscribeTargets(
            urls=("https://example.com/u",), mailtos=(), one_click=False
        )

        result = unsub.unsubscribe(targets)

        assert result.status == "failed"
        assert "403" in result.message


class TestClientIntegration:
    """GmailClient.get_unsubscribe_targets reads the right headers."""

    def _client(self):
        from gmail_filter_bot.gmail_client import GmailClient

        client = GmailClient.__new__(GmailClient)  # skip OAuth
        client.service = MagicMock()
        return client

    def test_reads_headers(self):
        client = self._client()
        client.service.users().messages().get().execute.return_value = {
            "payload": {
                "headers": [
                    {"name": "List-Unsubscribe", "value": "<https://example.com/u>"},
                    {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
                ]
            }
        }

        targets = client.get_unsubscribe_targets("m1")

        assert targets is not None
        assert targets.urls == ("https://example.com/u",)
        assert targets.one_click
        kwargs = client.service.users().messages().get.call_args.kwargs
        assert kwargs["format"] == "metadata"
        assert "List-Unsubscribe" in kwargs["metadataHeaders"]

    def test_missing_header_returns_none(self):
        client = self._client()
        client.service.users().messages().get().execute.return_value = {
            "payload": {"headers": [{"name": "From", "value": "a@b.com"}]}
        }

        assert client.get_unsubscribe_targets("m1") is None

    def test_http_error_returns_none(self):
        from googleapiclient.errors import HttpError

        client = self._client()
        client.service.users().messages().get().execute.side_effect = HttpError(
            MagicMock(status=404), b"not found"
        )

        assert client.get_unsubscribe_targets("m1") is None
