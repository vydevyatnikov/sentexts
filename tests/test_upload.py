from pathlib import Path
import traceback

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.parsers import ResponseParserError
from botocore.stub import ANY, Stubber
import pytest

from sentexts.cloud import (
    CloudAuthError, CloudConnectionError, CloudError, CloudFileExistsError,
    CloudFolderError, CloudPermissionError, MissingCredentialsError,
)
from sentexts.storage import UnsafePathError


def local_file(tmp_path, name="Решение 1.TXT", payload=b"original\r\n"):
    source = tmp_path / name
    source.write_bytes(payload)
    return source


def failure(code, message="", status=403):
    return ClientError({
        "Error": {"Code": code, "Message": message},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }, "PutObject")


def test_upload_streams_bytes_with_atomic_no_overwrite_and_keeps_local_data(client, sdk, tmp_path):
    source = local_file(tmp_path)
    instance, factory, credentials = sdk
    received = []

    def receive(**kwargs):
        assert not isinstance(kwargs["Body"], (bytes, bytearray))
        received.append((kwargs, kwargs["Body"].read()))
        return {"ETag": "test"}

    instance.put_object.side_effect = receive
    assert client.upload(source) == {
        source.name: "s3://test-corpus/Судебные решения/Решение 1.TXT",
    }
    parameters, payload = received[0]
    assert payload == source.read_bytes() == b"original\r\n"
    assert parameters["Body"].closed
    assert parameters["Bucket"] == "test-corpus"
    assert parameters["Key"] == "Судебные решения/Решение 1.TXT"
    assert parameters["IfNoneMatch"] == "*"
    assert parameters["ContentLength"] == len(payload)
    assert parameters["ContentType"] == "text/plain"
    assert factory.call_args.kwargs["aws_access_key_id"] == credentials[0]
    assert factory.call_args.kwargs["aws_secret_access_key"] == credentials[1]
    instance.head_object.assert_not_called()
    instance.get_object.assert_not_called()
    instance.close.assert_called_once_with()
    assert client.list_local_files() == []


def test_batch_uses_one_client_deduplicates_same_path_and_preserves_order(client, sdk, tmp_path):
    first = local_file(tmp_path, "first.txt")
    second = local_file(tmp_path, "second.txt")
    result = client.upload(iter([first, first, str(second)]))
    assert list(result) == ["first.txt", "second.txt"]
    assert sdk[0].put_object.call_count == 2
    sdk[1].assert_called_once()
    sdk[0].close.assert_called_once_with()


def test_relative_upload_path_is_relative_to_working_directory(client, sdk, tmp_path, monkeypatch):
    source = local_file(tmp_path, "one.txt")
    monkeypatch.chdir(tmp_path)
    client.upload("one.txt")
    assert sdk[0].put_object.call_args.kwargs["Key"].endswith("/one.txt")
    assert source.exists()


def test_upload_uses_current_bucket_and_prefix(client, sdk, tmp_path):
    client.update_settings(cloud_folder="s3://other-corpus/decisions/")
    source = local_file(tmp_path)
    assert client.upload(source)[source.name] == "s3://other-corpus/decisions/Решение 1.TXT"
    assert sdk[0].put_object.call_args.kwargs["Bucket"] == "other-corpus"


def test_overwrite_requires_explicit_true_and_preserves_cached_copy(client, sdk, tmp_path):
    source = local_file(tmp_path, "one.txt", b"new cloud content")
    cached = client.settings.storage_dir / source.name
    cached.write_bytes(b"old cache")
    client.upload(source, overwrite=True)
    assert "IfNoneMatch" not in sdk[0].put_object.call_args.kwargs
    assert client.read(source.name) == {source.name: "old cache"}
    sdk[0].get_object.assert_not_called()
    assert source.read_bytes() == b"new cloud content"


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_non_boolean_overwrite_is_rejected_before_network(client, sdk, tmp_path, value):
    source = local_file(tmp_path)
    with pytest.raises(TypeError):
        client.upload(source, overwrite=value)
    sdk[1].assert_not_called()


def test_empty_batch_needs_no_credentials_or_network(client):
    assert client.upload([]) == {}


@pytest.mark.parametrize("invalid", ["bad.pdf", "CON.txt", "missing.txt"])
def test_all_paths_are_validated_before_first_upload(client, sdk, tmp_path, invalid):
    source = local_file(tmp_path, "first.txt")
    invalid_path = tmp_path / invalid
    if invalid != "missing.txt":
        invalid_path.write_bytes(b"not for upload")
    with pytest.raises((UnsafePathError, FileNotFoundError)):
        client.upload([source, invalid_path])
    sdk[1].assert_not_called()
    assert source.read_bytes() == b"original\r\n"


def test_duplicate_names_from_different_directories_reject_entire_batch(client, sdk, tmp_path):
    source = local_file(tmp_path, "one.txt")
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    other = local_file(other_directory, "one.txt")
    with pytest.raises(ValueError, match="unique filenames"):
        client.upload([source, other])
    sdk[1].assert_not_called()


