"""Terminal UI driver — Textual-based replacement for the Mac menu bar app.

Layout:
  ┌────────────────────────────────────────────────────┐
  │ Conversations (sidebar) │ Conversation detail       │
  ├────────────────────────────────────────────────────┤
  │ Status bar: savings · cache · arm toggle · backends│
  └────────────────────────────────────────────────────┘

Run via:  python -m voitta_desktop --terminal
"""

from __future__ import annotations

import io
import logging
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer
from textual.message import Message
from textual.widgets import Footer, Header, Label, ListItem, ListView, RichLog, Static

from app_base import AppBase

if TYPE_CHECKING:
    pass

logger = logging.getLogger("voitta-desktop.tui")


# ── Messages (posted across thread boundary) ─────────────────────────────────

class ConversationsUpdated(Message):
    """Posted from proxy threads to trigger UI refresh."""


# ── Widgets ──────────────────────────────────────────────────────────────────

class ConvList(ListView):
    """Left sidebar: sorted list of active conversations."""

    def update(self, convs: list) -> None:
        self.clear()
        for conv in convs:
            tokens = conv.total_tokens
            tok_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)
            cache_pct = 0
            total_in = conv.input_tokens + conv.cache_read_input_tokens
            if total_in:
                cache_pct = conv.cache_read_input_tokens * 100 // total_in
            cache_str = f" {cache_pct}%" if cache_pct else ""
            label = f"{conv.label[:38]}  [{tok_str}{cache_str}] ×{conv.request_count}"
            # No id= — Textual raises DuplicateIds when rapid updates re-append
            # items before clear() has fully committed. _conv_id tracks selection.
            item = ListItem(Label(label))
            item._conv_id = conv.id
            self.append(item)


_SECTION_H = 4   # character rows per section (× 4 braille dots = 16 levels)

# Braille dot layout: dot_col (0=left,1=right), dot_row (0=top..3=bottom) → bit
_BDOTS = [
    [0x01, 0x02, 0x04, 0x08],  # col 0
    [0x10, 0x20, 0x40, 0x80],  # col 1
]
_B0 = 0x2800


import math as _math


def _log_norm(vals: list[int | float], res: int) -> list[int]:
    """Normalize values to 0..(res-1) on a log scale. 0 stays 0."""
    mx = max(vals, default=0)
    if mx <= 0:
        return [0] * len(vals)
    log_mx = _math.log1p(mx)
    return [round(_math.log1p(v) / log_mx * (res - 1)) for v in vals]


def _set_dot(grid, h, dc, dot_y):
    """Set a single braille dot at dot-column dc, absolute dot-row dot_y (0=top)."""
    cr = dot_y // 4
    dr = dot_y % 4
    ci = dc // 2   # char col = turn_index // 2, but here dc IS already the turn index
    # caller passes (grid, h, turn_index, dot_y_from_top)
    # repurpose: dc=turn_index
    ci = dc // 2
    dcol = dc % 2
    if 0 <= cr < h:
        grid[cr][ci] |= _BDOTS[dcol][dr]


