from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import socket
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from botocore.response import StreamingBody
import pytest

# Package import prepares templates. Keep collection-time setup outside checkout.
_bootstrap = TemporaryDirectory(prefix="sentexts-pytest-bootstrap-")
os.environ["CONFIG_PATH"] = str(Path(_bootstrap.name) / "config.json")
os.environ["ENV_PATH"] = str(Path(_bootstrap.name) / ".env")

from sentexts import Sentexts


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    directory = tmp_path / "texts"
    directory.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "storage_dir": str(directory),
        "cloud_folder": "s3://test-corpus/Судебные решения",
    }), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("AWS_ACCESS_KEY_ID=\nAWS_SECRET_ACCESS_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_PATH", str(config))
    monkeypatch.setenv("ENV_PATH", str(env))

    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not make network requests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def client():
    return Sentexts()


@pytest.fixture
def credentials():
    access = "test-id-" + secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    Path(os.environ["ENV_PATH"]).write_text(
        f"AWS_ACCESS_KEY_ID={access}\nAWS_SECRET_ACCESS_KEY={secret}\n",
        encoding="utf-8",
    )
    return access, secret


@pytest.fixture
def sdk(monkeypatch, credentials):
    instance = MagicMock(name="s3_client")
    instance.__enter__.return_value = instance

    def response(**kwargs):
        payload = "Суд постановил.\n".encode("utf-8")
        return {"Body": StreamingBody(BytesIO(payload), len(payload))}

    instance.get_object.side_effect = response
    factory = MagicMock(return_value=instance)
    monkeypatch.setattr("sentexts.cloud.boto3.client", factory)
    return instance, factory, credentials
