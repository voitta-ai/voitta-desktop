"""Jira URL parsing and project fetching."""

import base64
import re
from urllib.parse import urlparse, parse_qs

import requests


def parse_jira_url(url: str) -> tuple[str, str]:
    """Parse a Jira URL into (base_url, project_key).

    Supports:
      https://org.atlassian.net/jira/software/projects/PROJ/issues/...
      https://jira.example.com/browse/PROJ
      https://jira.example.com/browse/PROJ-123
      https://jira.example.com/projects/PROJ
      Also extracts project from JQL query param.
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path_parts = [p for p in (parsed.path or "").strip("/").split("/") if p]

    project_key = ""
    for i, part in enumerate(path_parts):
        if part in ("projects", "browse") and i + 1 < len(path_parts):
            key_part = path_parts[i + 1]
            project_key = key_part.split("-")[0].upper()
            break

    if not project_key:
        qs = parse_qs(parsed.query)
        jql = qs.get("jql", [""])[0]
        if jql:
            m = re.search(r'project\s*=\s*["\']?(\w+)', jql)
            if m:
                project_key = m.group(1).upper()

    return base_url, project_key


def fetch_jira_projects(base_url: str, email: str, token: str) -> list[tuple[str, str]]:
    """Fetch available Jira Cloud projects. Returns list of (key, name) tuples."""
    cred = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {cred}", "Content-Type": "application/json"}
    projects = []
    start_at = 0
    while True:
        try:
            resp = requests.get(
                f"{base_url}/rest/api/3/project/search",
                params={"startAt": start_at, "maxResults": 50},
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[jira] Failed to fetch projects: {resp.status_code} {resp.text[:200]}")
                break
            data = resp.json()
            for p in data.get("values", []):
                projects.append((p["key"], p.get("name", p["key"])))
            if data.get("isLast", True):
                break
            start_at += len(data.get("values", []))
        except Exception as e:
            print(f"[jira] Error fetching projects: {e}")
            break
    projects.sort()
    return projects
