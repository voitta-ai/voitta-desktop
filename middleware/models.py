"""Data models for conversation tracking."""

import base64
import struct
from dataclasses import dataclass, field
from enum import Enum


class BlockType(Enum):
    USER_TEXT = "user_text"
    ASSISTANT_TEXT = "assistant_text"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MCP_TOOL_CALL = "mcp_tool_call"
    SERVER_TOOL_CALL = "server_tool_call"


def classify_tool(name: str) -> BlockType:
    """Classify a tool name into the appropriate BlockType."""
    if name.startswith("mcp__"):
        return BlockType.MCP_TOOL_CALL
    if name in ("web_search", "code_execution", "computer", "text_editor", "bash"):
        return BlockType.SERVER_TOOL_CALL
    return BlockType.TOOL_CALL


@dataclass
class ImageInfo:
    """Metadata for a single image in the conversation."""
    media_type: str
    base64_chars: int
    raw_bytes: int
    width: int = 0
    height: int = 0
    source_type: str = "base64"
    thumbnail_b64: str = ""


@dataclass
class ContentBlock:
    """A single content block in a conversation turn."""
    block_type: BlockType
    summary: str
    timestamp: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Turn:
    """A single request/response turn in a conversation."""
    index: int
    label: str
    blocks: list[ContentBlock] = field(default_factory=list)
    chars_in: int = 0
    chars_out: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    user_text_chars: int = 0
    tool_result_chars: int = 0
    assistant_text_chars: int = 0
    tool_call_chars: int = 0
    image_chars: int = 0
    stale_read_chars: int = 0
    thinking_chars: int = 0
    images: list[ImageInfo] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ToolGroup:
    """A group of tools with the same prefix."""
    prefix: str
    count: int
    total_chars: int
    tools: list[tuple[str, int]]


@dataclass
class BodyBreakdown:
    """Breakdown of request body sizes."""
    system_prompt_chars: int = 0
    system_blocks: list[tuple[str, int]] = field(default_factory=list)
    tools_chars: int = 0
    tools_count: int = 0
    tool_groups: list[ToolGroup] = field(default_factory=list)
    messages_chars: int = 0
    other_chars: int = 0
    total_chars: int = 0


@dataclass
class Conversation:
    """A tracked conversation (Claude Code session)."""
    id: str
    label: str
    fingerprint: str
    started_at: float
    last_active: float
    turns: list[Turn] = field(default_factory=list)
    breakdown: BodyBreakdown | None = None
    model: str = ""

    @property
    def request_count(self) -> int:
        return len(self.turns)

    @property
    def input_tokens(self) -> int:
        return sum(t.input_tokens for t in self.turns)

    @property
    def output_tokens(self) -> int:
        return sum(t.output_tokens for t in self.turns)

    @property
    def cache_read_input_tokens(self) -> int:
        return sum(t.cache_read_input_tokens for t in self.turns)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ── Image helpers ────────────────────────────────────────────────────────────

def parse_image_dimensions(raw: bytes, media_type: str) -> tuple[int, int]:
    """Extract width/height from raw image bytes by reading headers."""
    try:
        if len(raw) >= 24 and raw[:4] == b'\x89PNG':
            w = struct.unpack('>I', raw[16:20])[0]
            h = struct.unpack('>I', raw[20:24])[0]
            return w, h

        if len(raw) >= 4 and raw[0:2] == b'\xff\xd8':
            i = 2
            while i < len(raw) - 9:
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h = struct.unpack('>H', raw[i + 5:i + 7])[0]
                    w = struct.unpack('>H', raw[i + 7:i + 9])[0]
                    return w, h
                if 0xC0 <= marker <= 0xFE and marker not in (0xD0, 0xD1, 0xD2, 0xD3,
                        0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA):
                    seg_len = struct.unpack('>H', raw[i + 2:i + 4])[0]
                    i += 2 + seg_len
                else:
                    i += 2

        if len(raw) >= 10 and raw[:3] == b'GIF':
            w = struct.unpack('<H', raw[6:8])[0]
            h = struct.unpack('<H', raw[8:10])[0]
            return w, h

        if len(raw) >= 30 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
            if raw[12:16] == b'VP8 ' and len(raw) >= 30:
                w = struct.unpack('<H', raw[26:28])[0] & 0x3FFF
                h = struct.unpack('<H', raw[28:30])[0] & 0x3FFF
                return w, h
            if raw[12:16] == b'VP8L' and len(raw) >= 25:
                bits = struct.unpack('<I', raw[21:25])[0]
                w = (bits & 0x3FFF) + 1
                h = ((bits >> 14) & 0x3FFF) + 1
                return w, h
    except Exception:
        pass
    return 0, 0


def extract_image_info(item: dict) -> ImageInfo | None:
    """Extract ImageInfo from an image content block."""
    src = item.get("source", {})
    source_type = src.get("type", "base64")
    media_type = src.get("media_type", "image/png")

    if source_type == "base64":
        data_str = src.get("data", "")
        base64_chars = len(data_str)
        raw_bytes = (base64_chars * 3) // 4

        width, height = 0, 0
        thumbnail_b64 = ""
        try:
            decode_chars = min(base64_chars, 174763)
            header_b64 = data_str[:decode_chars]
            pad_needed = (4 - len(header_b64) % 4) % 4
            header_raw = base64.b64decode(header_b64 + '=' * pad_needed)
            width, height = parse_image_dimensions(header_raw, media_type)
        except Exception:
            pass

        if base64_chars > 0:
            try:
                import io
                from PIL import Image as PILImage
                full_data = base64.b64decode(data_str + '=' * ((4 - len(data_str) % 4) % 4))
                img = PILImage.open(io.BytesIO(full_data))
                if width == 0:
                    width, height = img.size
                thumb_w = min(200, img.width)
                thumb_h = int(img.height * thumb_w / max(img.width, 1))
                img = img.convert("RGB")
                img = img.resize((thumb_w, thumb_h))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=60)
                thumbnail_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception:
                thumbnail_b64 = ""

        return ImageInfo(
            media_type=media_type,
            base64_chars=base64_chars,
            raw_bytes=raw_bytes,
            width=width,
            height=height,
            source_type=source_type,
            thumbnail_b64=thumbnail_b64,
        )

    elif source_type == "url":
        return ImageInfo(
            media_type=media_type,
            base64_chars=0,
            raw_bytes=0,
            source_type="url",
        )

    return None
