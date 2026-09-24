"""Focused tests for the standalone Admin UI locale bundle resolver."""

from pathlib import Path
import os

import orjson
import pytest

from mcpgateway import admin


@pytest.fixture(autouse=True)
def clear_i18n_cache():
    admin._i18n_js_cache["filename"] = None
    yield
    admin._i18n_js_cache["filename"] = None


def test_i18n_bundle_from_manifest_src(tmp_path: Path):
    bundle = tmp_path / "i18n-hashed.js"
    bundle.touch()
    manifest_dir = tmp_path / ".vite"
    manifest_dir.mkdir()
    (manifest_dir / "manifest.json").write_bytes(
        orjson.dumps({"arbitrary-key": {"src": "mcpgateway/admin_ui/i18n/standalone.js", "file": bundle.name}})
    )
    assert admin.get_i18n_js_filename(tmp_path) == bundle.name


def test_i18n_bundle_falls_back_to_newest_file(tmp_path: Path):
    older = tmp_path / "i18n-old.js"
    older.touch()
    newest = tmp_path / "i18n-new.js"
    newest.touch()
    os.utime(older, (1, 1))
    os.utime(newest, (2, 2))
    assert admin.get_i18n_js_filename(tmp_path) == newest.name


def test_i18n_bundle_missing_is_fail_open(tmp_path: Path):
    assert admin.get_i18n_js_filename(tmp_path) == ""


def test_i18n_bundle_refreshes_stale_cache(tmp_path: Path):
    first = tmp_path / "i18n-first.js"
    first.touch()
    assert admin.get_i18n_js_filename(tmp_path) == first.name
    first.unlink()
    second = tmp_path / "i18n-second.js"
    second.touch()
    assert admin.get_i18n_js_filename(tmp_path) == second.name
