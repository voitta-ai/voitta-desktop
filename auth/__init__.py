"""OAuth2 authentication for Microsoft, Google, and Jira."""

from .providers import (
    OAUTH_SCOPES, scopes_for_app, pkce_pair,
    do_auth_microsoft, do_auth_google,
    fetch_profile_microsoft, fetch_profile_google,
    do_refresh_microsoft, do_refresh_google,
)
from .callback import OAuthCallbackHandler, wait_for_callback
from .jira import parse_jira_url, fetch_jira_projects

__all__ = [
    "OAUTH_SCOPES", "scopes_for_app", "pkce_pair",
    "do_auth_microsoft", "do_auth_google",
    "fetch_profile_microsoft", "fetch_profile_google",
    "do_refresh_microsoft", "do_refresh_google",
    "OAuthCallbackHandler", "wait_for_callback",
    "parse_jira_url", "fetch_jira_projects",
]
