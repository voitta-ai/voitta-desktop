"""Voitta Desktop's LLM accounts: one Claude Code client, many upstreams.

The LLM proxy hands each request, after its middleware, to ``Router.route``.
"As is" (the default) leaves the request on the proxy's own path to
Anthropic with Claude Code's own login. Any other account is answered here:
a Claude or ChatGPT subscription, or an API-key provider such as DeepSeek or
Mistral, translated to and from the Anthropic Messages API where needed.

Each Claude Code window can pick its own account with the ``/llm`` mod
(``mod/``); windows without a pick use the global default.
"""
