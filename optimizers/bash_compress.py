"""BashCompressor — RTK-inspired output compression for Bash tool_results.

Operates on EVERY Bash tool_result the proxy sees, including the active turn.
Each filter assumes its transformation is semantically lossless (drops
formatting/noise, never data), so the output is replaced in place with no
recovery pointer — exactly as if RTK had run client-side before the result
ever reached the proxy.

Skipped:
  - Non-Bash tool_results.
  - Outputs already replaced by the older-turn BashOptimizer's
    "[bash output removed ...]" placeholder.
"""
from __future__ import annotations

import re

from middleware.parsing import find_tool_name, find_tool_input
from . import BaseOptimizer


# Marker the older-turn BashOptimizer leaves behind. We must not double-process.
_REMOVED_MARKER = "[bash output removed"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")
_CARRIAGE_RETURN_LINE_RE = re.compile(r"^.*\r(?=.+)", re.MULTILINE)

# Generic progress-noise patterns. Conservative — only drop lines that are
# clearly UI chatter, never anything that looks like data.
_PROGRESS_PATTERNS = [
    re.compile(r"^\s*\[#+\]\s*$"),                  # [###]
    re.compile(r"^\s*\[\s*=*>?\s*\]\s*\d*%?\s*$"),  # [===>      ] 47%
    re.compile(r"^\s*\d+(\.\d+)?\s*[MK]?B/s\s*$"),  # 1.2MB/s
    re.compile(r"^\s*\d+%\s+.*\d+(\.\d+)?\s*[MK]?B\s+\d+s\s*$"),  # download row
    re.compile(r"^Downloading\b.+\.{3,}\s*$", re.IGNORECASE),
    re.compile(r"^Resolving\b.+\.{3,}\s*$", re.IGNORECASE),
    re.compile(r"^Fetching\b.+\.{3,}\s*$", re.IGNORECASE),
    re.compile(r"^[⠁⠂⠄⡀⢀⠠⠐⠈]\s+.+", ),  # spinner chars from npm/yarn/etc
]


