"""Claude-link wrappers and the single source for the `armed` default.

Two defects motivated these. The TUI imported ``arm_claude_link`` and
``disarm_claude_link``, which did not exist — the load/check/plan/apply
sequence had been open-coded at three call sites instead. And the
``claude_link.armed`` default was written out at three places, two of which
disagreed with the third, so the effective value depended on which code path
happened to read the config first.

These functions edit a file outside our own tree (~/.claude/settings.json),
so idempotency is not a nicety.
"""

import json

import pytest

import claude_link
from config import CLAUDE_LINK_ARMED_DEFAULT, claude_link_armed

PORT = 18900
UPSTREAM = "https://api.anthropic.com"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(claude_link, "CLAUDE_SETTINGS_PATH", path)
    return path


# ── The functions the TUI could not import ───────────────────────────────────

def test_arm_and_disarm_exist():
    """The TUI's import used to fail outright."""
    assert callable(claude_link.arm_claude_link)
    assert callable(claude_link.disarm_claude_link)


def test_arm_wires_the_proxy_in(settings):
    assert claude_link.arm_claude_link(PORT, UPSTREAM) is True

    env = json.loads(settings.read_text())["env"]
    assert str(PORT) in env["ANTHROPIC_BASE_URL"]


def test_arm_twice_is_a_no_op(settings):
    claude_link.arm_claude_link(PORT, UPSTREAM)
    before = settings.read_text()

    assert claude_link.arm_claude_link(PORT, UPSTREAM) is False
    assert settings.read_text() == before


def test_disarm_removes_it_again(settings):
    claude_link.arm_claude_link(PORT, UPSTREAM)

    assert claude_link.disarm_claude_link(PORT) is True

    env = json.loads(settings.read_text()).get("env", {})
    assert "ANTHROPIC_BASE_URL" not in env


def test_disarm_when_not_armed_is_a_no_op(settings):
    settings.write_text(json.dumps({"env": {"SOMETHING_ELSE": "1"}}))

    assert claude_link.disarm_claude_link(PORT) is False


def test_unrelated_settings_are_preserved(settings):
    """We rewrite the whole file, so everything else has to survive it."""
    settings.write_text(json.dumps({
        "mcpServers": {"other": {"command": "x"}},
        "permissions": {"allow": ["Bash"]},
        "env": {"MY_VAR": "keep me"},
    }))

    claude_link.arm_claude_link(PORT, UPSTREAM)
    claude_link.disarm_claude_link(PORT)

    cfg = json.loads(settings.read_text())
    assert cfg["mcpServers"] == {"other": {"command": "x"}}
    assert cfg["permissions"] == {"allow": ["Bash"]}
    assert cfg["env"]["MY_VAR"] == "keep me"


def test_arm_creates_a_missing_settings_file(settings):
    assert not settings.exists()

    claude_link.arm_claude_link(PORT, UPSTREAM)

    assert settings.exists()


def test_round_trip_restores_the_base_url(settings):
    """Disarm must undo the redirect. ENABLE_TOOL_SEARCH is left behind on
    purpose — plan_disconnect documents that we don't track whether we added
    it, and "true" is harmless when not connected."""
    settings.write_text(json.dumps({"env": {"MY_VAR": "keep me"}}, indent=2) + "\n")

    claude_link.arm_claude_link(PORT, UPSTREAM)
    claude_link.disarm_claude_link(PORT)

    env = json.loads(settings.read_text())["env"]
    assert "ANTHROPIC_BASE_URL" not in env
    assert "VOITTA_ANTHROPIC_BASE_URL" not in env
    assert env["MY_VAR"] == "keep me"


def test_a_users_own_base_url_is_saved_and_restored(settings):
    """Someone already pointing Claude elsewhere must get it back verbatim."""
    settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://mine"}}))

    claude_link.arm_claude_link(PORT, UPSTREAM)
    armed = json.loads(settings.read_text())["env"]
    assert armed["VOITTA_ANTHROPIC_BASE_URL"] == "https://mine"

    claude_link.disarm_claude_link(PORT)
    restored = json.loads(settings.read_text())["env"]
    assert restored["ANTHROPIC_BASE_URL"] == "https://mine"
    assert "VOITTA_ANTHROPIC_BASE_URL" not in restored


# ── The default that disagreed with itself ───────────────────────────────────

def test_default_is_opt_in():
    """Arming edits a file we don't own, so absent config must mean off."""
    assert CLAUDE_LINK_ARMED_DEFAULT is False
    assert claude_link_armed({}) is False


def test_explicit_config_wins():
    assert claude_link_armed({"claude_link": {"armed": True}}) is True
    assert claude_link_armed({"claude_link": {"armed": False}}) is False


def test_malformed_config_falls_back_to_the_default():
    assert claude_link_armed({"claude_link": None}) is CLAUDE_LINK_ARMED_DEFAULT
    assert claude_link_armed({"claude_link": {}}) is CLAUDE_LINK_ARMED_DEFAULT
