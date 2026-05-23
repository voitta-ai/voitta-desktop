"""Briefcase entry shim — runs app.main().

Briefcase's macOS template launches the app via `python -m voitta_desktop`,
so we need a __main__.py inside this package. Both the bundled run and the
terminal-dev run land in the same `app.main()`, keeping behavior identical.

The bundled .app stages every source listed in pyproject.toml's
`[tool.briefcase.app.voitta_desktop].sources` into Contents/Resources/app/,
so app.py and its sibling packages (auth, mcpproxy, …) are already on the
default sys.path. No path manipulation needed.
"""
from app import main

if __name__ == "__main__":
    main()

