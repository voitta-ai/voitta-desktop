"""Cache simulator — measures byte-level prefix overlap between consecutive API requests.

Runs as the last middleware, after all optimizers, to capture the exact bytes
that would be sent to the API. Computes the longest common prefix (Anthropic's
cache model) between the current and previous request body per session.
"""

import logging

from .base import Middleware, ProxyRequest

logger = logging.getLogger("voitta-desktop.cache_sim")


class CacheSimulator(Middleware):
    """Tracks per-session request bodies and computes prefix cache overlap."""

    def __init__(self):
        # session_id -> last request body bytes
        self._prev_body: dict[str, bytes] = {}
        # session_id -> list of (total_bytes, prefix_bytes, boundary_msg_index) per turn
        self.history: dict[str, list[tuple[int, int, int]]] = {}

    def get_history(self, session_id: str) -> list[tuple[int, int, int]]:
        """Return [(total_bytes, prefix_bytes, boundary_msg_index), ...] for a session."""
        return self.history.get(session_id, [])

    async def on_request(self, request: ProxyRequest) -> ProxyRequest:
        if not request.path.startswith("/v1/messages"):
            return request
        if not request.body:
            return request

        # Skip quota-check requests (max_tokens=1)
        body = request.json
        if body and body.get("max_tokens") == 1:
            return request

        sid = request.headers.get("X-Claude-Code-Session-Id", "")
        if not sid:
            return request

        current = request.body
        total = len(current)

        prev = self._prev_body.get(sid)
        if prev is None:
            prefix = 0
        else:
            prefix = _common_prefix_len(prev, current)

        # Find which message the prefix boundary falls in
        boundary_msg = _find_boundary_msg(current, prefix)

        self._prev_body[sid] = current

        if sid not in self.history:
            self.history[sid] = []
        self.history[sid].append((total, prefix, boundary_msg))

        pct = (prefix / total * 100) if total > 0 else 0
        logger.debug("cache_sim | sid=%s turn=%d total=%d prefix=%d (%.1f%%)",
                     sid[:12], len(self.history[sid]) - 1, total, prefix, pct)

        return request


def _find_boundary_msg(body: bytes, prefix_len: int) -> int:
    """Find which message index the prefix boundary falls in.

    Scans the serialized JSON for message boundaries ('"role":') and returns
    the index of the last complete message before the prefix boundary.
    Returns -1 if the boundary is in the overhead (before messages).
    """
    if prefix_len <= 0:
        return -1
    # Find all message start positions by scanning for "role" keys
    # Each message in the array starts with {"role":"
    marker = b'"role":'
    msg_index = -1
    pos = 0
    while True:
        pos = body.find(marker, pos)
        if pos < 0 or pos >= prefix_len:
            break
        msg_index += 1
        pos += len(marker)
    # msg_index is the count of complete "role" markers before prefix_len
    # Divide by 2 since each turn has a user + assistant message pair,
    # but we want the raw message index. The tracker groups messages into turns.
    # Return the raw message count (0-based) that fits within the prefix.
    return msg_index


def _common_prefix_len(a: bytes, b: bytes) -> int:
    """Return the length of the longest common prefix between two byte strings."""
    min_len = min(len(a), len(b))
    # Compare in chunks for performance
    chunk = 4096
    matched = 0
    for offset in range(0, min_len, chunk):
        end = min(offset + chunk, min_len)
        a_chunk = a[offset:end]
        b_chunk = b[offset:end]
        if a_chunk == b_chunk:
            matched = end
        else:
            # Find exact divergence point within this chunk
            for i in range(len(a_chunk)):
                if a_chunk[i] != b_chunk[i]:
                    return matched + i
            return matched + len(a_chunk)
    return matched