def test_directory_is_not_uploaded(client, sdk, tmp_path):
    directory = tmp_path / "directory.txt"
    directory.mkdir()
    with pytest.raises(UnsafePathError):
        client.upload(directory)
    sdk[1].assert_not_called()


def test_symlink_is_not_uploaded(client, sdk, tmp_path):
    source = local_file(tmp_path)
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(UnsafePathError):
        client.upload(link)
    sdk[1].assert_not_called()


def test_size_limit_validates_whole_batch_without_allocating_large_files(
    client, sdk, tmp_path, monkeypatch,
):
    monkeypatch.setattr("sentexts.storage.MAX_UPLOAD_SIZE", 4)
    first = local_file(tmp_path, "first.txt", b"123")
    second = local_file(tmp_path, "second.txt", b"12345")
    with pytest.raises(ValueError, match="5 GiB"):
        client.upload([first, second])
    sdk[1].assert_not_called()


@pytest.mark.parametrize("value", [b"one.txt", [b"one.txt"], [None]])
def test_invalid_path_types_rejected_without_network(client, sdk, value):
    with pytest.raises(TypeError):
        client.upload(value)
    sdk[1].assert_not_called()


def test_missing_credentials_preserve_local_file(client, tmp_path):
    source = local_file(tmp_path)
    with pytest.raises(MissingCredentialsError):
        client.upload(source)
    assert source.read_bytes() == b"original\r\n"


@pytest.mark.parametrize("code,status,public_error", [
    ("AccessDenied", 403, CloudPermissionError),
    ("SignatureDoesNotMatch", 403, CloudAuthError),
    ("NoSuchBucket", 404, CloudFolderError),
    ("PreconditionFailed", 412, CloudFileExistsError),
    ("ConditionalRequestConflict", 409, CloudError),
])
def test_upload_errors_are_sanitized_and_do_not_retry_with_another_key(
    client, sdk, tmp_path, code, status, public_error, caplog, capsys,
):
    source = local_file(tmp_path)
    instance, factory, secrets = sdk
    streams = []

    def denied(**kwargs):
        streams.append(kwargs["Body"])
        raise failure(code, " ".join(secrets), status)

    instance.put_object.side_effect = denied
    with pytest.raises(public_error) as caught:
        client.upload(source)
    output = capsys.readouterr()
    diagnostic = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert all(secret not in diagnostic + caplog.text + output.out + output.err for secret in secrets)
    instance.put_object.assert_called_once()
    factory.assert_called_once()
    assert streams[0].closed
    instance.close.assert_called_once_with()
    assert source.read_bytes() == b"original\r\n"


@pytest.mark.parametrize("error_type,public_error", [
    (ReadTimeoutError, CloudConnectionError),
    (ResponseParserError, CloudError),
])
def test_sdk_failures_are_sanitized_and_stop_batch(client, sdk, tmp_path, error_type, public_error):
    first = local_file(tmp_path, "first.txt")
    second = local_file(tmp_path, "second.txt")
    third = local_file(tmp_path, "third.txt")
    secrets = sdk[2]
    error = (
        error_type(endpoint_url=" ".join(secrets))
        if error_type is ReadTimeoutError else error_type(" ".join(secrets))
    )
    sdk[0].put_object.side_effect = [{"ETag": "first"}, error]
    with pytest.raises(public_error) as caught:
        client.upload([first, second, third])
    diagnostic = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert all(secret not in diagnostic for secret in secrets)
    assert sdk[0].put_object.call_count == 2
    assert all(path.exists() for path in (first, second, third))


def test_local_open_error_is_not_hidden(client, sdk, tmp_path, monkeypatch):
    source = local_file(tmp_path)
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError("cannot read source")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(PermissionError, match="cannot read source"):
        client.upload(source)
    sdk[0].put_object.assert_not_called()
    sdk[0].close.assert_called_once_with()


def test_conditional_put_uses_real_sdk_contract_without_network(client, credentials, tmp_path, monkeypatch):
    source = local_file(tmp_path, "one.txt", b"SDK upload")
    s3 = boto3.client(
        "s3", endpoint_url="https://storage.yandexcloud.net", region_name="ru-central1",
        aws_access_key_id=credentials[0], aws_secret_access_key=credentials[1],
        config=Config(signature_version="s3v4"),
    )
    with Stubber(s3) as stubber:
        stubber.add_response(
            "put_object", {"ETag": '"test"'},
            {"Bucket": "test-corpus", "Key": "Судебные решения/one.txt",
             "Body": ANY, "ContentLength": 10, "ContentType": "text/plain",
             "IfNoneMatch": "*"},
        )
        monkeypatch.setattr("sentexts.cloud.boto3.client", lambda *a, **kw: s3)
        assert client.upload(source) == {"one.txt": "s3://test-corpus/Судебные решения/one.txt"}
        stubber.assert_no_pending_responses()
