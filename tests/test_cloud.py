from io import BytesIO
import os
from pathlib import Path
import traceback
import warnings

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from botocore.parsers import ResponseParserError
from botocore.response import StreamingBody
from botocore.stub import Stubber
import pytest

from sentexts.cloud import (
    CloudAuthError, CloudConnectionError, CloudError, CloudFileNotFoundWarning,
    CloudFolderError, CloudPermissionError, MissingCredentialsError,
    MissingTokenError, download, load_credentials,
)
from sentexts.config import ConfigError


def sdk_error(code, message="", status=403):
    return ClientError({
        "Error": {"Code": code, "Message": message},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }, "GetObject")


def object_response(payload=b"available"):
    return {"Body": StreamingBody(BytesIO(payload), len(payload))}


def test_download_is_complete_binary_and_cached(client, sdk):
    instance, factory, credentials = sdk
    result = client.read("Решение.txt")
    assert result == {"Решение.txt": "Суд постановил.\n"}
    target = client.settings.storage_dir / "Решение.txt"
    assert target.read_bytes() == "Суд постановил.\n".encode("utf-8")
    assert factory.call_args.args == ("s3",)
    kwargs = factory.call_args.kwargs
    assert kwargs["endpoint_url"] == "https://storage.yandexcloud.net"
    assert kwargs["region_name"] == "ru-central1"
    assert kwargs["aws_access_key_id"] == credentials[0]
    assert kwargs["aws_secret_access_key"] == credentials[1]
    config = kwargs["config"]
    assert config.signature_version == "s3v4"
    assert config.s3 == {"addressing_style": "path"}
    assert (config.connect_timeout, config.read_timeout) == (10, 60)
    assert config.retries == {"total_max_attempts": 1}
    instance.get_object.assert_called_once_with(
        Bucket="test-corpus", Key="Судебные решения/Решение.txt",
    )
    assert client.read(target.name) == result
    assert instance.get_object.call_count == 1
    instance.close.assert_called_once_with()
    instance.list_objects_v2.assert_not_called()
    assert not list(target.parent.glob("*.part"))


def test_incomplete_stream_is_closed_removed_and_next_read_can_retry(client, sdk):
    instance, _, _ = sdk
    raw = BytesIO(b"incomplete")
    instance.get_object.side_effect = None
    instance.get_object.return_value = {"Body": StreamingBody(raw, 100)}
    with pytest.raises(CloudConnectionError):
        client.read("case.txt")
    assert raw.closed
    assert not client.exists("case.txt")
    assert list(client.settings.storage_dir.iterdir()) == []
    instance.get_object.side_effect = lambda **kw: object_response(b"complete")
    assert client.read("case.txt") == {"case.txt": "complete"}


@pytest.mark.parametrize("code,status,public_error", [
    ("InvalidAccessKeyId", 403, CloudAuthError),
    ("SignatureDoesNotMatch", 403, CloudAuthError),
    ("ExpiredToken", 400, CloudAuthError),
    ("AccessDenied", 403, CloudPermissionError),
    ("NoSuchBucket", 404, CloudFolderError),
    ("InvalidBucketName", 400, CloudFolderError),
    ("SlowDown", 503, CloudError),
])
def test_cloud_errors_are_classified_and_sanitized(
    client, sdk, code, status, public_error, caplog, capsys,
):
    instance, _, secrets = sdk
    instance.get_object.side_effect = sdk_error(code, " ".join(secrets), status)
    with pytest.raises(public_error) as caught:
        client.read("case.txt")
    diagnostic = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    output = capsys.readouterr()
    for secret in secrets:
        assert secret not in diagnostic + caplog.text + output.out + output.err
    assert not client.exists("case.txt")
    assert list(client.settings.storage_dir.iterdir()) == []


