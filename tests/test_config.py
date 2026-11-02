from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from sentexts import Sentexts
from sentexts.config import ConfigError, Settings


def test_settings_load_as_local_path_and_cloud_string(client):
    assert client.settings.storage_dir.is_absolute()
    assert client.settings.storage_dir.is_dir()
    assert client.settings.cloud_folder == "s3://test-corpus/Судебные решения"


def test_valid_batch_changes_runtime_then_explicitly_saves(client, tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    before = client.settings.config_path.read_bytes()
    client.update_settings(storage_dir=str(selected), cloud_folder="s3://test-corpus/Другие решения")
    assert client.settings.storage_dir == selected
    assert client._storage.root == selected
    assert client.settings.config_path.read_bytes() == before
    assert client.save_settings() == client.settings.config_path
    restored = Sentexts()
    assert restored.settings.storage_dir == selected
    assert restored.settings.cloud_folder == "s3://test-corpus/Другие решения"
    assert set(json.loads(client.settings.config_path.read_text(encoding="utf-8"))) == {
        "storage_dir", "cloud_folder"
    }


@pytest.mark.parametrize("field", ["unexpected", "config_path", "retention_days", "token"])
def test_unknown_fields_reject_entire_batch(client, tmp_path, field):
    selected = tmp_path / "new"
    selected.mkdir()
    before = client.settings.storage_dir, client.settings.cloud_folder
    contents = client.settings.config_path.read_bytes()
    with pytest.raises(ConfigError):
        client.update_settings(storage_dir=selected, cloud_folder="s3://test-corpus/changed", **{field: "invalid"})
    assert (client.settings.storage_dir, client.settings.cloud_folder) == before
    assert client._storage.root == before[0]
    assert client.settings.config_path.read_bytes() == contents


@pytest.mark.parametrize("folder", [None, 1, True, "", " ", "../outside", "disk:/../outside", "a\\b", "https://example.org", "bad\nfolder", "disk:/sentexts", "s3://", "s3://UPPERCASE", "s3://bad_bucket", "s3://user:secret@bucket/", "s3://bucket/../outside", "s3://bucket/a\\b", "s3://ab", "s3://bucket../prefix"])
def test_invalid_values_do_not_apply_other_valid_changes(client, tmp_path, folder):
    selected = tmp_path / "selected"
    selected.mkdir()
    before = client.settings.storage_dir, client.settings.cloud_folder
    with pytest.raises(ConfigError):
        client.update_settings(storage_dir=selected, cloud_folder=folder)
    assert (client.settings.storage_dir, client.settings.cloud_folder) == before
    assert client._storage.root == before[0]


@pytest.mark.parametrize("value", [None, True, 5, "", "bad\x00path"])
def test_invalid_storage_value_is_a_config_error(client, value):
    before = client.settings.storage_dir
    with pytest.raises(ConfigError):
        client.update_settings(storage_dir=value)
    assert client.settings.storage_dir == before


def test_storage_requires_existing_directory_not_file(client, tmp_path):
    file = tmp_path / "file.txt"
    file.write_text("ordinary file")
    for value in (tmp_path / "missing", file):
        with pytest.raises(ConfigError, match="existing directory"):
            client.update_settings(storage_dir=value)
    assert not (tmp_path / "missing").exists()


def test_settings_update_directly_is_reflected_by_client(client, tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "one.txt").write_text("new root", encoding="utf-8")
    client.settings.update(storage_dir=selected)
    assert client.read("one.txt") == {"one.txt": "new root"}


def test_failed_save_preserves_existing_json(client, monkeypatch):
    original = client.settings.config_path.read_bytes()
    client.update_settings(cloud_folder="s3://test-corpus/changed")

    def denied(*args, **kwargs):
        raise PermissionError("destination is read-only")

    monkeypatch.setattr("sentexts.config.os.replace", denied)
    with pytest.raises(PermissionError):
        client.save_settings()
    assert client.settings.config_path.read_bytes() == original
    assert client.settings.cloud_folder == "s3://test-corpus/changed"
    assert not list(client.settings.config_path.parent.glob(".sentexts-config-*"))


@pytest.mark.parametrize("content", ["bad JSON", "null", "[]", "{}", '{"storage_dir":"data"}', '{"storage_dir":null,"cloud_folder":null,"token":""}'])
def test_malformed_config_is_not_ignored(client, content):
    client.settings.config_path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        Settings()


def test_null_defaults_are_created_but_not_implicitly_saved(client, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path = client.settings.config_path
    path.write_text('{"storage_dir":null,"cloud_folder":null}', encoding="utf-8")
    with pytest.warns(UserWarning) as warnings:
        defaults = Settings()
    assert len(warnings) == 2
    assert defaults.storage_dir == tmp_path / "texts"
    assert defaults.storage_dir.is_dir()
    assert defaults.cloud_folder == "s3://sentexts-corpus-73fa/texts/"
    assert json.loads(path.read_text(encoding="utf-8"))["storage_dir"] is None


def test_relative_path_is_fixed_once(client, monkeypatch, tmp_path):
    relative = tmp_path / "relative"
    relative.mkdir()
    (relative / "one.txt").write_text("cached", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    client.settings.config_path.write_text(json.dumps({
        "storage_dir": "relative", "cloud_folder": "s3://test-corpus/texts",
    }), encoding="utf-8")
    restored = Sentexts()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert restored.read("one.txt") == {"one.txt": "cached"}
    assert restored.settings.storage_dir == relative


def test_import_templates_preserve_existing_files_and_bindings(tmp_path):
    config = tmp_path / "custom" / "config.json"
    env = tmp_path / "credentials" / ".env"
    child_env = os.environ.copy()
    child_env.update(CONFIG_PATH=str(config), ENV_PATH=str(env))
    code = (
        "import os, pathlib, sentexts; "
        "assert sentexts.Sentexts; "
        "assert pathlib.Path(os.environ['CONFIG_PATH']).is_file(); "
        "assert pathlib.Path(os.environ['ENV_PATH']).is_file()"
    )
    subprocess.run([sys.executable, "-I", "-c", code], env=child_env, check=True, capture_output=True)
    assert json.loads(config.read_text(encoding="utf-8")) == {"storage_dir": None, "cloud_folder": None}
    assert env.read_text(encoding="utf-8").endswith("AWS_SECRET_ACCESS_KEY=\n")
    if os.name == "posix":
        assert env.stat().st_mode & 0o777 == 0o600
    config.write_text("existing user config", encoding="utf-8")
    env.write_text("# existing user file\n", encoding="utf-8")
    subprocess.run([sys.executable, "-I", "-c", code], env=child_env, check=True, capture_output=True)
    assert config.read_text(encoding="utf-8") == "existing user config"
    assert env.read_text(encoding="utf-8") == "# existing user file\n"
