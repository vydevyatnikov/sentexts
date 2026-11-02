from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import secrets
import socket
from unittest.mock import MagicMock

import boto3
from botocore.session import Session
from botocore.response import StreamingBody
import pytest

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
    monkeypatch.setattr(
        "sentexts.config.user_config_path", lambda *args, **kwargs: tmp_path / "defaults",
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not make network requests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def client(tmp_path):
    return Sentexts(tmp_path)


@pytest.fixture
def credentials(tmp_path):
    access = "test-id-" + secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    (tmp_path / ".env").write_text(
        f"AWS_ACCESS_KEY_ID={access}\nAWS_SECRET_ACCESS_KEY={secret}\n",
        encoding="utf-8",
    )
    return access, secret


@pytest.fixture
def s3_session(tmp_path):
    """Isolate offline SDK contract checks without changing runtime AWS guards."""
    session = Session()
    config = session.get_component("config_store")
    config.set_config_variable("profile", None)
    config.set_config_variable("config_file", str(tmp_path / "unused-aws-config"))
    config.set_config_variable("credentials_file", str(tmp_path / "unused-aws-credentials"))
    return boto3.session.Session(botocore_session=session)


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
