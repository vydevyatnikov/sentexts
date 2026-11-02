"""Two non-secret settings, validated updates and explicit JSON persistence."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import tempfile
import warnings

from platformdirs import user_config_path


class ConfigError(ValueError):
    """The JSON configuration or a setting is invalid."""


def atomic_replacement(target: Path, data: str) -> None:
    """Replace a file only after its complete UTF-8 contents have been written."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            prefix=".sentexts-config-", dir=target.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _storage_directory(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ConfigError("storage_dir must be a non-empty directory path.")
    try:
        path = Path(value).expanduser().resolve()
    except (ValueError, RuntimeError):
        raise ConfigError("storage_dir is not a valid path.") from None
    if not path.is_dir():
        raise ConfigError("storage_dir must refer to an existing directory.")
    return path


DEFAULT_CLOUD_FOLDER = "s3://sentexts-corpus-73fa/texts/"


def split_cloud_folder(value: str) -> tuple[str, str]:
    """Parse an S3 bucket/prefix string; object keys remain literal Unicode."""
    if not isinstance(value, str) or not value.startswith("s3://"):
        raise ConfigError("cloud_folder must use s3://bucket/prefix.")
    bucket, _, prefix = value[5:].partition("/")
    if (
        re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) is None
        or ".." in bucket
        or "\\" in prefix
        or any(ord(character) < 32 for character in prefix)
        or any(part in (".", "..") for part in prefix.split("/"))
    ):
        raise ConfigError("cloud_folder must contain a valid S3 bucket and prefix.")
    return bucket, prefix.rstrip("/")


def _cloud_folder(value: str) -> str:
    split_cloud_folder(value)
    return value


class Settings:
    """Runtime settings; update() validates everything before changing either field."""

    def __init__(self, settings_path: str | Path | None = None) -> None:
        if settings_path is None:
            settings_path = user_config_path("sentexts", appauthor=False)
        directory = Path(settings_path).expanduser().resolve()
        self.config_path = directory / "config.json"
        self.env_path = directory / ".env"

        for _path, _contents in (
                (self.config_path, json.dumps({"storage_dir": None, "cloud_folder": None}, indent=2) + "\n"),
                (self.env_path,
                 "# Enter your personal S3 keys with the required permissions; never commit this file.\nAWS_ACCESS_KEY_ID=\nAWS_SECRET_ACCESS_KEY=\n"),
        ):
            _path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with os.fdopen(
                        os.open(_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                        "w", encoding="utf-8", newline="\n",
                ) as _stream:
                    _stream.write(_contents)
            except FileExistsError:
                pass

        self.storage_dir, self.cloud_folder = self.load_config()

    def load_config(self) -> tuple[Path, str]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise ConfigError("Configuration must contain valid UTF-8 JSON.") from None
        if not isinstance(data, dict) or set(data) != {"storage_dir", "cloud_folder"}:
            raise ConfigError("Configuration requires exactly storage_dir and cloud_folder.")

        folder = data["cloud_folder"]
        if folder is None:
            folder = DEFAULT_CLOUD_FOLDER
            warnings.warn(f"cloud_folder is not set; using {DEFAULT_CLOUD_FOLDER}.", stacklevel=2)
        folder = _cloud_folder(folder)

        directory = data["storage_dir"]
        if directory is None:
            directory = Path("texts").resolve()
            directory.mkdir(parents=True, exist_ok=True)
            warnings.warn("storage_dir is not set; using ./texts.", stacklevel=2)
        return _storage_directory(directory), folder

    def update(self, **changes) -> Settings:
        if set(changes) - {"storage_dir", "cloud_folder"}:
            raise ConfigError("Only storage_dir and cloud_folder can be updated.")
        directory = _storage_directory(changes.get("storage_dir", self.storage_dir))
        folder = _cloud_folder(changes.get("cloud_folder", self.cloud_folder))
        self.storage_dir, self.cloud_folder = directory, folder
        return self

    def save_config(self) -> Path:
        """Explicitly persist the current settings; keep the original on write failure."""
        directory = _storage_directory(self.storage_dir)
        folder = _cloud_folder(self.cloud_folder)
        data = json.dumps(
            {"storage_dir": str(directory), "cloud_folder": folder},
            ensure_ascii=False, indent=2,
        ) + "\n"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replacement(self.config_path, data)
        return self.config_path