def _braille_line(vals: list[int | float], h: int, color: str, label: str) -> list[str]:
    """Braille line chart: log scale, full interpolation between every pair of turns."""
    n = len(vals)
    if n == 0:
        return [f"[bold {color}]{label}[/bold {color}]│"]
    mx = max(vals, default=0)
    res = h * 4   # total dot rows, 0=top res-1=bottom → we invert

    norm = _log_norm(vals, res)  # 0=bottom, res-1=top

    ncols = (n + 1) // 2
    grid = [[0] * ncols for _ in range(h)]

    def place(turn_idx: int, y: int):
        """Place dot for turn turn_idx at normalized height y (0=bottom)."""
        dot_from_top = (res - 1) - y
        ci = turn_idx // 2
        dcol = turn_idx % 2
        cr = dot_from_top // 4
        dr = dot_from_top % 4
        if 0 <= cr < h and 0 <= ci < ncols:
            grid[cr][ci] |= _BDOTS[dcol][dr]

    # Place each point and interpolate between consecutive turns
    for i, y in enumerate(norm):
        place(i, y)
        if i > 0:
            y0, y1 = norm[i - 1], y
            lo, hi = sorted([y0, y1])
            # For each intermediate level, assign to the turn whose column is closer
            for fy in range(lo + 1, hi):
                frac = (fy - y0) / (y1 - y0) if y1 != y0 else 0.5
                ti = i - 1 if frac < 0.5 else i
                place(ti, fy)

    lw = len(label)
    rows = []
    for r in range(h):
        line = "".join(chr(_B0 | grid[r][ci]) for ci in range(ncols))
        prefix = f"[bold {color}]{label}[/bold {color}]│" if r == 0 else f"{'':>{lw}}│"
        if r == 0 and mx >= 1000:
            suffix = f"  [dim]max {mx//1000}k[/dim]"
        elif r == 0 and mx:
            suffix = f"  [dim]max {mx}[/dim]"
        else:
            suffix = ""
        rows.append(f"{prefix}[{color}]{line}[/{color}]{suffix}")
    return rows


