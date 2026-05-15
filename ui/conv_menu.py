"""Conversation menu entries + per-conversation chart popup.

Extracted from menu.py as a mixin. Owns ``_update_conversations``,
``_build_conv_item``, and ``_show_conv_popup``. Reaches the host via
``self._tracker``, ``self._optimizer_pipeline``, ``self._cache_sim``,
``self._conv_menus``, ``self._conv_header``, ``self._fmt_tokens``,
and the rumps ``self.menu``.

Each conversation is a single top-level menu item whose click opens the
chart popup — no submenu drilldown.
"""
from __future__ import annotations

import rumps
from AppKit import (
    NSApp, NSBackingStoreBuffered, NSFloatingWindowLevel, NSScreen, NSWindow,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
)
from Foundation import NSMakeRect
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore

from ui.chart import generate_chart_html


class ConvMenuMixin:
    """Mixin: conversation menu entries + chart popup."""

    def _update_conversations(self):
        convs = self._tracker.get_conversations_sorted()

        stale_ids = set(self._conv_menus.keys()) - {c.id for c in convs}
        for cid in stale_ids:
            try:
                del self.menu[self._conv_menus[cid].title]
            except KeyError:
                pass
            del self._conv_menus[cid]

        if not convs or not any(c.turns for c in convs):
            return

        prev = self._conv_header.title
        for conv in [c for c in convs[:20] if c.turns]:
            tokens = self._fmt_tokens(conv.total_tokens)
            cache_info = ""
            if conv.cache_read_input_tokens > 0:
                total_in = conv.input_tokens + conv.cache_read_input_tokens
                cache_pct = (conv.cache_read_input_tokens * 100) // max(total_in, 1)
                cache_info = f" cache:{cache_pct}%"

            title = f"{conv.label}  [{tokens}{cache_info}] ×{conv.request_count}"

            if conv.id in self._conv_menus:
                old_item = self._conv_menus[conv.id]
                if old_item.title != title:
                    try:
                        del self.menu[old_item.title]
                    except KeyError:
                        pass
                    new_item = self._build_conv_item(conv, title)
                    self._conv_menus[conv.id] = new_item
                    self.menu.insert_after(prev, new_item)
            else:
                new_item = self._build_conv_item(conv, title)
                self._conv_menus[conv.id] = new_item
                self.menu.insert_after(prev, new_item)

            prev = self._conv_menus[conv.id].title

    def _build_conv_item(self, conv, title: str) -> rumps.MenuItem:
        """Build a flat conversation menu entry: clicking opens the chart popup,
        no submenu children. rumps fires the callback only when a MenuItem has
        no children, which is exactly the behaviour we want here."""
        item = rumps.MenuItem(title, callback=self._show_conv_popup)
        item._conv_label = conv.label
        item._conv_id = conv.id
        return item

    def _show_conv_popup(self, sender):
        conv_label = getattr(sender, '_conv_label', 'Conversation')
        conv_id = getattr(sender, '_conv_id', None)

        breakdown_data = {"system": 0, "tools": 0, "other": 0}
        turns_data = []
        if conv_id:
            conv = self._tracker.get_conversation(conv_id)
            if conv:
                bd = conv.breakdown
                if bd:
                    breakdown_data = {
                        "system": bd.system_prompt_chars,
                        "tools": bd.tools_chars,
                        "other": bd.other_chars,
                        "tools_count": bd.tools_count,
                        "tool_groups": [
                            {"prefix": g.prefix, "count": g.count, "chars": g.total_chars}
                            for g in bd.tool_groups[:8]
                        ],
                        "system_blocks": [
                            {"preview": p[:60], "chars": c}
                            for p, c in bd.system_blocks[:5]
                        ],
                    }
                stripped_ids = self._optimizer_pipeline.stripped_tool_ids
                stripped_msgs = self._optimizer_pipeline.stripped_msg_indices
                for t in conv.turns:
                    images_data = []
                    for img in t.images:
                        token_chars = int(img.width * img.height / 750 * 3.5)
                        images_data.append({
                            "media_type": img.media_type,
                            "base64_chars": img.base64_chars,
                            "raw_bytes": img.raw_bytes,
                            "width": img.width, "height": img.height,
                            "source_type": img.source_type,
                            "thumbnail": img.thumbnail_b64 if img.thumbnail_b64 else "",
                            "token_chars": token_chars,
                        })
                    blocks_data = [
                        {"type": b.block_type.value, "summary": b.summary[:100]}
                        for b in t.blocks
                    ]
                    # Compute per-turn stripped chars from optimizer data
                    stripped_tool = sum(
                        stripped_ids.get(tid, 0) for tid in t.tool_use_ids
                    )
                    stripped_think = sum(
                        stripped_msgs.get(mi, 0)
                        for mi in range(t._msg_range[0], t._msg_range[1])
                    )
                    turns_data.append({
                        "index": t.index, "label": t.label[:30],
                        "user_text": t.user_text_chars,
                        "tool_result": t.tool_result_chars,
                        "assistant_text": t.assistant_text_chars,
                        "tool_call": t.tool_call_chars,
                        "image": sum(int(img.width * img.height / 750 * 3.5) for img in t.images),
                        "bash": t.bash_chars,
                        "thinking": t.thinking_chars,
                        "stripped_tool": stripped_tool,
                        "stripped_thinking": stripped_think,
                        "images": images_data,
                        "blocks": blocks_data,
                        "input_tokens": t.input_tokens,
                        "output_tokens": t.output_tokens,
                        "cache_read_input_tokens": t.cache_read_input_tokens,
                        "cache_creation_input_tokens": t.cache_creation_input_tokens,
                        "cache_control_types": t.cache_control_types,
                        "msg_count": t._msg_range[1] - t._msg_range[0],
                        "file_ops": [
                            {
                                "tool": op.tool_name,
                                "file": op.file_path,
                                "start": op.start_line,
                                "end": op.end_line,
                                "old_len": op.old_str_len,
                                "new_len": op.new_str_len,
                                "content_len": op.content_len,
                            }
                            for op in t.file_ops
                        ],
                    })

        active = self._optimizer_pipeline.active_optimizers
        cache_history = self._cache_sim.get_history(conv_id) if conv_id else []
        # Align cache data from the end (cache resets on restart, turns don't)
        ch_offset = len(turns_data) - len(cache_history)
        for i, td in enumerate(turns_data):
            ci = i - ch_offset
            if 0 <= ci < len(cache_history):
                ch = cache_history[ci]
                td["cache_sim"] = ch
            else:
                td["cache_sim"] = None
        html = generate_chart_html(None, breakdown_data, turns_data, active)

        screen = NSScreen.mainScreen().frame()
        num_turns = len(turns_data)
        width = max(720, min(int(screen.size.width * 0.9), 40 * (num_turns + 1) + 140))
        has_file_ops = any(td.get("file_ops") for td in turns_data)
        height = 700 if has_file_ops else 520
        frame = NSMakeRect(0, 0, width, height)
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        window.setTitle_(conv_label)
        window.setLevel_(NSFloatingWindowLevel)
        window.center()

        config = WKWebViewConfiguration.alloc().init()
        config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
        webview = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        webview.loadHTMLString_baseURL_(html, None)
        window.setContentView_(webview)
        window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

        if not hasattr(self, '_popup_windows'):
            self._popup_windows = []
        self._popup_windows.append(window)
