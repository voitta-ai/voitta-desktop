"""Generate the conversation-chart HTML used by both the menu-bar popup and test harness.

The template lives in ``chart_template.html`` alongside this module. We inject
a single ``<script>`` block holding three data globals (``_BREAKDOWN``,
``_TURNS``, ``_ACTIVE_OPT``) plus the optional title div — the template
reads those globals on load. Keeping the JS in a real .js/.html file means
no f-string escape doubling and no Python brace-mangling when editing.
"""

from __future__ import annotations

import json
from pathlib import Path

_TEMPLATE_PATH = Path(__file__).parent / "chart_template.html"


def generate_chart_html(
    title: str | None,
    breakdown: dict,
    turns: list[dict],
    active_optimizers: dict[str, int] | None = None,
) -> str:
    """Return a self-contained HTML page with a stacked-bar + cumulative-line chart.

    Parameters
    ----------
    title:
        Optional heading shown above the legend. When *None* the slot
        collapses (no extra vertical space).
    breakdown:
        ``{"system": int, "tools": int, ...}`` — overhead char counts.
        May also contain ``system_blocks``, ``tool_groups``, ``tools_count``.
    turns:
        Per-turn dicts with keys such as ``index``, ``label``, ``user_text``,
        ``tool_result``, ``assistant_text``, ``tool_call``, ``image``,
        ``images``, and optional ``blocks``.
    active_optimizers:
        ``{chart_key: keep_turns}`` for each enabled optimizer. Only keys
        present here will be hatched and subtracted from the cumulative
        optimized line. ``None`` means no optimizers.
    """
    if active_optimizers is None:
        active_optimizers = {}

    title_div = f'<div class="title">{title}</div>' if title else ""
    data_script = (
        "<script>"
        f"const _BREAKDOWN = {json.dumps(breakdown)};"
        f"const _TURNS = {json.dumps(turns)};"
        f"const _ACTIVE_OPT = {json.dumps(active_optimizers)};"
        "</script>"
    )

    html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        html
        .replace("<!--TITLE_DIV-->", title_div)
        .replace("<!--DATA-->", data_script)
    )
