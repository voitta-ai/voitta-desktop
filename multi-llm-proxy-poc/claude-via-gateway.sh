#!/bin/bash
# Start Claude Code against the local gateway. Extra arguments go to claude.
PORT="${LLMGW_PORT:-18910}"
if ! curl -sf "http://localhost:$PORT/_gw/api/state" >/dev/null; then
  echo "Gateway is not running on :$PORT. Start it with: ../.venv/bin/python run.py" >&2
  exit 1
fi
echo "Claude Code -> gateway http://localhost:$PORT (/llm picks this window's account)"
# No auth token: Claude Code keeps its own login, which the "As is" option forwards.
exec claude --settings "{\"env\":{\"ANTHROPIC_BASE_URL\":\"http://localhost:$PORT\"}}" "$@"
