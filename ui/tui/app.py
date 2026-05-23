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


_BAR_H = 8   # rows per metric section


def _section(turns: list, get_val, color: str, label: str) -> list[str]:
    """Render one bar-chart section: _BAR_H rows tall, one col per turn."""
    vals = [get_val(t) for t in turns]
    mx = max(vals, default=0)
    rows = []
    for r in range(_BAR_H):
        cells = []
        for v in vals:
            bar_h = round(v / mx * _BAR_H) if mx else 0
            if r >= _BAR_H - bar_h:
                cells.append(f"[{color}]█[/{color}]")
            else:
                cells.append("[dim]·[/dim]")
        prefix = f"[bold {color}]{label}[/bold {color}]│" if r == 0 else f"{'':>{len(label)}}│"
        rows.append(prefix + "".join(cells))
    return rows


def _render_turn_chart(conv) -> str:
    turns = conv.turns
    if not turns:
        return "[dim]no turns yet[/dim]"

    label_w = 3  # "in " / "cr " / "out"

    in_sec  = _section(turns, lambda t: t.input_tokens,            "blue",   "in ")
    cr_sec  = _section(turns, lambda t: t.cache_read_input_tokens, "green",  "cr ")
    out_sec = _section(turns, lambda t: t.output_tokens,           "yellow", "out")

    sep = f"{'':>{label_w}}┼" + "─" * len(turns)

    # x-axis: turn number every 5 turns, last-digit only
    x_axis = f"{'':>{label_w}}│" + "".join(
        str((t.index + 1) % 10) if (t.index + 1) % 5 == 0 else " "
        for t in turns
    )

    # token totals for last turn
    last = turns[-1]
    last_stats = (
        f"  [dim]last turn:[/dim] "
        f"[blue]in={last.input_tokens:,}[/blue]  "
        f"[green]cr={last.cache_read_input_tokens:,}[/green]  "
        f"[yellow]out={last.output_tokens:,}[/yellow]"
    )
    header = (
        f"[bold]{conv.label}[/bold]  "
        f"model=[italic]{conv.model or '?'}[/italic]  "
        f"requests={conv.request_count}  turns={len(turns)}"
        + last_stats
    )
    legend = (
        f"{'':>{label_w}}  "
        "[blue]█[/blue] input  "
        "[green]█[/green] cache-read  "
        "[yellow]█[/yellow] output"
    )

    lines = [header, legend, sep]
    lines += in_sec
    lines.append(sep)
    lines += cr_sec
    lines.append(sep)
    lines += out_sec
    lines += [sep, x_axis]
    return "\n".join(lines)


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
        # autoscroll to the right so the latest turn is always visible
        self.scroll_end(animate=False)


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
