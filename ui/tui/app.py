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

import logging
import threading
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

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
            item = ListItem(Label(label), id=f"conv-{conv.id}")
            item._conv_id = conv.id
            self.append(item)


class ConvDetail(Static):
    """Right panel: turn-by-turn view of the selected conversation."""

    def show(self, conv) -> None:
        if conv is None:
            self.update("No conversation selected.")
            return

        lines = [f"[bold]{conv.label}[/bold]  model={conv.model or '?'}  "
                 f"requests={conv.request_count}\n"]

        for turn in conv.turns:
            tok = f"in={turn.input_tokens} cr={turn.cache_read_input_tokens} out={turn.output_tokens}"
            lines.append(f"[dim]── Turn {turn.index + 1}  {tok}[/dim]")
            for block in turn.blocks:
                btype = getattr(block, "type", "?")
                if btype == "text":
                    text = (block.text or "").strip()
                    lines.append(f"  [green]text[/green]  {text[:120]}")
                elif btype == "tool_use":
                    lines.append(f"  [yellow]tool[/yellow]  {block.name}")
                elif btype == "tool_result":
                    lines.append(f"  [blue]result[/blue]  (id={block.tool_use_id})")
                elif btype == "thinking":
                    lines.append(f"  [magenta]think[/magenta]  {(block.thinking or '')[:80]}")
        self.update("\n".join(lines))


class StatusBar(Static):
    """Bottom status line: savings, cache hit rate, arm status, backends."""

    def refresh_stats(self, app_ref: "TUIApp") -> None:
        savings = getattr(app_ref._optimizer_pipeline, "total_savings_usd", 0) or 0
        convs = list(app_ref._tracker.conversations.values())
        total_in = sum(c.input_tokens + c.cache_read_input_tokens for c in convs)
        total_cr = sum(c.cache_read_input_tokens for c in convs)
        cache_pct = int(total_cr * 100 / total_in) if total_in else 0
        arm = "[green]Armed[/green]" if app_ref.claude_link_armed else "[dim]Disarmed[/dim]"
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
            f"  [dim]^C quit  ^D arm/disarm  ^R refresh tools[/dim]"
        )


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
        overflow-y: auto;
    }
    StatusBar {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
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
    ]

    def __init__(self):
        App.__init__(self)
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
        yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Voitta Desktop"
        self.sub_title = (
            f"LLM :{self.llm_proxy_port}  MCP :{self.mcp_proxy_port}"
        )
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