def _render_turn_chart(conv) -> str:
    turns = conv.turns
    if not turns:
        return "[dim]no turns yet[/dim]"

    pre_vals  = [t.input_tokens + t.cache_read_input_tokens + t.stripped_chars for t in turns]
    post_vals = [t.input_tokens + t.cache_read_input_tokens                    for t in turns]
    cr_vals   = [t.cache_read_input_tokens for t in turns]
    out_vals  = [t.output_tokens           for t in turns]
    hit_vals  = [
        int(t.cache_read_input_tokens * 100 / (t.input_tokens + t.cache_read_input_tokens))
        if (t.input_tokens + t.cache_read_input_tokens) else 0
        for t in turns
    ]

    n   = len(turns)
    lw  = 5
    cw  = (n + 1) // 2   # braille chars wide (2 turns per char)
    sep = f"{'':>{lw}}┼" + "─" * cw
    # x-axis: each braille char = turns i*2 and i*2+1
    # show label at every 5th turn; use last digit of turn number
    def _xlabel(i):
        t0 = i * 2 + 1   # turn number (1-based) of left dot-col
        t1 = i * 2 + 2
        if t0 % 10 == 0: return str(t0 // 10 % 10) if t0 >= 10 else " "
        if t0 % 5 == 0:  return str(t0 % 10)
        if t1 % 5 == 0:  return str(t1 % 10)
        return " "
    x_axis = f"{'':>{lw}}│" + "".join(_xlabel(i) for i in range(cw))

    pre_sec  = _braille_line(pre_vals,  _SECTION_H, "red",    "pre  ")
    post_sec = _braille_line(post_vals, _SECTION_H, "blue",   "post ")
    cr_sec   = _braille_line(cr_vals,   _SECTION_H, "green",  "cr   ")
    out_sec  = _braille_line(out_vals,  _SECTION_H, "yellow", "out  ")
    hit_sec  = _braille_line(hit_vals,  _SECTION_H, "cyan",   "hit% ")

    last = turns[-1]
    last_pre = last.input_tokens + last.cache_read_input_tokens + last.stripped_chars
    last_post = last.input_tokens + last.cache_read_input_tokens
    savings_pct = int(last.stripped_chars * 100 / last_pre) if last_pre else 0
    last_hit = hit_vals[-1]

    header = (
        f"[bold]{conv.label}[/bold]  "
        f"[dim]{conv.model or '?'}[/dim]  "
        f"turns={len(turns)}  requests={conv.request_count}"
    )
    last_line = (
        f"[dim]last:[/dim]  "
        f"[red]pre={last_pre:,}[/red]  "
        f"[blue]post={last_post:,}[/blue]  "
        f"[green]cr={last.cache_read_input_tokens:,}[/green]  "
        f"[yellow]out={last.output_tokens:,}[/yellow]  "
        f"[cyan]hit={last_hit}%[/cyan]  "
        f"[magenta]saved={savings_pct}%[/magenta]"
    )
    legend = (
        f"{'':>{lw}}  "
        "[red]⠿[/red] pre-opt  "
        "[blue]⠿[/blue] post-opt  "
        "[green]⠿[/green] cache-read  "
        "[yellow]⠿[/yellow] output  "
        "[cyan]⠿[/cyan] cache-hit%"
    )

    lines = [header, last_line, legend, sep]
    lines += pre_sec;  lines.append(sep)
    lines += post_sec; lines.append(sep)
    lines += cr_sec;   lines.append(sep)
    lines += out_sec;  lines.append(sep)
    lines += hit_sec;  lines += [sep, x_axis]

    lines.append("")
    lines += _render_breakdown(conv)

    return "\n".join(lines)


def _k(v: int) -> str:
    if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
    if v >= 1_000:     return f"{v//1_000}k"
    return str(v)


def _render_breakdown(conv) -> list[str]:
    bd = conv.breakdown
    lines = []

    # ── Context composition ──────────────────────────────────────────
    if bd:
        sys_k  = _k(bd.system_prompt_chars)
        tool_k = _k(bd.tools_chars)
        other_k = _k(bd.other_chars)
        lines.append(
            f"[dim]context:[/dim]  "
            f"[blue]system {sys_k}[/blue]  "
            f"[yellow]tools {tool_k} ({bd.tools_count})[/yellow]  "
            f"[dim]other {other_k}[/dim]"
        )

        # Per-tool-group breakdown (top 6)
        if bd.tool_groups:
            groups = sorted(bd.tool_groups, key=lambda g: g.total_chars, reverse=True)[:6]
            row = "  [dim]tools:[/dim]  " + "  ".join(
                f"[yellow]{g.prefix}[/yellow][dim]×{g.count} {_k(g.total_chars)}[/dim]"
                for g in groups
            )
            lines.append(row)

        # System blocks (top 3)
        if bd.system_blocks:
            for preview, chars in bd.system_blocks[:3]:
                lines.append(f"  [dim]sys·[/dim] [blue]{preview[:60]}[/blue] [dim]{_k(chars)}[/dim]")

    # ── Last-turn content breakdown ───────────────────────────────────
    if conv.turns:
        last = conv.turns[-1]
        parts = []
        if last.user_text_chars:    parts.append(f"[green]user {_k(last.user_text_chars)}[/green]")
        if last.tool_result_chars:  parts.append(f"[cyan]results {_k(last.tool_result_chars)}[/cyan]")
        if last.bash_chars:         parts.append(f"[dim]bash {_k(last.bash_chars)}[/dim]")
        if last.thinking_chars:     parts.append(f"[magenta]think {_k(last.thinking_chars)}[/magenta]")
        if last.assistant_text_chars: parts.append(f"[white]asst {_k(last.assistant_text_chars)}[/white]")
        if last.tool_call_chars:    parts.append(f"[yellow]calls {_k(last.tool_call_chars)}[/yellow]")
        if last.images:             parts.append(f"[dim]imgs ×{len(last.images)}[/dim]")
        if parts:
            lines.append(f"[dim]last turn:[/dim]  " + "  ".join(parts))

    # ── Cumulative savings ────────────────────────────────────────────
    total_in = sum(t.input_tokens + t.cache_read_input_tokens for t in conv.turns)
    total_cr = sum(t.cache_read_input_tokens for t in conv.turns)
    total_stripped = sum(t.stripped_chars for t in conv.turns)
    cache_pct = int(total_cr * 100 / total_in) if total_in else 0
    saved_pct = int(total_stripped * 100 / (total_in + total_stripped)) if (total_in + total_stripped) else 0
    lines.append(
        f"[dim]totals:[/dim]  "
        f"[green]cache {cache_pct}%[/green]  "
        f"[magenta]optimizer saved {saved_pct}%[/magenta]  "
        f"[dim]in {_k(total_in)}  cr {_k(total_cr)}[/dim]"
    )

    return lines


class ConvDetail(ScrollableContainer):
    """Right panel: horizontal-scrolling bar chart of turns."""

    def compose(self) -> ComposeResult:
        yield Static(id="detail-inner")

    def show(self, conv) -> None:
        inner = self.query_one("#detail-inner", Static)
        if conv is None:
            inner.update("No conversation selected.")
            return
        inner.update(_render_turn_chart(conv))
        self.call_after_refresh(self._scroll_to_right)

    def _scroll_to_right(self) -> None:
        self.scroll_x = self.max_scroll_x


class StatusBar(Static):
    """Bottom status line: savings, cache hit rate, arm status, backends."""

    def refresh_stats(self, app_ref: "TUIApp") -> None:
        savings = getattr(app_ref._optimizer_pipeline, "total_savings_usd", 0) or 0
        convs = list(app_ref._tracker.conversations.values())
        total_in = sum(c.input_tokens + c.cache_read_input_tokens for c in convs)
        total_cr = sum(c.cache_read_input_tokens for c in convs)
        cache_pct = int(total_cr * 100 / total_in) if total_in else 0
        # Read ground truth from ~/.claude/settings.json, not the stale flag.
        from claude_link import is_voitta_connected, load_claude_settings
        actually_armed = is_voitta_connected(load_claude_settings(), app_ref.llm_proxy_port)
        app_ref.claude_link_armed = actually_armed  # keep flag in sync
        arm = "[green]Armed[/green]" if actually_armed else "[dim]Disarmed[/dim]"
        backends = len([
            s for s in app_ref.mcp_servers
            if (s.get("prefix") or "").strip()
            and (app_ref._mcp_tools.get((s.get("prefix") or "").strip()) or [])
        ])
        self.update(
            f"  💰 ${savings:.3f} saved  "
            f"📊 {cache_pct}% cache  "
            f"🔗 {arm}  "
            f"⚙ {backends} backend(s) active  "
            f"  [dim]^C quit  ^D arm/disarm  ^R refresh  ^L log[/dim]"
        )


class LogPanel(RichLog):
    """Collapsible log panel — shows captured stdout/stderr/logging output."""
    pass


# ── stdout/stderr → log file + optional TUI panel ────────────────────────────

_LOG_PATH = Path.home() / ".voitta-desktop" / "logs" / "desktop.log"


class _TUILogHandler(logging.Handler):
    """Forwards log records to the Textual LogPanel when it's visible."""

    def __init__(self):
        super().__init__()
        self._panel: LogPanel | None = None

    def set_panel(self, panel: LogPanel) -> None:
        self._panel = panel

    def emit(self, record: logging.LogRecord) -> None:
        if self._panel is None:
            return
        try:
            msg = self.format(record)
            self._panel.app.call_from_thread(self._panel.write, msg)
        except Exception:
            pass


_tui_log_handler = _TUILogHandler()


def _redirect_stdio(log_path: Path) -> None:
    """Redirect stdout and stderr to the log file so they don't bleed into the TUI."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file


# ── Main Textual app ─────────────────────────────────────────────────────────

class TUIApp(App, AppBase):
    """Voitta Desktop — terminal mode."""

    terminal_mode = True

    CSS = """
    ConvList {
        width: 40;
        border-right: solid $primary-darken-2;
    }
    ConvDetail {
        width: 1fr;
        padding: 1 2;
        overflow-x: scroll;
        overflow-y: hidden;
    }
    #detail-inner {
        width: auto;
    }
    StatusBar {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
    }
    LogPanel {
        height: 12;
        border-top: solid $warning;
        background: $surface-darken-2;
        display: none;
    }
    LogPanel.visible {
        display: block;
    }
    """

    BINDINGS = [
        # ctrl+c / ctrl+q are Textual built-ins for quit — no custom q binding
        # to avoid accidental exits when navigating the conversation list.
        # ctrl+a is screen's prefix — use ctrl+d (arm/disarm) instead.
        # ctrl+b is tmux's prefix — avoid entirely.
        # ctrl+r has no multiplexer meaning in this context.
        Binding("ctrl+d", "toggle_arm", "Arm/Disarm"),
        Binding("ctrl+r", "refresh_tools", "Refresh tools"),
        Binding("ctrl+l", "toggle_log", "Log"),
    ]

    def __init__(self):
        App.__init__(self)
        _redirect_stdio(_LOG_PATH)
        self._init_base()
        self.mcp_proxy_port = self._resolve_port_terminal("MCP proxy", self.mcp_proxy_port)
        self.llm_proxy_port = self._resolve_port_terminal("LLM proxy", self.llm_proxy_port)
        self._build_proxy_stack()
        self._selected_conv_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            yield ConvList(id="conv-list")
            yield ConvDetail(id="conv-detail")
        yield LogPanel(id="log-panel", max_lines=500, markup=False)
        yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Voitta Desktop"
        self.sub_title = (
            f"LLM :{self.llm_proxy_port}  MCP :{self.mcp_proxy_port}"
        )
        # Wire log handler to the panel
        panel = self.query_one("#log-panel", LogPanel)
        _tui_log_handler.set_panel(panel)
        _tui_log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        )
        logging.getLogger().addHandler(_tui_log_handler)

        threading.Thread(target=self._run_llm_proxy, daemon=True).start()
        threading.Thread(target=self._run_mcp_proxy, daemon=True).start()
        self._refresh_ui()

    # ── Incoming event from proxy threads ───────────────────────────

    def notify_update(self) -> None:
        """Called from background threads — schedule a UI refresh."""
        self.post_message(ConversationsUpdated())

    def on_conversations_updated(self, _: ConversationsUpdated) -> None:
        self._refresh_ui()

    # ── UI refresh ───────────────────────────────────────────────────

    def _refresh_ui(self) -> None:
        convs = sorted(
            self._tracker.conversations.values(),
            key=lambda c: c.last_active,
            reverse=True,
        )
        conv_list = self.query_one("#conv-list", ConvList)
        conv_list.update(convs)

        selected = next(
            (c for c in convs if c.id == self._selected_conv_id), None
        ) or (convs[0] if convs else None)
        if selected:
            self._selected_conv_id = selected.id
        self.query_one("#conv-detail", ConvDetail).show(selected)
        self.query_one("#status-bar", StatusBar).refresh_stats(self)

    # ── List selection ───────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        conv_id = getattr(event.item, "_conv_id", None)
        if conv_id:
            self._selected_conv_id = conv_id
            conv = self._tracker.conversations.get(conv_id)
            self.query_one("#conv-detail", ConvDetail).show(conv)

    # ── Actions ─────────────────────────────────────────────────────

    def action_toggle_arm(self) -> None:
        from claude_link import arm_claude_link, disarm_claude_link
        if self.claude_link_armed:
            disarm_claude_link()
            self.claude_link_armed = False
            self._config.setdefault("claude_link", {})["armed"] = False
        else:
            arm_claude_link(self.llm_proxy_port)
            self.claude_link_armed = True
            self._config.setdefault("claude_link", {})["armed"] = True
        self._save_config()
        self.query_one("#status-bar", StatusBar).refresh_stats(self)

    def action_toggle_log(self) -> None:
        panel = self.query_one("#log-panel", LogPanel)
        panel.toggle_class("visible")

    def action_refresh_tools(self) -> None:
        import asyncio
        backends = getattr(self, "_mcp_backends", [])
        if not backends:
            self.notify("No backends mounted yet.", severity="warning")
            return

        async def _do_refresh():
            results = []
            for name, _, proxy in backends:
                ok, count, err = await proxy.force_refresh()
                results.append(f"{name}: {'✓ ' + str(count) + ' tools' if ok else '✗ ' + (err or 'failed')}")
            self.notify("\n".join(results), title="Tool refresh")

        asyncio.create_task(_do_refresh())
