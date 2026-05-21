"""Codex audit (commit 73a3a2a) — d5_build_runner._validate_dist tests.

A clean exit-0 from the `claude` CLI is not proof of a real site: the
/new-service-site skill can exit 0 with an empty, junk, or pathologically
oversized dist/. `_validate_dist` is the gate that turns those into loud,
bounded build failures instead of a "succeeded" row pointing at nothing.

d5_build_runner.py reads config from env at import but does NOT open a DB
connection, so importing it here is cheap and side-effect-free.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def runner_module():
    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    if "d5_build_runner" in sys.modules:
        del sys.modules["d5_build_runner"]
    return importlib.import_module("d5_build_runner")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TestValidateDist:
    def test_missing_dist_dir(self, runner_module, tmp_path):
        # build_dir exists but no dist/ subdir at all.
        reason = runner_module._validate_dist(tmp_path)
        assert reason == "missing dist/"

    def test_missing_index_html(self, runner_module, tmp_path):
        (tmp_path / "dist").mkdir()
        _write(tmp_path / "dist" / "style.css", b"body{}")
        reason = runner_module._validate_dist(tmp_path)
        assert reason == "missing dist/index.html"

    def test_empty_index_html(self, runner_module, tmp_path):
        # index.html exists but is below MIN_INDEX_BYTES.
        _write(tmp_path / "dist" / "index.html", b"<html></html>")  # ~13 bytes
        reason = runner_module._validate_dist(tmp_path)
        assert reason is not None
        assert "too small" in reason

    def test_oversized_dist(self, runner_module, tmp_path, monkeypatch):
        # Lower the cap so we don't have to write 25 MB to disk.
        monkeypatch.setattr(runner_module, "MAX_DIST_BYTES", 1000)
        monkeypatch.setattr(runner_module, "MIN_INDEX_BYTES", 10)
        _write(tmp_path / "dist" / "index.html", b"<html>" + b"x" * 2000 + b"</html>")
        reason = runner_module._validate_dist(tmp_path)
        assert reason is not None
        assert "too large" in reason

    def test_valid_dist_passes(self, runner_module, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_module, "MIN_INDEX_BYTES", 10)
        monkeypatch.setattr(runner_module, "MAX_DIST_BYTES", 25_000_000)
        index_html = b"<!doctype html><html><body><h1>Real Site</h1></body></html>"
        _write(tmp_path / "dist" / "index.html", index_html)
        _write(tmp_path / "dist" / "assets" / "app.js", b"console.log(1)")
        assert runner_module._validate_dist(tmp_path) is None
