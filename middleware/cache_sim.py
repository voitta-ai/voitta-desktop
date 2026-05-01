"""Cache simulator — simulates Anthropic's prompt caching at content block level.

Anthropic's cache processes in order: tools → system → messages.
Cache operates at content block boundaries with cumulative prefix hashing.
A change in any block invalidates all subsequent blocks' cache entries.
The system walks backward from cache_control breakpoints to find the
longest matching prefix of identical content blocks.

Reference: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
"""

import json
import logging

from .base import Middleware, ProxyRequest

logger = logging.getLogger("voitta-desktop.cache_sim")


class CacheSimulator(Middleware):
    """Simulates Anthropic's content-block-level prefix caching."""

    def __init__(self):
        # session_id -> previous request's block sequence
        self._prev_blocks: dict[str, list[str]] = {}
        # session_id -> list of per-turn cache data dicts
        self.history: dict[str, list[dict]] = {}

    def get_history(self, session_id: str) -> list[dict]:
        return self.history.get(session_id, [])

    async def on_request(self, request: ProxyRequest) -> ProxyRequest:
        # Exact match — /v1/messages/count_tokens has a different block shape
        # (typically 1 message, 0 tools, 0 system). Letting it through would
        # overwrite _prev_blocks with that tiny prefix and zero out the match
        # on the next real /v1/messages request.
        if request.path.split("?")[0] != "/v1/messages":
            return request
        if not request.body:
            return request

        body = request.json
        if not body or body.get("max_tokens") == 1:
            return request

        sid = request.headers.get("X-Claude-Code-Session-Id", "")
        if not sid:
            return request

        # Build ordered block sequence: tools → system → messages
        blocks = _extract_blocks(body)
        total_blocks = len(blocks)

        # Find longest matching prefix of identical blocks
        prev = self._prev_blocks.get(sid)
        if prev is None:
            cached_blocks = 0
        else:
            cached_blocks = _matching_prefix_blocks(prev, blocks)

        self._prev_blocks[sid] = blocks

        # Compute byte sizes for chart display
        tools_list = body.get("tools", [])
        system_list = body.get("system", [])
        messages_list = body.get("messages", [])

        tools_block_count = len(tools_list)
        system_block_count = len(system_list)
        # Each message is one block in the sequence

        total_bytes = len(request.body)
        # Estimate cached bytes from block ratio
        cached_ratio = cached_blocks / total_blocks if total_blocks > 0 else 0

        # Find which section the cache boundary falls in
        # Block sequence: tools[0..T-1], system[0..S-1], messages[0..M-1]
        boundary_section, boundary_index = _locate_boundary(
            cached_blocks, tools_block_count, system_block_count
        )

        if sid not in self.history:
            self.history[sid] = []
        self.history[sid].append({
            "total_blocks": total_blocks,
            "cached_blocks": cached_blocks,
            "total_bytes": total_bytes,
            "cached_ratio": cached_ratio,
            "tools_blocks": tools_block_count,
            "system_blocks": system_block_count,
            "boundary_section": boundary_section,
            "boundary_index": boundary_index,
        })

        pct = cached_ratio * 100
        logger.debug("cache_sim | sid=%s turn=%d blocks=%d/%d (%.1f%%) boundary=%s[%d]",
                     sid[:12], len(self.history[sid]) - 1,
                     cached_blocks, total_blocks, pct,
                     boundary_section, boundary_index)

        return request


def _normalize_cch(s: str) -> str:
    """Normalize volatile cch= hash in system prompt blocks."""
    import re
    return re.sub(r'cch=[0-9a-f]+', 'cch=0', s)


def _extract_blocks(body: dict) -> list[str]:
    """Extract content blocks in Anthropic's cache order: tools → system → messages.

    Each block is serialized to a canonical JSON string for comparison.
    Returns a list of block strings.
    """
    blocks = []
    sep = (",", ":")

    # Tools — each tool definition is one block
    for tool in body.get("tools", []):
        blocks.append(json.dumps(tool, separators=sep, sort_keys=True))

    # System — each content block in the system array
    for block in body.get("system", []):
        # Strip cache_control for comparison — it's a directive, not content
        b = {k: v for k, v in block.items() if k != "cache_control"} if isinstance(block, dict) else block
        # Normalize volatile per-request identifiers (cch=XXXXX)
        s = json.dumps(b, separators=sep, sort_keys=True)
        blocks.append(_normalize_cch(s))

    # Messages — each message is one block
    for msg in body.get("messages", []):
        # Strip cache_control from content blocks within messages
        m = _strip_cache_control(msg)
        blocks.append(json.dumps(m, separators=sep, sort_keys=True))

    return blocks


def _strip_cache_control(msg: dict) -> dict:
    """Remove cache_control annotations from a message for comparison.

    cache_control is a caching directive, not semantic content.
    Its presence/absence shouldn't affect content equality.
    """
    content = msg.get("content")
    if isinstance(content, list):
        cleaned = []
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                block = {k: v for k, v in block.items() if k != "cache_control"}
            cleaned.append(block)
        return {**msg, "content": cleaned}
    return msg


def _matching_prefix_blocks(prev: list[str], curr: list[str]) -> int:
    """Count how many blocks from the start are identical between prev and curr."""
    count = 0
    for a, b in zip(prev, curr):
        if a != b:
            break
        count += 1
    return count


def _locate_boundary(cached_blocks: int, tools_count: int, system_count: int) -> tuple[str, int]:
    """Determine which section and index the cache boundary falls in.

    Block order: tools[0..T-1], system[0..S-1], messages[0..M-1]
    Returns (section_name, index_within_section).
    """
    if cached_blocks < tools_count:
        return ("tools", cached_blocks)
    cached_blocks -= tools_count
    if cached_blocks < system_count:
        return ("system", cached_blocks)
    cached_blocks -= system_count
    return ("messages", cached_blocks)
