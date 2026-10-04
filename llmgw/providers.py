"""Provider presets.

Every account has a ``kind`` that selects the adapter in ``upstream.py``:

  anthropic_oauth   Claude subscription; pass through, swap auth
  anthropic_compat  Anthropic-shaped API-key endpoint (DeepSeek, Kimi, GLM...)
  openai_chat       OpenAI Chat Completions shape (Mistral, OpenAI API, ...)
  openai_codex      ChatGPT subscription via the Codex Responses backend

``models`` starts empty; the first model listing fills it from the provider's
own catalog (``models.suggest``). It maps Claude Code's two tiers onto the provider's models: requests
for a ``*haiku*`` model use ``small``, every other ``claude-*`` model uses
``big``. A model name that does not start with ``claude`` is sent as-is, so
``/model deepseek-reasoner`` in Claude Code works without remapping.
"""

from .common import Unsupported

PRESETS = {
    "claude": {
        "label": "Claude (subscription)",
        "kind": "anthropic_oauth",
        "base_url": "https://api.anthropic.com",
        "auth": "oauth",
        "models": None,  # native, never remapped
    },
    "openai": {
        "label": "ChatGPT (subscription)",
        "kind": "openai_codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "auth": "oauth",
        "models": {"big": "", "small": ""},
    },
    "deepseek": {
        "label": "DeepSeek (API key)",
        "kind": "anthropic_compat",
        "base_url": "https://api.deepseek.com/anthropic",
        "auth": "api_key",
        "models_listing": "openai_root",
        "models": {"big": "", "small": ""},
    },
    "mistral": {
        "label": "Mistral (API key)",
        "kind": "openai_chat",
        "base_url": "https://api.mistral.ai/v1",
        "auth": "api_key",
        "models": {"big": "", "small": ""},
        # Mistral rejects tool_call ids that are not exactly 9 alphanumerics,
        # and rejects unknown request fields such as stream_options.
        "tool_id_style": "alnum9",
        "stream_usage_option": False,
    },
    "openai_api": {
        "label": "OpenAI (API key)",
        "kind": "openai_chat",
        "base_url": "https://api.openai.com/v1",
        "auth": "api_key",
        "models": {"big": "", "small": ""},
        "max_tokens_field": "max_completion_tokens",
        "reasoning_param": True,
    },
    "custom_anthropic": {
        "label": "Other Anthropic-compatible (API key)",
        "kind": "anthropic_compat",
        "base_url": "",
        "auth": "api_key",
        "models": {"big": "", "small": ""},
    },
    "custom_openai": {
        "label": "Other OpenAI-compatible (API key)",
        "kind": "openai_chat",
        "base_url": "",
        "auth": "api_key",
        "models": {"big": "", "small": ""},
    },
}

# Adapter options an account inherits from its preset unless it overrides them.
OPTION_KEYS = ("tool_id_style", "stream_usage_option", "max_tokens_field", "reasoning_param")


def preset(provider: str) -> dict:
    try:
        return PRESETS[provider]
    except KeyError:
        raise ValueError(f"unknown provider {provider!r}") from None


def option(account: dict, key: str, default=None):
    if key in account.get("options", {}):
        return account["options"][key]
    return PRESETS.get(account["provider"], {}).get(key, default)


def map_model(account: dict, requested: str) -> str:
    models = account.get("models")
    if not models or not requested.startswith("claude"):
        return requested
    tier = "small" if "haiku" in requested else "big"
    if not models.get(tier):
        raise Unsupported(f"{account['label']}: no {'Background' if tier == 'small' else 'Main'} model "
                          f"is set (Claude Code asked for {requested}); pick one in Voitta Desktop (Settings → LLMs)")
    return models[tier]
