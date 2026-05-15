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


def _safe_json(value) -> str:
    """JSON-encode for inclusion inside an HTML <script> block.

    Escapes ``</`` as ``<\\/`` — a JSON string containing ``</script>``
    would otherwise terminate the surrounding ``<script>`` tag and the rest
    of the data would render as page text (exactly the symptom users hit
    when a tool result captured HTML source containing ``</script>``).
    ``\\/`` is equivalent to ``/`` per JSON 7159 §7 (and the JS HTML5 spec
    explicitly allows it), so the parsed value is unchanged in both
    Python's ``json.loads`` and the browser's ``JSON.parse``.
    """
    return json.dumps(value).replace("</", "<\\/")


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
        f"const _BREAKDOWN = {_safe_json(breakdown)};"
        f"const _TURNS = {_safe_json(turns)};"
        f"const _ACTIVE_OPT = {_safe_json(active_optimizers)};"
        "</script>"
    )

    html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        html
        .replace("<!--TITLE_DIV-->", title_div)
        .replace("<!--DATA-->", data_script)
    )