@pytest.mark.parametrize("error_type", [EndpointConnectionError, ReadTimeoutError])
def test_network_errors_hide_endpoint_and_credentials(client, sdk, error_type):
    instance, _, secrets = sdk
    instance.get_object.side_effect = error_type(endpoint_url=" ".join(secrets))
    with pytest.raises(CloudConnectionError) as caught:
        client.read("case.txt")
    diagnostic = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert all(secret not in diagnostic for secret in secrets)
    assert list(client.settings.storage_dir.iterdir()) == []


def test_missing_credentials_do_not_fall_back_to_environment(client, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    with pytest.raises(MissingCredentialsError, match="AWS_ACCESS_KEY_ID"):
        client.read("case.txt")
    assert MissingTokenError is MissingCredentialsError
    assert list(client.settings.storage_dir.iterdir()) == []


def test_malformed_sdk_response_is_sanitized_and_client_is_closed(client, sdk):
    instance, _, secrets = sdk
    instance.get_object.side_effect = ResponseParserError(" ".join(secrets))
    with pytest.raises(CloudError) as caught:
        client.read("case.txt")
    diagnostic = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert all(secret not in diagnostic for secret in secrets)
    assert list(client.settings.storage_dir.iterdir()) == []
    instance.close.assert_called_once_with()


@pytest.mark.parametrize("content", [
    "AWS_ACCESS_KEY_ID=one\n",
    "AWS_SECRET_ACCESS_KEY=two\n",
    "AWS_ACCESS_KEY_ID=one\nAWS_SECRET_ACCESS_KEY=   \n",
    "YANDEX_OAUTH_TOKEN=legacy\n",
])
def test_partial_and_legacy_credentials_are_not_accepted(client, content):
    Path(os.environ["ENV_PATH"]).write_text(content, encoding="utf-8")
    with pytest.raises(MissingCredentialsError):
        load_credentials()


def test_credentials_use_selected_file_without_interpolation_or_environment_changes(
    tmp_path, monkeypatch,
):
    selected = tmp_path / "selected.env"
    selected.write_text(
        "AWS_ACCESS_KEY_ID=selected-id\nAWS_SECRET_ACCESS_KEY=$" "{SENTEXTS_TEST_VALUE}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_PATH", str(selected))
    monkeypatch.setenv("SENTEXTS_TEST_VALUE", "must-not-expand")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-change")
    assert load_credentials() == ("selected-id", "$" "{SENTEXTS_TEST_VALUE}")
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "must-not-change"


def test_explicit_credentials_file_overrides_env_path(tmp_path):
    selected = tmp_path / "selected.env"
    selected.write_text(
        "AWS_ACCESS_KEY_ID=selected-id\nAWS_SECRET_ACCESS_KEY=selected-secret\n",
        encoding="utf-8",
    )
    assert load_credentials(selected) == ("selected-id", "selected-secret")


def test_invalid_credentials_encoding_is_sanitized(client, credentials):
    secret = credentials[1]
    Path(os.environ["ENV_PATH"]).write_bytes(secret.encode() + b"\xff")
    with pytest.raises(ConfigError) as caught:
        load_credentials()
    diagnostic = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert secret not in diagnostic


@pytest.mark.parametrize("folder,bucket,key", [
    ("s3://test-corpus", "test-corpus", "one.txt"),
    ("s3://test-corpus/", "test-corpus", "one.txt"),
    ("s3://test-corpus/Решения/", "test-corpus", "Решения/one.txt"),
    ("s3://other-corpus/Решения 2026", "other-corpus", "Решения 2026/one.txt"),
    ("s3://test-corpus/raw%20#?data", "test-corpus", "raw%20#?data/one.txt"),
])
def test_selected_bucket_and_literal_prefix_are_used(client, sdk, folder, bucket, key):
    client.update_settings(cloud_folder=folder)
    client.read("one.txt")
    sdk[0].get_object.assert_called_once_with(Bucket=bucket, Key=key)


def test_missing_download_parent_keeps_original_error(client):
    with pytest.raises(FileNotFoundError):
        download(
            "one.txt", client.settings.storage_dir / "missing" / "one.txt",
            "s3://test-corpus/texts",
        )


def test_failed_publication_does_not_leave_ready_copy(client, sdk, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("cannot publish file")

    monkeypatch.setattr("sentexts.cloud.os.replace", denied)
    with pytest.raises(PermissionError):
        client.read("case.txt")
    assert list(client.settings.storage_dir.iterdir()) == []


def test_failed_download_preserves_existing_destination(client, sdk):
    target = client.settings.storage_dir / "case.txt"
    target.write_bytes(b"original")
    sdk[0].get_object.side_effect = sdk_error("AccessDenied")
    with pytest.raises(CloudPermissionError):
        download(target.name, target, client.settings.cloud_folder)
    assert target.read_bytes() == b"original"
    assert list(target.parent.iterdir()) == [target]


def test_missing_file_warns_without_listing_prefix_or_leaking_response(client, sdk, caplog):
    instance, _, secrets = sdk
    instance.get_object.side_effect = sdk_error("NoSuchKey", " ".join(secrets), 404)
    with pytest.warns(CloudFileNotFoundWarning, match="missing.txt") as caught:
        assert client.read("missing.txt") == {}
    assert list(client.settings.storage_dir.iterdir()) == []
    assert all(secret not in str(caught[0].message) + caplog.text for secret in secrets)
    instance.list_objects_v2.assert_not_called()


def test_missing_file_does_not_interrupt_batch(client, sdk):
    instance, _, _ = sdk

    def read_cloud(**kwargs):
        if kwargs["Key"].endswith("/missing.txt"):
            raise sdk_error("NoSuchKey", status=404)
        return object_response()

    instance.get_object.side_effect = read_cloud
    with pytest.warns(CloudFileNotFoundWarning):
        result = client.read(["first.txt", "missing.txt", "last.txt"])
    assert result == {"first.txt": "available", "last.txt": "available"}
    assert instance.get_object.call_count == 3
    assert not client.exists("missing.txt")


def test_missing_file_is_retried_on_next_read(client, sdk):
    instance, _, _ = sdk
    instance.get_object.side_effect = sdk_error("NoSuchKey", status=404)
    with pytest.warns(CloudFileNotFoundWarning):
        assert client.read("later.txt") == {}
    instance.get_object.side_effect = lambda **kw: object_response(b"now present")
    assert client.read("later.txt") == {"later.txt": "now present"}


def test_warning_as_error_does_not_expose_sdk_exception(client, sdk):
    instance, _, secrets = sdk
    instance.get_object.side_effect = sdk_error("NoSuchKey", " ".join(secrets), 404)
    with warnings.catch_warnings():
        warnings.simplefilter("error", CloudFileNotFoundWarning)
        with pytest.raises(CloudFileNotFoundWarning) as caught:
            client.read("missing.txt")
    diagnostic = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert all(secret not in diagnostic for secret in secrets)
    assert caught.value.__context__ is None
    assert list(client.settings.storage_dir.iterdir()) == []


def test_s3_download_uses_real_sdk_contract_without_network(client, credentials, monkeypatch):
    payload = b"SDK contract\n"
    s3 = boto3.client(
        "s3", endpoint_url="https://storage.yandexcloud.net",
        region_name="ru-central1",
        aws_access_key_id=credentials[0], aws_secret_access_key=credentials[1],
        config=Config(signature_version="s3v4"),
    )
    with Stubber(s3) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(BytesIO(payload), len(payload)),
             "ContentLength": len(payload)},
            {"Bucket": "test-corpus", "Key": "Судебные решения/Решение 1.TXT"},
        )
        monkeypatch.setattr("sentexts.cloud.boto3.client", lambda *a, **kw: s3)
        assert client.read("Решение 1.TXT") == {"Решение 1.TXT": "SDK contract\n"}
        stubber.assert_no_pending_responses()
