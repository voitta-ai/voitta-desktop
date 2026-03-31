"""Context optimizers — strip stale or redundant content from older turns."""

import base64
import logging

from middleware.base import Middleware, ProxyRequest
from middleware.models import parse_image_dimensions

logger = logging.getLogger("voitta-desktop.optimizers")

# Per-million input token pricing by model family
_INPUT_PRICE_PER_M = {
    "opus": 15.0,
    "sonnet": 3.0,
    "haiku": 0.80,
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


class BaseOptimizer(Middleware):
    """Base class for context optimizers.

    Subclasses implement `_optimize()` to modify messages and return tokens saved.
    Common infrastructure: turn-boundary detection, per-model savings tracking.

    Each subclass should set `chart_key` to the turn-data field it strips
    (e.g. "image", "bash", "stale_read", "thinking").  The pipeline uses
    this to tell the chart which optimizers are active.
    """

    chart_key: str = ""

    def __init__(self, keep_turns: int = 5):
        self.keep_turns = keep_turns
        self.tokens_saved: dict[str, int] = {}

    @property
    def total_savings_usd(self) -> float:
        total = 0.0
        for family, tokens in self.tokens_saved.items():
            price = _INPUT_PRICE_PER_M.get(family, 3.0)
            total += tokens / 1_000_000 * price
        return total

    def _find_user_turn_starts(self, messages: list) -> list[int]:
        """Return message indices where each user turn begins."""
        starts: list[int] = []
        prev_role = None
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            if role == "user" and prev_role != "user":
                starts.append(i)
            prev_role = role
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
        body = request.require_json()
        if not body or not body.get("messages"):
            return request

        model = body.get("model", "")
        messages = body["messages"]

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

    def __init__(self, optimizers: list[BaseOptimizer], enabled: bool = False, haiku_only: bool = True):
        self.optimizers = optimizers
        self.enabled = enabled
        self.haiku_only = haiku_only

    @property
    def total_savings_usd(self) -> float:
        return sum(o.total_savings_usd for o in self.optimizers)

    @property
    def active_optimizers(self) -> dict[str, int]:
        """Return {chart_key: keep_turns} for each enabled optimizer."""
        if not self.enabled:
            return {}
        return {o.chart_key: o.keep_turns for o in self.optimizers if o.chart_key}

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
        return request
