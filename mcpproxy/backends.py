"""Loads MCP backend definitions from backends.yaml."""

from pathlib import Path

import yaml

_YAML_PATH = Path(__file__).with_name("backends.yaml")

with open(_YAML_PATH) as f:
    MCP_BACKENDS: list[dict] = yaml.safe_load(f)


def simple_backends():
    """Return backends that have a url (auto-mountable)."""
    return [b for b in MCP_BACKENDS if "url" in b]


def build_instructions():
    """Build the LLM instructions string from the registry."""
    lines = [
        "You are connected through Voitta Desktop, a unified MCP proxy. "
        "All tool names are prefixed by backend:"
    ]
    for b in MCP_BACKENDS:
        lines.append(f"  \u2022 {b['prefix']}_* \u2014 {b['description']}")
    lines.append(
        "If a google_workspace_* tool fails with an auth error, "
        "ask the user to log in via the Voitta Desktop menu bar icon."
    )
    return "\n".join(lines)


def tool_tree_groups():
    """Return (prefix, label) pairs for the settings tool tree."""
    return [(b["prefix"], b["label"]) for b in MCP_BACKENDS]
