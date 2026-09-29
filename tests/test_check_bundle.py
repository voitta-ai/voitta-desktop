"""check_bundle must observe the bundle without modifying it.

It runs after ``briefcase build`` has ad-hoc signed the .app. Importing the
app tree with bytecode writing enabled dropped ``__pycache__/*.pyc`` into the
sealed ``Resources/``, and every one of those was a "file added" that made
``codesign --verify`` fail on the finished build. Shipped that way once.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import check_bundle  # noqa: E402


def _fake_bundle(tmp_path: pathlib.Path) -> pathlib.Path:
    """The minimum shape check_bundle_imports() walks: an .app whose
    Resources/app holds one importable root module."""
    resources = tmp_path / "Fake.app" / "Contents" / "Resources" / "app"
    resources.mkdir(parents=True)
    (resources / "app.py").write_text("VALUE = 1\n")
    return tmp_path / "Fake.app"


def test_import_check_writes_no_bytecode_into_the_bundle(tmp_path, monkeypatch) -> None:
    app = _fake_bundle(tmp_path)
    monkeypatch.setattr(check_bundle, "ENTRY_MODULES", ["app"])

    error = check_bundle.check_bundle_imports(app)

    assert error is None, error
    pyc = list(app.rglob("*.pyc")) + list(app.rglob("__pycache__"))
    assert pyc == [], f"check_bundle modified the sealed bundle: {pyc}"


def test_import_check_runs_the_interpreter_with_bytecode_disabled(tmp_path, monkeypatch) -> None:
    """Pin the mechanism, not just the outcome: both the -B flag and the env
    var must be present, so a future refactor cannot drop one and keep the
    test green by luck of a cached module."""
    app = _fake_bundle(tmp_path)
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["env"] = kw.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(check_bundle.subprocess, "run", fake_run)
    monkeypatch.setattr(check_bundle, "ENTRY_MODULES", ["app"])

    check_bundle.check_bundle_imports(app)

    assert "-B" in seen["argv"]
    assert seen["env"].get("PYTHONDONTWRITEBYTECODE") == "1"