# ── Filters ──────────────────────────────────────────────────────────────────

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (color codes, cursor moves, OSC strings)."""
    cleaned = _ANSI_RE.sub("", text)
    # Carriage-return-overwritten lines: keep only the final state.
    cleaned = _CARRIAGE_RETURN_LINE_RE.sub("", cleaned)
    return cleaned


def _trim_whitespace(text: str) -> str:
    """Collapse runs of blank lines to one; strip trailing whitespace per line."""
    lines = [line.rstrip() for line in text.split("\n")]
    out = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        out.append(line)
    # Drop leading/trailing blank lines.
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _strip_progress(text: str) -> str:
    """Drop lines matching common download/install progress patterns."""
    out = []
    for line in text.split("\n"):
        if any(p.match(line) for p in _PROGRESS_PATTERNS):
            continue
        out.append(line)
    return "\n".join(out)


# ── Smart-command filters ────────────────────────────────────────────────────
#
# Each takes raw output, returns compressed output. They MUST be conservative:
# drop only lines that are textbook noise; never drop error markers, file paths,
# version numbers, or anything that could be data the LLM is reasoning about.

def _smart_git_status(text: str) -> str:
    out = []
    skip_patterns = [
        re.compile(r'^\s*\(use "git'),
        re.compile(r'^\s*\(commit or discard'),
        re.compile(r"^On branch "),
        re.compile(r"^Your branch is up to date"),
        re.compile(r"^nothing added to commit but untracked files"),
    ]
    for line in text.split("\n"):
        if any(p.match(line) for p in skip_patterns):
            continue
        out.append(line)
    return _trim_whitespace("\n".join(out))


def _smart_git_log(text: str) -> str:
    """Drop empty author/email lines; keep dates, hashes, subjects."""
    out = []
    skip = re.compile(r"^Author: .*<>$")  # only literally-empty emails
    for line in text.split("\n"):
        if skip.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def _smart_npm(text: str) -> str:
    skip_patterns = [
        re.compile(r"^npm warn deprecated", re.IGNORECASE),
        re.compile(r"^npm notice", re.IGNORECASE),
        re.compile(r"^added \d+ packages? in", re.IGNORECASE),
        re.compile(r"^changed \d+ packages? in", re.IGNORECASE),
        re.compile(r"^\s*\d+ packages? are looking for funding"),
        re.compile(r"^\s+run `npm fund`"),
        re.compile(r"^found \d+ vulnerabilities", re.IGNORECASE),  # the summary line
    ]
    out = []
    for line in text.split("\n"):
        if any(p.match(line) for p in skip_patterns):
            continue
        out.append(line)
    return _trim_whitespace("\n".join(out))


def _smart_pip(text: str) -> str:
    skip_patterns = [
        re.compile(r"^Collecting "),
        re.compile(r"^\s*Using cached "),
        re.compile(r"^\s*Downloading "),
        re.compile(r"^\s*Building wheel for "),
    ]
    out = []
    for line in text.split("\n"):
        if any(p.match(line) for p in skip_patterns):
            continue
        out.append(line)
    return _trim_whitespace("\n".join(out))


def _smart_pytest(text: str) -> str:
    """Keep summary, fail list, and any actual error/traceback. Drop verbose
    progress dots and per-test 'PASSED' rows."""
    out = []
    skip_passed = re.compile(r"^.+::.+ PASSED\s*\[\s*\d+%\]\s*$")
    for line in text.split("\n"):
        if skip_passed.match(line):
            continue
        out.append(line)
    return _trim_whitespace("\n".join(out))


def _smart_docker_ps(text: str) -> str:
    """Trim the long PORTS column to first port only — that's what the LLM
    usually needs. Conservative: only collapse when there are obvious commas."""
    out = []
    for line in text.split("\n"):
        if "->" in line and ", " in line:
            # "0.0.0.0:5432->5432/tcp, 0.0.0.0:5433->5433/tcp" → keep first
            parts = line.split(", ")
            if len(parts) > 1:
                line = parts[0] + " (+" + str(len(parts) - 1) + " more)"
        out.append(line)
    return "\n".join(out)


_SMART_HANDLERS = [
    # (regex matched against `command` from tool_use input, handler)
    (re.compile(r"^\s*git\s+status\b"), _smart_git_status),
    (re.compile(r"^\s*git\s+log\b"),    _smart_git_log),
    (re.compile(r"^\s*npm\b"),          _smart_npm),
    (re.compile(r"^\s*pnpm\b"),         _smart_npm),
    (re.compile(r"^\s*yarn\b"),         _smart_npm),
    (re.compile(r"^\s*pip\s+install\b"), _smart_pip),
    (re.compile(r"^\s*pip3\s+install\b"), _smart_pip),
    (re.compile(r"^\s*pytest\b"),       _smart_pytest),
    (re.compile(r"^\s*docker\s+ps\b"),  _smart_docker_ps),
]


def _smart_for(command: str) -> callable | None:
    for pat, handler in _SMART_HANDLERS:
        if pat.search(command):
            return handler
    return None


# ── Optimizer ────────────────────────────────────────────────────────────────


class BashCompressor(BaseOptimizer):
    """RTK-style filter chain for Bash tool outputs across all turns."""

    chart_key = "bash_compress"

    def __init__(
        self,
        strip_ansi: bool = True,
        trim_whitespace: bool = True,
        strip_progress: bool = False,
        smart_commands: bool = False,
    ):
        # keep_turns is unused — we override the threshold to "all messages".
        super().__init__(keep_turns=0)
        self.strip_ansi = strip_ansi
        self.trim_whitespace = trim_whitespace
        self.strip_progress = strip_progress
        self.smart_commands = smart_commands

    def _threshold_msg_index(self, messages: list) -> int | None:
        """Process every message, including the active turn."""
        return len(messages) if messages else None

    def _compress(self, text: str, command: str) -> str:
        """Apply the configured filter chain; return possibly-shorter text."""
        if self.strip_ansi:
            text = _strip_ansi(text)
        if self.trim_whitespace:
            text = _trim_whitespace(text)
        if self.strip_progress:
            text = _strip_progress(text)
        if self.smart_commands:
            handler = _smart_for(command)
            if handler is not None:
                try:
                    text = handler(text)
                except Exception:
                    # Smart filters must never break the request; fall back
                    # to whatever generic compression already produced.
                    pass
        return text

    def _optimize(self, messages: list, threshold_msg_idx: int) -> int:
        """Compress Bash tool_result content in messages[0..threshold_msg_idx)."""
        # If no filter is enabled there is nothing to do — the pipeline still
        # calls us, but we should not do work or claim savings.
        if not (self.strip_ansi or self.trim_whitespace
                or self.strip_progress or self.smart_commands):
            return 0

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
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    new_content.append(item)
                    continue

                tool_use_id = item.get("tool_use_id", "")
                tool_name = find_tool_name(messages, tool_use_id)
                if tool_name != "Bash":
                    new_content.append(item)
                    continue

                rc = item.get("content", "")
                if not isinstance(rc, str) or not rc:
                    new_content.append(item)
                    continue

                # Skip outputs already replaced by the older-turn BashOptimizer.
                if rc.startswith(_REMOVED_MARKER):
                    new_content.append(item)
                    continue

                command = ""
                if self.smart_commands:
                    inp = find_tool_input(messages, tool_use_id)
                    cmd = inp.get("command", "")
                    if isinstance(cmd, str):
                        command = cmd

                compressed = self._compress(rc, command)

                # Only swap if compression actually shrunk the text.
                saved_chars = len(rc) - len(compressed)
                if saved_chars <= 0:
                    new_content.append(item)
                    continue

                tokens_removed += saved_chars // 4
                self.last_stripped_ids[tool_use_id] = saved_chars

                new_content.append(dict(item, content=compressed))
                modified = True

            if modified:
                messages[i] = dict(msg, content=new_content)

        return tokens_removed
