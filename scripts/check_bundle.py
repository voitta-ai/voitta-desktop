#!/usr/bin/env python3
"""Fail the build if the .app can't import itself.

Briefcase's `sources` list enumerates root modules one by one — there is no
glob. A new root module that isn't added there is simply absent from the
bundle, and nothing catches it: the test suite and `python app.py` both run
from the repo, where the file obviously exists. The failure only appears
when a user double-clicks the shipped app, as an ImportError on a module
that works fine everywhere else.

That has now happened twice (paths/runtime/lifecycle, then app_base), so
this runs as part of the build:

1. Every ``*.py`` at the repo root must appear in ``sources``.
2. The bundle's own interpreter must import the real entry point, with only
   the bundle's directories on the path — which is the actual thing that
   was broken and the only check that would have caught app_base.py.

Usage:  check_bundle.py <path to .app>
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Imported for their side effects on the entry path; if any of these fails to
# resolve inside the bundle, the app dies on launch.
ENTRY_MODULES = [
    "app",
    "app_base",
    "paths",
    "runtime",
    "lifecycle",
    "config",
    "claude_link",
    "ui.menu",
    "ui.tool_gate",
    "ui.main_thread",
    "ui.settings_window",
    "mcpproxy.server",
    "middleware.logger",
    "middleware.tracker",
    "optimizers.object_store",
    "auth.token_store",
    "auth.callback",
]


def check_sources_complete() -> list[str]:
    """Every root-level module must be declared in `sources`."""
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())
    sources = set(manifest["tool"]["briefcase"]["app"]["voitta_desktop"]["sources"])
    missing = sorted(
        p.name for p in ROOT.glob("*.py") if p.name not in sources
    )
    return missing


def check_bundle_imports(app_dir: pathlib.Path) -> str | None:
    """Import the entry modules with only the bundle's directories on the path.

    Briefcase ships a Python.framework with no interpreter executable, so we
    use the venv's — same 3.12 the bundle's ``app_packages`` extensions were
    built for. That means this validates *module presence and importability*
    inside the bundle, not the bundled interpreter itself, which is exactly
    the failure being guarded against.

    The repo root is dropped from ``sys.path`` so a module missing from the
    bundle cannot quietly resolve from the checkout — that would pass the
    check on a bundle that cannot launch. Paths are absolute because the
    subprocess runs with ``cwd`` inside the bundle, where a relative entry
    would resolve to nothing and every import would fail for the wrong
    reason.

    Returns None on success, or the captured error output.
    """
    resources = app_dir.resolve() / "Contents" / "Resources"
    app_path = resources / "app"
    packages_path = resources / "app_packages"
    if not app_path.is_dir():
        return f"no app directory at {app_path}"

    program = (
        "import sys\n"
        # Drop the repo root and cwd-relative entries, but keep the stdlib.
        f"sys.path = [p for p in sys.path if p not in ('', {str(ROOT)!r})]\n"
        f"sys.path[:0] = [{str(app_path)!r}, {str(packages_path)!r}]\n"
        "import importlib\n"
        f"for name in {ENTRY_MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, cwd=str(resources),
    )
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip()
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    app_dir = pathlib.Path(sys.argv[1])

    missing = check_sources_complete()
    if missing:
        print("[check_bundle] root modules missing from pyproject.toml sources:",
              file=sys.stderr)
        for name in missing:
            print(f"    {name}", file=sys.stderr)
        return 1

    if not app_dir.is_dir():
        print(f"[check_bundle] no such bundle: {app_dir}", file=sys.stderr)
        return 1

    error = check_bundle_imports(app_dir)
    if error:
        print("[check_bundle] the bundle cannot import itself:", file=sys.stderr)
        print(error, file=sys.stderr)
        print("\n[check_bundle] a missing module usually means it is absent from "
              "`sources` in pyproject.toml.", file=sys.stderr)
        return 1

    print(f"[check_bundle] ok — {len(ENTRY_MODULES)} entry modules import inside the bundle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
