import stat

from llmgw.store import AS_IS, AccountStore, SessionPicks, public_view


def test_relogin_replaces_credentials_and_keeps_settings(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    a, replaced = store.upsert("claude", "u1:o1", "a@x.com", {"access_token": "old", "refresh_token": "r", "expires_at": 1})
    assert not replaced and store.active_id == AS_IS  # adding an account never changes the default
    b, _ = store.upsert("openai", "u2:acc", "b@y.com", {"access_token": "t", "refresh_token": "r", "expires_at": 1})
    store.set_active(b["id"])
    store.update_settings(b["id"], models={"big": "custom", "small": "custom-mini"})
    store.set_status(b["id"], "needs_login", "expired")

    again, replaced = store.upsert("openai", "u2:acc", "b@y.com", {"access_token": "new", "refresh_token": "r2", "expires_at": 2})
    assert replaced
    assert len(store.list()) == 2
    assert again["credentials"]["access_token"] == "new"
    assert again["models"] == {"big": "custom", "small": "custom-mini"}
    assert again["status"] == "ok"
    assert store.active_id == b["id"]

    # persisted, private
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reloaded = AccountStore(path)
    assert reloaded.get(b["id"])["credentials"]["access_token"] == "new"


def test_deleting_the_default_resets_it_to_as_is(tmp_path):
    store = AccountStore(tmp_path / "a.json")
    store.upsert("deepseek", "k1", "ds", {"api_key": "sk-123456789"})
    store.upsert("mistral", "k2", "mi", {"api_key": "abc"})
    store.set_active("deepseek:k1")
    store.delete("deepseek:k1")
    assert store.active_id == AS_IS  # never some other account


def test_session_picks_persist_and_expire(tmp_path):
    path = tmp_path / "sessions.json"
    picks = SessionPicks(path)
    picks.set("s1", "deepseek:k1")
    picks.set("s2", "mistral:k2")
    picks.set("s2", None)
    assert SessionPicks(path).all().keys() == {"s1"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    import json, time
    data = json.loads(path.read_text())
    data["s1"]["used_at"] = time.time() - SessionPicks.PICK_TTL_S - 1
    path.write_text(json.dumps(data))
    assert SessionPicks(path).get("s1") is None


def test_public_view_hides_secrets(tmp_path):
    store = AccountStore(tmp_path / "a.json")
    a, _ = store.upsert("deepseek", "k1", "ds", {"api_key": "sk-123456789"})
    view = public_view(a)
    assert "credentials" not in view and view["key_hint"] == "…6789"
    assert view["models"] == {"big": "", "small": ""}  # filled from the provider's list, never hardcoded
