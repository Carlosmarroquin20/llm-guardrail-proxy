"""Upstream forwarder.

Wraps an injected :class:`httpx.AsyncClient` with a circuit breaker and
returns a Starlette ``StreamingResponse`` so the proxy can support both
chunked SSE responses (chat streaming) and conventional bodies through the
same code path.

Hop-by-hop headers (RFC 7230 §6.1) and any header that would corrupt the
client-visible transport are stripped before relay.
"""

from __future__ import annotations

from typing import Mapping

import httpx
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from llm_guardrail_proxy.proxy.circuit_breaker import CircuitBreaker
from llm_guardrail_proxy.proxy.envelope import Provider, ProxyRequest
from llm_guardrail_proxy.proxy.exceptions import UpstreamError

# Headers that must never be propagated. ``content-length`` and
# ``transfer-encoding`` are recomputed by the response layer; the others are
# hop-by-hop or carry information about the prior hop (``host``) that the
# next hop must derive for itself.
_HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def _filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}


class UpstreamForwarder:
    """Forward an approved :class:`ProxyRequest` to its provider origin.

    The forwarder owns neither the HTTP client nor the breaker — both are
    injected so callers can share a single client pool across many requests
    and so tests can substitute ``httpx.MockTransport`` for the network.
    """

    __slots__ = ("_breaker", "_client", "_origins")

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        breaker: CircuitBreaker,
        origins: Mapping[Provider, str],
    ) -> None:
        self._client = client
        self._breaker = breaker
        self._origins = dict(origins)

    async def forward(self, request: ProxyRequest) -> StreamingResponse:
        origin = self._origins.get(request.parsed.provider)
        if origin is None:
            # This is a programming error, not a transport one: the
            # adapter accepted a path the forwarder cannot route. We surface
            # it as ``UpstreamError`` because, from the client's perspective,
            # the upstream is unavailable.
            raise UpstreamError(
                f"No upstream origin configured for provider "
                f"'{request.parsed.provider.value}'."
            )

        url = origin.rstrip("/") + request.path
        outbound_headers = _filter_headers(request.headers)

        async def _send() -> httpx.Response:
            outbound = self._client.build_request(
                method=request.method,
                url=url,
                headers=outbound_headers,
                content=request.raw_body,
            )
            try:
                return await self._client.send(outbound, stream=True)
            except httpx.HTTPError as exc:
                raise UpstreamError(f"Upstream call failed: {exc}") from exc

        upstream = await self._breaker.call(_send)

        # The response object owns an open socket; closing it must happen
        # only *after* the StreamingResponse has drained the body. Starlette
        # invokes the BackgroundTask once the response is fully sent.
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=_filter_headers(upstream.headers),
            media_type=upstream.headers.get("content-type"),
            background=BackgroundTask(upstream.aclose),
        )
