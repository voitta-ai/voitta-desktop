"""FileAccessOptimizer — bidirectional invalidation of stale file operations.

For each file, later operations can invalidate earlier ones:

  - Write replaces the whole file -> invalidates ALL earlier ops
  - Full Read (no offset/limit) -> invalidates ALL earlier ops (supersedes)
  - Edit modifies part of the file -> invalidates ALL earlier reads
    (conservative: we can't easily determine which lines were affected)
  - Partial Read (with offset/limit) -> only invalidates earlier reads
    whose range overlaps

Reference: AgentDiet (2509.23586) identifies these as "redundant information"
in LLM agent trajectories.
"""

from . import BaseOptimizer

FILE_READ_KEEP_TURNS = 5

# All file-accessing tools whose results can be invalidated
_FILE_TOOLS = frozenset({"Read", "Write", "Edit"})


def _read_range(inp: dict) -> tuple[int, int]:
    """Extract (start_line, end_line) from a Read tool input.

    Returns (0, MAX) for full reads.  Lines are 1-based in the tool,
    but we use them only for overlap checks so exact semantics don't matter.
    """
    MAX = 10_000_000
    offset = inp.get("offset")
    limit = inp.get("limit")

    if offset is None and limit is None:
        return (0, MAX)

    # offset/limit may be ints, strings, or lists (e.g. [255, 360] from some clients)
    if isinstance(offset, list):
        start = int(offset[0]) if offset else 0
    else:
        start = int(offset) if offset is not None else 0
    if isinstance(limit, list):
        end = start + int(limit[0]) if limit else MAX
    elif limit is not None:
        end = start + int(limit)
    else:
        end = MAX

    return (start, end)


def _ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


class _FileOp:
    """A single file operation recorded during message scanning."""
    __slots__ = ("tool_id", "tool_name", "file_path", "msg_idx", "read_range")

    def __init__(self, tool_id: str, tool_name: str, file_path: str,
                 msg_idx: int, read_range: tuple[int, int] | None):
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.file_path = file_path
        self.msg_idx = msg_idx
        self.read_range = read_range


def _scan_ops(messages: list) -> list[_FileOp]:
    """Scan all messages and return file operations in order."""
    ops: list[_FileOp] = []
    for i, msg in enumerate(messages):
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = item.get("name", "")
            if name not in _FILE_TOOLS:
                continue
            inp = item.get("input", {})
            if not isinstance(inp, dict):
                continue
            fp = inp.get("file_path", "")
            if not fp:
                continue
            tool_id = item.get("id", "")
            if not tool_id:
                continue

            rng = None
            if name == "Read":
                rng = _read_range(inp)

            ops.append(_FileOp(tool_id, name, fp, i, rng))
    return ops


def _find_stale_ops(ops: list[_FileOp]) -> set[str]:
    """Return tool_use_ids of operations that are superseded by later ones.

    Invalidation rules (applied per file):
      - Write           -> invalidates ALL earlier ops on that file
      - Full Read       -> invalidates ALL earlier ops on that file
      - Edit            -> invalidates ALL earlier Reads on that file
      - Partial Read    -> invalidates earlier Reads with overlapping range
    """
    by_file: dict[str, list[_FileOp]] = {}
    for op in ops:
        by_file.setdefault(op.file_path, []).append(op)

    stale_ids: set[str] = set()

    for fp, file_ops in by_file.items():
        for i, op in enumerate(file_ops):
            for later in file_ops[i + 1:]:
                invalidated = False

                if later.tool_name == "Write":
                    invalidated = True

                elif later.tool_name == "Read" and later.read_range is not None:
                    MAX = 10_000_000
                    is_full_read = (later.read_range == (0, MAX))

                    if is_full_read:
                        invalidated = True
                    elif op.tool_name == "Read" and op.read_range is not None:
                        invalidated = _ranges_overlap(op.read_range, later.read_range)

                elif later.tool_name == "Edit":
                    if op.tool_name == "Read":
                        invalidated = True

                if invalidated:
                    stale_ids.add(op.tool_id)
                    break

    return stale_ids


class FileReadOptimizer(BaseOptimizer):
    """Replace stale file-access results in older turns with a short placeholder."""

    chart_key = "stale_read"

    def __init__(self, keep_turns: int = FILE_READ_KEEP_TURNS):
        super().__init__(keep_turns=keep_turns)

    def _optimize(self, messages: list, threshold_msg_idx: int) -> int:
        ops = _scan_ops(messages)
        stale_ids = _find_stale_ops(ops)

        if not stale_ids:
            return 0

        op_lookup = {op.tool_id: (op.tool_name, op.file_path) for op in ops}

        tokens_removed = 0

        for i in range(threshold_msg_idx):
            msg = messages[i]
            if msg.get("role") != "user":
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

                if item.get("type") == "tool_result":
                    tool_use_id = item.get("tool_use_id", "")
                    if tool_use_id in stale_ids:
                        tool_name, fp = op_lookup[tool_use_id]
                        old_chars = _content_chars(item.get("content", ""))
                        placeholder = f"[stale file {tool_name.lower()} — {fp} was modified in a later turn]"
                        saved_chars = max(0, old_chars - len(placeholder))
                        if saved_chars > 0:
                            new_item = dict(item, content=placeholder)
                            new_content.append(new_item)
                            tokens_removed += int(saved_chars / 3.5)
                            modified = True
                            continue

                new_content.append(item)

            if modified:
                messages[i] = dict(msg, content=new_content)

        return tokens_removed


def analyze_stale_reads(messages: list) -> dict[str, int]:
    """Analyze messages and return {tool_use_id: chars} for stale file operations.

    Read-only analysis — messages are not modified.
    """
    ops = _scan_ops(messages)
    stale_ids = _find_stale_ops(ops)

    stale: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            tool_use_id = item.get("tool_use_id", "")
            if tool_use_id in stale_ids:
                stale[tool_use_id] = _content_chars(item.get("content", ""))

    return stale


def _content_chars(content) -> int:
    """Count characters in a tool_result content field."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                total += len(b.get("text", ""))
        return total
    return 0
