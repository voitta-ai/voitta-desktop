"""Voitta multi-LLM gateway (POC).

An Anthropic-Messages-shaped endpoint for Claude Code that forwards each
request to whichever upstream account is currently active: a Claude
subscription, a ChatGPT subscription (Codex backend), or an API-key
provider such as DeepSeek or Mistral.
"""
