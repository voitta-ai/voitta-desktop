"""ImageOptimizer — strips images from older conversation turns, stores them by hash."""

import hashlib

from . import BaseOptimizer, image_tokens

IMAGE_KEEP_TURNS = 5

# Global hash→object store shared with the MCP get_vt_object tool.
# Each entry: {"type": "image"|..., "data": <original content block>}
vt_object_store: dict[str, dict] = {}


def _image_hash(item: dict) -> str:
    """Compute a short hash from image data."""
    data = item.get("source", {}).get("data", "")
    return hashlib.sha256(data[:4096].encode()).hexdigest()[:12]


def _store_image(item: dict) -> str:
    """Store an image block in the global store and return its hash."""
    img_hash = _image_hash(item)
    vt_object_store[img_hash] = {"type": "image", "data": item}
    return img_hash


def _image_placeholder(item: dict, img_hash: str) -> dict:
    """Build a text block that replaces a removed image."""
    src = item.get("source", {})
    media_type = src.get("media_type", "image")
    data_len = len(src.get("data", ""))
    raw_kb = (data_len * 3 // 4) / 1024
    return {
        "type": "text",
        "text": (
            f"[image removed — {media_type}, ~{raw_kb:.0f} KB. "
            f'Use MCP tool get_vt_object(hash="{img_hash}") to retrieve this image]'
        ),
    }


class ImageOptimizer(BaseOptimizer):
    """Strips images from older conversation turns to reduce context size.

    Removed images are stored by hash in the module-level vt_object_store
    and can be retrieved via the get_vt_object MCP tool.
    """

    def __init__(self, keep_turns: int = IMAGE_KEEP_TURNS):
        super().__init__(keep_turns=keep_turns)

    def _optimize(self, messages: list, threshold_msg_idx: int) -> int:
        tokens_removed = 0

        for i in range(threshold_msg_idx):
            msg = messages[i]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            new_content = []
            for item in content:
                if not isinstance(item, dict):
                    new_content.append(item)
                    continue

                if item.get("type") == "image":
                    tokens_removed += image_tokens(item)
                    img_hash = _store_image(item)
                    new_content.append(_image_placeholder(item, img_hash))

                elif item.get("type") == "tool_result":
                    rc = item.get("content")
                    if isinstance(rc, list):
                        new_rc = []
                        for b in rc:
                            if isinstance(b, dict) and b.get("type") == "image":
                                tokens_removed += image_tokens(b)
                                img_hash = _store_image(b)
                                new_rc.append(_image_placeholder(b, img_hash))
                            else:
                                new_rc.append(b)
                        item = dict(item)
                        item["content"] = new_rc
                    new_content.append(item)
                else:
                    new_content.append(item)

            messages[i] = dict(msg, content=new_content)

        return tokens_removed
