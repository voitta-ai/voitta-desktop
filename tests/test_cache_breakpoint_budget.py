"""The injected cache breakpoint must respect Anthropic's 4-block limit.

Claude Code now places 4 cache_control markers itself (2 on system, 2 at
the message tail). The pipeline's extra stable-zone marker made 5, and the
API rejects every such request with a 400 — see the fail_400 dumps from
2026-08-27. The injector now counts markers across tools/system/messages
and yields when the budget is spent.
"""

import json

from middleware.base import ProxyRequest
from optimizers import OptimizerPipeline


def _pipeline():
    return OptimizerPipeline([], enabled=True)


def _request(n_system_markers=0, n_tail_markers=0, n_tool_markers=0, turns=6):
    """A body with `turns` human turns and the given marker layout."""
    tools = [{"name": f"t{i}", "input_schema": {}} for i in range(3)]
    for i in range(n_tool_markers):
        tools[i]["cache_control"] = {"type": "ephemeral"}

    system = [{"type": "text", "text": "identity"},
              {"type": "text", "text": "big prompt"}]
    for i in range(n_system_markers):
        system[i]["cache_control"] = {"type": "ephemeral"}

    messages = []
    for i in range(turns):
        messages.append({"role": "user",
                         "content": [{"type": "text", "text": f"question {i}"}]})
        messages.append({"role": "assistant",
                         "content": [{"type": "text", "text": f"answer {i}"}]})
    for k in range(n_tail_markers):
        messages[-(1 + 2 * k)]["content"][0]["cache_control"] = \
            {"type": "ephemeral", "ttl": "1h"}

    body = {"model": "claude-opus-5", "system": system, "tools": tools,
            "messages": messages}
    req = ProxyRequest(method="POST", path="/v1/messages", headers={}, body=b"")
    req.json = body
    return req


def _marker_count(req):
    return OptimizerPipeline._count_cache_markers(req.json)


def test_injects_when_under_budget():
    req = _request(n_system_markers=2, n_tail_markers=1)   # 3 markers
    out = _pipeline()._inject_cache_breakpoint(req)
    assert _marker_count(out) == 4


def test_skips_at_api_max():
    """The 2026-08-27 regression: Claude Code's own 4 markers + ours = 400."""
    req = _request(n_system_markers=2, n_tail_markers=2)   # 4 markers
    before = json.dumps(req.json, sort_keys=True)
    out = _pipeline()._inject_cache_breakpoint(req)
    assert _marker_count(out) == 4
    assert json.dumps(out.json, sort_keys=True) == before   # untouched


def test_counts_tool_markers_toward_budget():
    req = _request(n_tool_markers=2, n_system_markers=2)   # 4, all pre-messages
    out = _pipeline()._inject_cache_breakpoint(req)
    assert _marker_count(out) == 4


def test_count_helper_spans_all_sections():
    req = _request(n_tool_markers=1, n_system_markers=1, n_tail_markers=1)
    assert _marker_count(req) == 3
