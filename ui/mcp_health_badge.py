"""How the menu bar shows the MCP server's health (mcpproxy/health.py).

Broken: a red rounded rectangle around the whole status item (the count and
the dog together), a tooltip saying since when and why, and a red first line
in the dropdown. Recovered within the last day: a plain line saying when it
broke and when it came back. Healthy: nothing.
"""

from __future__ import annotations

import time

from AppKit import (
    NSBezierPath, NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName, NSView,
)
from Foundation import NSAttributedString, NSInsetRect

RECOVERED_SHOWN_S = 24 * 3600


def _hhmm(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


def describe(snap: dict | None, now: float | None = None) -> tuple[str | None, str | None, bool]:
    """(tooltip, dropdown line, is_broken) for a health snapshot."""
    if not snap:
        return None, None, False
    now = time.time() if now is None else now
    if snap["state"] == "broken":
        n = snap["failures"]
        head = f"MCP server failing since {_hhmm(snap['since'])} ({n} failed request{'s' if n != 1 else ''})"
        error = (snap["last_error"] or "")[:160]
        note = snap["note"] or "Voitta is rebuilding it; restart Voitta Desktop if it stays red."
        tooltip = f"{head}.\nLast error: {error}\n{note}\nDetails: ~/.voitta-desktop/logs/desktop.log"
        tail = "restart Voitta Desktop" if "Restart" in note else "rebuilding…"
        return tooltip, f"⚠ {head} — {tail}", True
    rec = snap.get("recovered")
    if rec and now - rec["at"] < RECOVERED_SHOWN_S:
        line = f"MCP server failed {_hhmm(rec['since'])}–{_hhmm(rec['at'])}, recovered (see desktop.log)"
        return None, line, False
    return None, None, False


class VoittaMcpRedFrame(NSView):
    """A red rounded rectangle over the status item; clicks pass through."""

    def drawRect_(self, _rect):
        NSColor.systemRedColor().setStroke()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSInsetRect(self.bounds(), 0.75, 0.75), 4.0, 4.0)
        path.setLineWidth_(1.5)
        path.stroke()

    def hitTest_(self, _point):
        return None


_FRAME_ID = "voitta-mcp-red-frame"


def style_status_button(button, broken: bool, tooltip: str | None):
    frame = next((v for v in button.subviews() if v.identifier() == _FRAME_ID), None)
    if frame is None and broken:
        frame = VoittaMcpRedFrame.alloc().initWithFrame_(button.bounds())
        frame.setIdentifier_(_FRAME_ID)
        frame.setAutoresizingMask_(18)  # follows the item's width and height
        button.addSubview_(frame)
    if frame is not None:
        frame.setFrame_(button.bounds())
        frame.setHidden_(not broken)
        frame.setNeedsDisplay_(True)
    button.setToolTip_(tooltip)


def style_alert_item(ns_item, line: str | None, broken: bool):
    """The dropdown's first line: hidden when there is nothing to say."""
    ns_item.setHidden_(line is None)
    if line is None:
        return
    attrs = {NSFontAttributeName: NSFont.menuFontOfSize_(0)}
    if broken:
        attrs[NSForegroundColorAttributeName] = NSColor.systemRedColor()
    ns_item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(line, attrs))
