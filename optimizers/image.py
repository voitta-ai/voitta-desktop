"""ImageOptimizer — strips images from older conversation turns, caches them for retrieval."""

import hashlib

from . import BaseOptimizer, image_tokens

IMAGE_KEEP_TURNS = 5

# Tool definition injected when cached images exist
IMAGE_RETRIEVAL_TOOL = {
    "name": "voitta_proxy_get_image",
    "description": (
        "Retrieve a previously seen image that was removed from context to save tokens. "
        "Call this when you need to re-examine an image referenced by its hash."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "image_hash": {
                "type": "string",
                "description": "The hash identifier of the image to retrieve.",
            }
        },
        "required": ["image_hash"],
    },
}


def _image_hash(item: dict) -> str:
    """Compute a short hash from image data."""
    data = item.get("source", {}).get("data", "")
    return hashlib.sha256(data[:4096].encode()).hexdigest()[:12]


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
            f'To see this image, call voitta_proxy_get_image(image_hash="{img_hash}")]'
        ),
    }


class ImageOptimizer(BaseOptimizer):
    """Strips images from older conversation turns to reduce context size.

    Removed images are cached by hash and can be retrieved via
    the voitta_proxy_get_image tool.
    """

    def __init__(self, keep_turns: int = IMAGE_KEEP_TURNS):
        super().__init__(keep_turns=keep_turns)
        self.image_cache: dict[str, dict] = {}

    def get_cached_image(self, img_hash: str) -> dict | None:
        """Return the original image block for the given hash, or None."""
        return self.image_cache.get(img_hash)

    @property
    def has_cached_images(self) -> bool:
        return bool(self.image_cache)

    def _cache_image(self, item: dict) -> str:
        """Store an image block and return its hash."""
        img_hash = _image_hash(item)
        self.image_cache[img_hash] = item
        return img_hash

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
                    img_hash = self._cache_image(item)
                    new_content.append(_image_placeholder(item, img_hash))

                elif item.get("type") == "tool_result":
                    rc = item.get("content")
                    if isinstance(rc, list):
                        new_rc = []
                        for b in rc:
                            if isinstance(b, dict) and b.get("type") == "image":
                                tokens_removed += image_tokens(b)
                                img_hash = self._cache_image(b)
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
