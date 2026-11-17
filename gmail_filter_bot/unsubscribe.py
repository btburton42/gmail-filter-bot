"""Unsubscribe from mailing lists via ``List-Unsubscribe`` headers.

Gmail's API has **no** unsubscribe call. The ability to unsubscribe is
advertised per-message instead, using two headers (RFC 2369 and RFC 8058):

    List-Unsubscribe: <mailto:unsub@example.com>, <https://example.com/u?id=1>
    List-Unsubscribe-Post: List-Unsubscribe=One-Click

This module parses those headers and performs the unsubscribe. It is split
from :mod:`gmail_filter_bot.gmail_client` (which only *fetches* the headers)
so the parsing and the network call are both testable without a Gmail client.

Handling tiers, best to worst:

  1. ``https://`` URL **plus** ``List-Unsubscribe-Post: List-Unsubscribe=One-Click``
     -- RFC 8058 one-click: a single POST with no confirmation page. This is
     the only path that reliably completes without a human in a browser, and
     bulk senders are now required to support it.
  2. ``https://`` URL only -- issued as a GET. Usually lands on a confirmation
     page, so the result is reported as "maybe" rather than "done".
  3. ``mailto:`` only -- would require the ``gmail.send`` scope to mail the
     request, which this tool does not request. Reported as a manual step.
  4. Nothing usable -- reported as unsupported.

Only ``https://`` endpoints are contacted; an ``http://`` target is refused
rather than sent in cleartext.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

#: How long to wait on the unsubscribe endpoint, in seconds.
TIMEOUT = 10

#: Some senders put a bare mailto/URL in the header without angle brackets.
_ANGLE_BRACKET_RE = re.compile(r"<([^>]+)>")

#: Sent to the endpoint's User-Agent. Identifies us; some EDIs gate on this.
USER_AGENT = "gmail-filter-bot (+https://github.com/btburton42/gmail-filter-bot)"

#: RFC 8058 defines this exact body for one-click unsubscribes.
ONE_CLICK_BODY = b"List-Unsubscribe=One-Click"


@dataclass(frozen=True)
class UnsubscribeTargets:
    """Parsed ``List-Unsubscribe`` options for a single message."""

    urls: tuple[str, ...]
    mailtos: tuple[str, ...]
    one_click: bool

    @property
    def https_urls(self) -> tuple[str, ...]:
        """URLs that are safe to contact (https only)."""
        return tuple(u for u in self.urls if u.lower().startswith("https://"))

    @property
    def insecure_urls(self) -> tuple[str, ...]:
        """URLs we refuse to contact because they are not https."""
        return tuple(u for u in self.urls if not u.lower().startswith("https://"))


@dataclass(frozen=True)
class UnsubscribeResult:
    """Outcome of an unsubscribe attempt.

    ``status`` is one of:

      - ``one_click`` -- RFC 8058 POST succeeded; the sender has been told.
      - ``url``       -- GET issued and the endpoint returned 2xx, but the
                        sender may still want a confirmation click.
      - ``manual``    -- only a ``mailto:`` was offered; nothing was sent.
      - ``unsupported`` -- no usable endpoint in the headers.
      - ``failed``    -- an endpoint existed but the request failed.
    """

    status: str
    message: str
    url: str | None = None

    @property
    def ok(self) -> bool:
        """True when the sender has (or very likely has) been unsubscribed."""
        return self.status in ("one_click", "url")


def parse_list_unsubscribe(
    header: str | None, post_header: str | None = None
) -> UnsubscribeTargets | None:
    """Parse ``List-Unsubscribe`` / ``List-Unsubscribe-Post`` header values.

    Returns ``None`` when the headers advertise nothing actionable.
    """
    if not header or not header.strip():
        return None

    raw_targets = _ANGLE_BRACKET_RE.findall(header)
    if not raw_targets:
        # Tolerate a header without angle brackets.
        raw_targets = header.split(",")

    stripped = [t.strip() for t in raw_targets]
    candidates = [t for t in stripped if t]
    urls = tuple(t for t in candidates if t.lower().startswith(("http://", "https://")))
    mailtos = tuple(t for t in candidates if t.lower().startswith("mailto:"))

    if not urls and not mailtos:
        return None

    one_click = False
    if post_header and "one-click" in post_header.lower().replace(" ", ""):
        # RFC 8058 one-click only applies to an https URL.
        one_click = any(u.lower().startswith("https://") for u in urls)

    return UnsubscribeTargets(urls=urls, mailtos=mailtos, one_click=one_click)


def _host(url: str) -> str:
    """Return the host[:port] of ``url`` for display (never the full URL)."""
    try:
        return urllib.parse.urlsplit(url).netloc or url
    except ValueError:  # pragma: no cover - urlsplit is very forgiving
        return url


def _mailto_address(mailto: str) -> str:
    """Strip ``mailto:`` and any ``?subject=`` query from a mailto target."""
    target = mailto[len("mailto:") :] if mailto.lower().startswith("mailto:") else mailto
    address, _, _query = target.partition("?")
    return urllib.parse.unquote(address).strip()


def _request(url: str, *, data: bytes | None = None) -> int:
    """Send a single request to ``url`` and return the HTTP status code."""
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:  # noqa: S310
        return int(getattr(response, "status", 0) or response.getcode())


def unsubscribe(targets: UnsubscribeTargets | None) -> UnsubscribeResult:
    """Perform the best available unsubscribe for ``targets``.

    Never raises: transport and HTTP errors are folded into a ``failed``
    result so the interactive triage loop can carry on.
    """
    if targets is None:
        return UnsubscribeResult(
            "unsupported",
            "no List-Unsubscribe header on the most recent message",
        )

    if not targets.https_urls and targets.insecure_urls:
        return UnsubscribeResult(
            "failed",
            f"refusing non-https unsubscribe URL ({_host(targets.insecure_urls[0])})",
        )

    if targets.https_urls:
        url = targets.https_urls[0]
        host = _host(url)
        try:
            if targets.one_click:
                status = _request(url, data=ONE_CLICK_BODY)
                if 200 <= status < 300:
                    return UnsubscribeResult(
                        "one_click", f"unsubscribed (one-click via {host})", url
                    )
                return UnsubscribeResult(
                    "failed", f"one-click request to {host} returned HTTP {status}", url
                )
            status = _request(url)
            if 200 <= status < 300:
                return UnsubscribeResult(
                    "url",
                    f"opened {host} (HTTP {status}); sender may still need a confirm click",
                    url,
                )
            return UnsubscribeResult("failed", f"request to {host} returned HTTP {status}", url)
        except urllib.error.HTTPError as e:
            return UnsubscribeResult("failed", f"{host} returned HTTP {e.code}", url)
        except urllib.error.URLError as e:
            return UnsubscribeResult("failed", f"could not reach {host}: {e.reason}", url)
        except (OSError, ValueError) as e:  # timeouts, DNS, malformed URL
            return UnsubscribeResult("failed", f"could not reach {host}: {e}", url)

    if targets.mailtos:
        address = _mailto_address(targets.mailtos[0])
        return UnsubscribeResult(
            "manual",
            f"only an email option is offered; unsubscribe by mailing {address}",
        )

    return UnsubscribeResult("unsupported", "no usable unsubscribe endpoint found")
