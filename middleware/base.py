"""Base middleware classes and request/response data objects."""

import gzip
import json
import zlib
from dataclasses import dataclass, field


@dataclass
class ProxyRequest:
    """Mutable request object passed through middleware chain."""
    method: str
    path: str
    headers: dict
    body: bytes
    _json: dict | None = field(default=None, repr=False)

    @property
    def json(self) -> dict | None:
        if self._json is None and self.body:
            try:
                self._json = json.loads(self.body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return self._json

    @json.setter
    def json(self, value: dict):
        self._json = value
        self.body = json.dumps(value).encode()


@dataclass
class ProxyResponse:
    """Mutable response metadata (headers/status). Body chunks flow separately."""
    status: int
    headers: dict


class Middleware:
    """Base class for request/response middleware."""

    async def on_request(self, request: ProxyRequest) -> ProxyRequest:
        return request

    async def on_response_started(self, request: ProxyRequest, response: ProxyResponse) -> ProxyResponse:
        return response

    async def on_response_chunk(self, request: ProxyRequest, chunk: bytes) -> bytes:
        return chunk

    async def on_response_done(self, request: ProxyRequest, response: ProxyResponse):
        pass


def decompress(data: bytes, encoding: str) -> str:
    """Decompress response body based on Content-Encoding header."""
    encoding = encoding.lower().strip()
    try:
        if encoding == "gzip":
            return gzip.decompress(data).decode("utf-8", errors="replace")
        elif encoding == "deflate":
            return zlib.decompress(data).decode("utf-8", errors="replace")
        elif encoding == "br":
            try:
                import brotli
                return brotli.decompress(data).decode("utf-8", errors="replace")
            except ImportError:
                pass
    except Exception:
        pass
    return data.decode("utf-8", errors="replace")
