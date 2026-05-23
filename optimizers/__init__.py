"""Context optimizers — strip stale or redundant content from older turns."""

import base64
import logging

from middleware.base import Middleware, ProxyRequest
from middleware.models import parse_image_dimensions

logger = logging.getLogger("voitta-desktop.optimizers")

# Per-million token pricing by model family.
# Stripped tokens would, in steady state, have been served as cache reads
# (not fresh input) — Anthropic prices cache reads at 10% of input. We use
# the cache-read rate so displayed savings reflect the realistic avoided
# cost, not a 10x-inflated figure priced at full input rate.
_CACHE_READ_PRICE_PER_M = {
    "opus": 1.50,
    "sonnet": 0.30,
    "haiku": 0.08,
}


def model_family(model: str) -> str:
    """Map a model ID like 'claude-sonnet-4-20250514' to a family key."""
    m = model.lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in m:
            return family
    return "sonnet"


def image_tokens(item: dict) -> int:
    """Estimate token count for an image using Anthropic's formula: (w*h)/750."""
    src = item.get("source", {})
    data_str = src.get("data", "")
    media_type = src.get("media_type", "image/png")

    if not data_str:
        return 0

    try:
        decode_chars = min(len(data_str), 174763)
        header_b64 = data_str[:decode_chars]
        pad_needed = (4 - len(header_b64) % 4) % 4
        header_raw = base64.b64decode(header_b64 + "=" * pad_needed)
        w, h = parse_image_dimensions(header_raw, media_type)
        if w > 0 and h > 0:
            return max(1, (w * h) // 750)
    except Exception:
        pass

    # Fallback: rough estimate from base64 size
    raw_bytes = (len(data_str) * 3) // 4
    pixels = raw_bytes // 4
    return max(1, pixels // 750)


def _pick_ttl(body: dict, messages: list, bp_msg_idx: int) -> str:
    """Pick a cache_control ttl that respects Anthropic's ordering constraint.

    Rule: a `ttl='1h'` block must not come after a `ttl='5m'` block in
    processing order (tools → system → messages, in document order).

    Strategy:
      - If any 5m block already appears before our injection point, we must
        use 5m (a 1h here would sit after a 5m → API error).
      - Otherwise, match the TTL Claude Code uses on nearby in-message
        breakpoints; if no in-message breakpoint exists, fall back to the
        system/tools TTL; if none at all, default to 5m.
    """
    def _ttl(block):
        if not isinstance(block, dict):
            return None
        cc = block.get("cache_control")
        if isinstance(cc, dict):
            return cc.get("ttl", "5m")  # Anthropic defaults ephemeral to 5m
        return None

    def _iter_ttls_before():
        """Yield TTLs of cache_control blocks strictly before bp_msg_idx,
        in processing order (tools → system → messages[0..bp_msg_idx-1])."""
        for tool in body.get("tools") or []:
            t = _ttl(tool)
            if t:
                yield t
        for block in body.get("system") or []:
            t = _ttl(block)
            if t:
                yield t
        for i in range(bp_msg_idx):
            m = messages[i]
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list):
                for block in content:
                    t = _ttl(block)
                    if t:
                        yield t

    seen_before = list(_iter_ttls_before())
    if "5m" in seen_before:
        return "5m"

    # No 5m before us — safe to use either. Match Claude Code's in-message
    # breakpoint style if we can see one; otherwise fall back to tools/system.
    for i in range(bp_msg_idx, len(messages)):
        m = messages[i]
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for block in content:
                t = _ttl(block)
                if t:
                    return t
    if seen_before:
        return seen_before[-1]
    return "5m"


class BaseOptimizer(Middleware):
    """Base class for context optimizers.

    Subclasses implement `_optimize()` to modify messages and return tokens saved.
    Common infrastructure: turn-boundary detection, per-model savings tracking.

    Each subclass should set `chart_key` to the turn-data field it strips
    (e.g. "image", "bash", "thinking").  The pipeline uses this to tell
    the chart which optimizers are active.
    """

    chart_key: str = ""

    def __init__(self, keep_turns: int = 5):
        self.keep_turns = keep_turns
        self.tokens_saved: dict[str, int] = {}
        # Populated by _optimize(): tool_use_id → chars stripped
        self.last_stripped_ids: dict[str, int] = {}
        # For thinking blocks: msg_index → chars stripped
        self.last_stripped_msg_indices: dict[int, int] = {}

    @property
    def total_savings_usd(self) -> float:
        total = 0.0
        for family, tokens in self.tokens_saved.items():
            price = _CACHE_READ_PRICE_PER_M.get(family, 0.30)
            total += tokens / 1_000_000 * price
        return total

    @staticmethod
    def _is_human_input(msg: dict) -> bool:
        """True if this user message contains human-typed text (not just tool_result)."""
        content = msg.get("content")
        if isinstance(content, str):
            return True
        if isinstance(content, list):
            return any(
                isinstance(b, dict) and b.get("type") not in ("tool_result",)
                for b in content
            )
        return False

    def _find_user_turn_starts(self, messages: list) -> list[int]:
        """Return message indices where each human input turn begins.

        Only counts user messages with actual text — tool_result-only
        messages (tool round-trips) are not separate turns.
        """
        starts: list[int] = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and self._is_human_input(msg):
                starts.append(i)
        return starts

    def _threshold_msg_index(self, messages: list) -> int | None:
        """Return the message index below which content can be optimized.

        Returns None if there aren't enough turns to apply the threshold.
        """
        starts = self._find_user_turn_starts(messages)
        if len(starts) <= self.keep_turns:
            return None
        return starts[len(starts) - self.keep_turns]

    async def on_request(self, request: ProxyRequest) -> ProxyRequest:
        body = request.json
        if not body or not body.get("messages"):
            return request

        model = body.get("model", "")
        messages = body["messages"]

        self.last_stripped_ids = {}
        self.last_stripped_msg_indices = {}

        threshold = self._threshold_msg_index(messages)
        if threshold is None:
            return request

        # Work on a copy so earlier middlewares keep the original
        messages = list(messages)
        tokens_removed = self._optimize(messages, threshold)

        if tokens_removed > 0:
            family = model_family(model)
            self.tokens_saved[family] = self.tokens_saved.get(family, 0) + tokens_removed
            request.json = dict(body, messages=messages)
            logger.info("%s: saved %d tokens (family=%s, cumulative $%.4f)",
                        self.__class__.__name__, tokens_removed, family, self.total_savings_usd)

        return request

    def _optimize(self, messages: list, threshold_msg_idx: int) -> int:
        """Modify messages in-place and return total tokens saved.

        Override in subclasses. Only modify messages before threshold_msg_idx.
        """
        raise NotImplementedError


class OptimizerPipeline(Middleware):
    """Runs multiple optimizers in sequence, aggregates savings."""

    def __init__(self, optimizers: list[BaseOptimizer], enabled: bool = True, haiku_only: bool = False, tracker=None):
        self.optimizers = optimizers
        self.enabled = enabled
        self.haiku_only = haiku_only
        self._tracker = tracker  # optional ref for per-turn stripped_chars stamping

    @property
    def total_savings_usd(self) -> float:
        return sum(o.total_savings_usd for o in self.optimizers)

    @property
    def active_optimizers(self) -> dict[str, int]:
        """Return {chart_key: keep_turns} for each enabled optimizer."""
        if not self.enabled:
            return {}
        return {o.chart_key: o.keep_turns for o in self.optimizers if o.chart_key}

    @property
    def stripped_tool_ids(self) -> dict[str, int]:
        """Merged {tool_use_id: chars} across all optimizers from last request."""
        merged: dict[str, int] = {}
        for o in self.optimizers:
            merged.update(o.last_stripped_ids)
        return merged

    @property
    def stripped_msg_indices(self) -> dict[int, int]:
        """Merged {msg_index: chars} for thinking blocks from last request."""
        merged: dict[int, int] = {}
        for o in self.optimizers:
            merged.update(o.last_stripped_msg_indices)
        return merged

    # Minimum turns before we inject a cache breakpoint
    CACHE_BP_MIN_TURNS = 4

    def _inject_cache_breakpoint(self, request: ProxyRequest) -> ProxyRequest:
        """Add a cache_control breakpoint one turn before the optimizer threshold.

        Places it on the last content block of the message just before the
        "about to be stripped" turn — the fully-stable zone where all content
        was stripped in a previous request and won't change again.
        """
        body = request.json
        if not body:
            return request
        messages = body.get("messages")
        if not messages:
            return request

        starts = [
            i for i, m in enumerate(messages)
            if m.get("role") == "user" and BaseOptimizer._is_human_input(m)
        ]
        if len(starts) < self.CACHE_BP_MIN_TURNS:
            return request

        # Use the smallest non-zero keep_turns across optimizers. The breakpoint
        # marks the leading edge of the *fully-optimized* zone — the region where
        # every threshold-based optimizer has already run. That zone ends one
        # turn before the most-aggressive (smallest) threshold; placing the
        # marker any deeper would land in a flux band where bytes flip when an
        # optimizer with a larger keep_turns crosses its own threshold next turn.
        # BashCompressor uses keep_turns=0 (processes all messages) and is
        # excluded from the min — it doesn't define a threshold boundary.
        keeps = [o.keep_turns for o in self.optimizers if o.keep_turns > 0]
        keep = min(keeps) if keeps else 5

        stable_idx = len(starts) - keep - 1
        if stable_idx < 0:
            return request

        # Breakpoint goes on the last message before the stable turn's start
        # (i.e., the last message of the turn before the stable turn)
        # Actually: on the last message of the stable turn itself
        # stable turn starts at starts[stable_idx], next turn at starts[stable_idx+1]
        if stable_idx + 1 < len(starts):
            bp_msg_idx = starts[stable_idx + 1] - 1
        else:
            bp_msg_idx = len(messages) - 1

        if bp_msg_idx < 0:
            return request

        # Pick TTL that respects Anthropic's ordering constraint:
        # a ttl='1h' block must not come after any ttl='5m' block in
        # processing order (tools → system → messages). We inspect the
        # cache_control blocks Claude Code has already placed and adapt.
        ttl = _pick_ttl(body, messages, bp_msg_idx)
        bp = {"type": "ephemeral", "ttl": ttl}
        messages = list(messages)
        msg = dict(messages[bp_msg_idx])
        content = msg.get("content")
        if isinstance(content, list) and content:
            content = list(content)
            last_block = dict(content[-1])
            if "cache_control" not in last_block:
                last_block["cache_control"] = bp
                content[-1] = last_block
                msg["content"] = content
        elif isinstance(content, str):
            # Wrap string content in a block so we can add cache_control
            msg["content"] = [{"type": "text", "text": content, "cache_control": bp}]
        else:
            return request

        messages[bp_msg_idx] = msg
        request.json = dict(body, messages=messages)
        logger.info("Cache breakpoint injected at msg[%d] ttl=%s (stable turn %d/%d)",
                     bp_msg_idx, ttl, stable_idx, len(starts))
        return request

    async def on_request(self, request: ProxyRequest) -> ProxyRequest:
        if not self.enabled:
            return request

        # Check haiku_only filter
        if self.haiku_only:
            body = request.require_json()
            if body:
                model = body.get("model", "").lower()
                if "haiku" not in model:
                    return request

        for o in self.optimizers:
            request = await o.on_request(request)

        # Stamp per-turn stripped_chars on the current turn so historical
        # charts can show pre vs post without relying on last_stripped_* dicts
        # (which reset each request).
        if self._tracker:
            body = request.json
            if body:
                sid = request.headers.get("X-Claude-Code-Session-Id", "")
                if not sid:
                    import hashlib, json as _json
                    sid = hashlib.md5(_json.dumps(body.get("system", "")).encode()).hexdigest()[:8]
                conv = self._tracker.conversations.get(sid)
                if conv and conv.turns:
                    total_stripped = sum(self.stripped_tool_ids.values()) + sum(self.stripped_msg_indices.values())
                    conv.turns[-1].stripped_chars = total_stripped

        # Inject cache breakpoint only when optimization is active
        request = self._inject_cache_breakpoint(request)
        return request
