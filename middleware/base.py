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

    def require_json(self) -> dict:
        """Parse request body as JSON or raise a descriptive error."""
        if self._json is not None:
            return self._json
        if not self.body:
            raise ValueError("Expected JSON request body, got empty body")
        try:
            self._json = json.loads(self.body)
        except UnicodeDecodeError as e:
            raise ValueError("Request body is not valid UTF-8 JSON") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"Request body is not valid JSON: {e}") from e
        return self._json

    @json.setter
    def json(self, value: dict):
        self._json = value
        self.body = json.dumps(value, separators=(',', ':')).encode()


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
    if not encoding or encoding == "identity":
        return data.decode("utf-8", errors="replace")
    if encoding == "gzip":
        return gzip.decompress(data).decode("utf-8", errors="replace")
    if encoding == "deflate":
        return zlib.decompress(data).decode("utf-8", errors="replace")
    if encoding == "br":
        try:
            import brotli
        except ImportError as e:
            raise RuntimeError("brotli support is required for br-encoded responses") from e
        return brotli.decompress(data).decode("utf-8", errors="replace")
    raise ValueError(f"Unsupported Content-Encoding: {encoding}")
