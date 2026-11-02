from io import BytesIO
import json
import os
from pathlib import Path
import secrets
from unittest.mock import MagicMock

from botocore.response import StreamingBody
import pytest

from sentexts import Sentexts
from sentexts.cloud import MissingCredentialsError, load_credentials


def profile(tmp_path, name):
    folder = tmp_path / name
    directory = folder / "texts"
    directory.mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps({
        "storage_dir": str(directory), "cloud_folder": f"s3://test-corpus/{name}",
    }), encoding="utf-8")
    return Sentexts(folder)


def key_pair():
    return "test-id-" + secrets.token_hex(8), secrets.token_urlsafe(32)


def enter_keys(app, pair, monkeypatch):
    values = iter(pair)
    monkeypatch.setattr("sentexts.api.getpass", lambda **kwargs: next(values))
    app.update_env()


@pytest.mark.parametrize("operation", ["read", "upload"])
def test_cloud_operations_and_key_updates_are_instance_local(
    tmp_path, monkeypatch, capsys, operation,
):
    bindings = {
        "SETTINGS_PATH": str(tmp_path / "ignored-settings"),
        "CONFIG_PATH": str(tmp_path / "ignored.json"),
        "ENV_PATH": str(tmp_path / "ignored.env"),
    }
    for name, value in bindings.items():
        monkeypatch.setenv(name, value)
    first = profile(tmp_path, "first")
    second = profile(tmp_path, "second")
    original, replacement, other = key_pair(), key_pair(), key_pair()
    enter_keys(first, original, monkeypatch)
    enter_keys(second, other, monkeypatch)
    second_contents = second.settings.env_path.read_bytes()
    enter_keys(first, replacement, monkeypatch)
    assert second.settings.env_path.read_bytes() == second_contents
    assert load_credentials(first.settings.env_path) == replacement
    assert load_credentials(second.settings.env_path) == other
    assert {name: os.environ[name] for name in bindings} == bindings

    sdk = MagicMock()
    sdk.get_object.side_effect = lambda **kw: {
        "Body": StreamingBody(BytesIO(b"downloaded"), len(b"downloaded")),
    }
    factory = MagicMock(return_value=sdk)
    monkeypatch.setattr("sentexts.cloud.boto3.client", factory)
    source = tmp_path / "source.txt"
    source.write_bytes(b"upload")
    for index, app in enumerate((first, second, first)):
        if operation == "read":
            name = f"case-{index}"
            assert app.read(name) == {name + ".txt": "downloaded"}
        else:
            expected = app.settings.cloud_folder + "/source.txt"
            assert app.upload(source) == {"source.txt": expected}
    assert [
        (call.kwargs["aws_access_key_id"], call.kwargs["aws_secret_access_key"])
        for call in factory.call_args_list
    ] == [replacement, other, replacement]
    assert {name: os.environ[name] for name in bindings} == bindings
    output = capsys.readouterr()
    assert all(value not in output.out + output.err for pair in (original, replacement, other) for value in pair)


def test_instance_without_keys_does_not_use_another_instances_keys(tmp_path, monkeypatch):
    configured = profile(tmp_path, "configured")
    blank = profile(tmp_path, "blank")
    enter_keys(configured, key_pair(), monkeypatch)
    factory = MagicMock()
    monkeypatch.setattr("sentexts.cloud.boto3.client", factory)
    with pytest.raises(MissingCredentialsError):
        blank.read("missing")
    factory.assert_not_called()
    assert not blank.exists("missing.txt")


def test_settings_updates_and_saves_do_not_change_another_profile(tmp_path):
    first = profile(tmp_path, "first")
    second = profile(tmp_path, "second")
    second_contents = second.settings.config_path.read_bytes()
    first.update_settings(cloud_folder="s3://test-corpus/changed")
    first.save_settings()
    assert second.settings.cloud_folder == "s3://test-corpus/second"
    assert second.settings.config_path.read_bytes() == second_contents
    assert Sentexts(first.settings.config_path.parent).settings.cloud_folder == "s3://test-corpus/changed"


def test_instances_sharing_a_folder_have_separate_runtime_settings(tmp_path):
    first = profile(tmp_path, "shared")
    second = Sentexts(first.settings.config_path.parent)
    first.update_settings(cloud_folder="s3://test-corpus/in-memory")
    assert first.settings is not second.settings
    assert second.settings.cloud_folder == "s3://test-corpus/shared"
    first.save_settings()
    assert second.settings.cloud_folder == "s3://test-corpus/shared"
    assert Sentexts(first.settings.config_path.parent).settings.cloud_folder == "s3://test-corpus/in-memory"


def test_relative_settings_folder_is_fixed_at_construction(tmp_path, monkeypatch):
    original = profile(tmp_path, "relative")
    monkeypatch.chdir(tmp_path)
    app = Sentexts("relative")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    enter_keys(app, key_pair(), monkeypatch)
    app.update_settings(cloud_folder="s3://test-corpus/saved")
    assert app.save_settings() == original.settings.config_path
    assert app.settings.env_path == original.settings.env_path
    assert not list(elsewhere.iterdir())


def test_empty_key_input_does_not_modify_either_instances_file(tmp_path, monkeypatch):
    first = profile(tmp_path, "first")
    second = profile(tmp_path, "second")
    before = first.settings.env_path.read_bytes(), second.settings.env_path.read_bytes()
    values = iter(["", "synthetic-value"])
    monkeypatch.setattr("sentexts.api.getpass", lambda **kwargs: next(values))
    with pytest.raises(ValueError):
        first.update_env()
    assert (first.settings.env_path.read_bytes(), second.settings.env_path.read_bytes()) == before
