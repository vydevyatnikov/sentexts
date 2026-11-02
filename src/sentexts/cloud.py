"""Synchronous downloads and explicit uploads through Yandex Object Storage."""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import tempfile
from typing import Iterable
import warnings

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.parsers import ResponseParserError
from dotenv import dotenv_values

from .config import ConfigError, split_cloud_folder


class CloudError(RuntimeError):
    """A cloud operation failed; diagnostics never include SDK response text."""


class MissingCredentialsError(CloudError):
    """The selected .env does not contain a complete pair of S3 credentials."""


# Keep existing exception handlers working when upgrading from Yandex.Disk.
MissingTokenError = MissingCredentialsError


class CloudAuthError(CloudError):
    """Object Storage rejected authentication."""


class CloudPermissionError(CloudError):
    """The account does not have permission to perform the requested operation."""


class CloudFolderError(CloudError):
    """The configured bucket is missing or has an invalid name."""


class CloudFileNotFoundWarning(UserWarning):
    """An object is missing under the configured prefix and will be skipped."""


class CloudConnectionError(CloudError):
    """The SDK could not complete the network request or response stream."""


class CloudFileExistsError(CloudError):
    """A conditional upload refused to replace an existing object."""


MAX_UPLOAD_SIZE = 5 * 1024 ** 3


def load_credentials(env_path: str | Path | None = None) -> tuple[str, str]:
    """Read only the selected .env, without interpolation or ambient credentials."""
    if env_path is None:
        default = Path(__file__).resolve().parent / ".env"
        env_path = os.environ.get("ENV_PATH", default)
    try:
        values = dotenv_values(Path(env_path).expanduser(), interpolate=False)
    except UnicodeError:
        raise ConfigError("The selected .env must contain valid UTF-8 text.") from None
    access = values.get("AWS_ACCESS_KEY_ID")
    secret = values.get("AWS_SECRET_ACCESS_KEY")
    if not all(isinstance(value, str) and value.strip() for value in (access, secret)):
        raise MissingCredentialsError(
            "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the selected .env file."
        )
    return access.strip(), secret.strip()


def _client():
    access, secret = load_credentials()
    return boto3.client(
        "s3",
        endpoint_url="https://storage.yandexcloud.net",
        region_name="ru-central1",
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=10,
            read_timeout=60,
            retries={"total_max_attempts": 1},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _client_failure(error: ClientError, operation: str) -> CloudError:
    """Classify a response without copying its message, URL or credentials."""
    code = error.response.get("Error", {}).get("Code", "")
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if code in {
        "InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidSecurity",
        "InvalidToken", "ExpiredToken", "RequestTimeTooSkewed",
    }:
        return CloudAuthError(
            "Object Storage rejected authentication; check the key pair and system clock."
        )
    if code in {"NoSuchBucket", "InvalidBucketName"}:
        return CloudFolderError(
            "The configured Object Storage bucket is missing or has an invalid name."
        )
    if code == "AccessDenied" or status == 403:
        return CloudPermissionError(
            "Object Storage denied the operation; check this key's permissions."
        )
    if operation == "upload" and status == 412:
        return CloudFileExistsError(
            "The object already exists; use overwrite=True to replace it explicitly."
        )
    return CloudError(f"Object Storage could not complete the {operation}.")


def download(filename: str, destination: Path, folder: str) -> bool:
    """Stream one object into a temporary file, publishing only a complete copy.

    The storage layer validates the basename. SDK exception messages and chained
    tracebacks may contain secrets, so expose only fixed, classified diagnostics.
    Prefixes are parts of object keys, not independent S3 directory resources.
    """
    bucket, prefix = split_cloud_folder(folder)
    key = f"{prefix}/{filename}" if prefix else filename
    destination = Path(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".sentexts-download-", suffix=".part",
            dir=destination.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            missing_file = False
            with closing(_client()) as client:
                try:
                    response = client.get_object(Bucket=bucket, Key=key)
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != "NoSuchKey":
                        raise
                    missing_file = True
                else:
                    body = response["Body"]
                    try:
                        for chunk in body.iter_chunks(chunk_size=64 * 1024):
                            stream.write(chunk)
                    finally:
                        body.close()
            if missing_file:
                # Raise warnings outside the SDK handler to keep warning-as-error
                # tracebacks free of the original exception and its response.
                warnings.warn(
                    f"File {filename!r} was not found in the cloud folder; skipping.",
                    CloudFileNotFoundWarning, stacklevel=2,
                )
                return False
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return True
    except ClientError as error:
        raise _client_failure(error, "download") from None
    except ResponseParserError:
        raise CloudError("Object Storage returned an invalid response.") from None
    except BotoCoreError:
        raise CloudConnectionError(
            "Object Storage request failed, timed out, or returned an incomplete stream."
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def upload(sources: Iterable[Path], folder: str, *, overwrite: bool = False) -> dict[str, str]:
    """Stream an already validated batch to S3 using a single SDK client.

    The storage layer validates local paths and basenames. The no-overwrite
    condition is checked atomically by S3, without a preliminary HEAD request.
    Local files and the reader's cache are not changed.
    """
    if not isinstance(overwrite, bool):
        raise TypeError("overwrite must be True or False.")
    bucket, prefix = split_cloud_folder(folder)
    uploaded = {}
    try:
        with closing(_client()) as client:
            for source in sources:
                key = f"{prefix}/{source.name}" if prefix else source.name
                with source.open("rb") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    if size > MAX_UPLOAD_SIZE:
                        raise ValueError("A single upload is limited to 5 GiB.")
                    parameters = {
                        "Bucket": bucket, "Key": key, "Body": stream,
                        "ContentLength": size, "ContentType": "text/plain",
                    }
                    if not overwrite:
                        parameters["IfNoneMatch"] = "*"
                    client.put_object(**parameters)
                uploaded[source.name] = f"s3://{bucket}/{key}"
        return uploaded
    except ClientError as error:
        raise _client_failure(error, "upload") from None
    except ResponseParserError:
        raise CloudError("Object Storage returned an invalid response.") from None
    except BotoCoreError:
        raise CloudConnectionError("Object Storage upload failed or timed out.") from None
