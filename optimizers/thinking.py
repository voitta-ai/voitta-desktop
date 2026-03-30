"""ThinkingOptimizer — strips thinking blocks from older assistant turns.

Older thinking blocks should either be passed back exactly as received or
omitted entirely. Rewriting them in-place can invalidate Anthropic's required
`signature` field for extended thinking blocks.
"""

from . import BaseOptimizer

THINKING_KEEP_TURNS = 5


class ThinkingOptimizer(BaseOptimizer):
    """Omit older thinking blocks from assistant turns."""

    def __init__(self, keep_turns: int = THINKING_KEEP_TURNS):
        super().__init__(keep_turns=keep_turns)

    def _optimize(self, messages: list, threshold_msg_idx: int) -> int:
        tokens_removed = 0

        for i in range(threshold_msg_idx):
            msg = messages[i]
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            new_content = []
            modified = False
            for item in content:
                if not isinstance(item, dict):
                    new_content.append(item)
                    continue

                if item.get("type") == "thinking":
                    thinking_text = item.get("thinking", "")
                    signature = item.get("signature", "")
                    saved_chars = len(thinking_text) + len(signature)
                    tokens_removed += int(saved_chars / 3.5)
                    modified = True
                    continue

                new_content.append(item)

            if modified:
                messages[i] = dict(msg, content=new_content)

        return tokens_removed
