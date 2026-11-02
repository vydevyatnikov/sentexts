"""Public interface for cached reads, explicit S3 uploads and manual cleanup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable
from getpass import getpass
from dotenv import set_key

from .config import Settings
from .storage import LocalStorage, upload_files


class Sentexts:
    """Use the selected local text directory and download only missing copies."""

    def __init__(self):
        self._settings = Settings()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def _storage(self) -> LocalStorage:
        # Storage has no state other than its root; always use current settings.
        return LocalStorage(self._settings.storage_dir)

    def update_settings(self, **changes) -> None:
        """Validate a whole update and apply it in memory, without saving JSON."""
        self._settings.update(**changes)

    def save_settings(self) -> Path:
        return self._settings.save_config()

    @staticmethod
    def update_env() -> None:
        access_key = getpass(prompt="Новый Access key (символы не будут видны):")
        secret_key = getpass(prompt="Новый Secret Access key (символы не будут видны):")
        if not access_key or not secret_key:
            raise ValueError('Ключи не должны быть пустыми')

        env_pat = Path(os.environ.get("ENV_PATH"))
        set_key(env_pat, 'AWS_ACCESS_KEY_ID', access_key)
        set_key(env_pat, 'AWS_SECRET_ACCESS_KEY', secret_key)

        print('Ключи сохранены')

    def exists(self, filename: str) -> bool:
        return self._storage.exists(filename)

    def read(self, filenames: str | Iterable[str], *, encoding: str = "utf-8") -> dict[str, str]:
        """Read full .txt names or stems; warn and skip missing cloud objects."""
        if isinstance(filenames, str):
            filenames = [filenames]
        return self._storage.read(filenames, self.settings.cloud_folder, encoding=encoding)

    def list_local_files(self) -> list[str]:
        return self._storage.list_local_files()

    def upload(
        self, paths: str | Path | Iterable[str | Path], *, overwrite: bool = False,
    ) -> dict[str, str]:
        """Upload local .txt paths to cloud_folder using the selected key pair."""
        return upload_files(paths, self.settings.cloud_folder, overwrite=overwrite)

    def cleanup(self, filenames: Iterable[str] | None = None) -> list[Path]:
        """Manually delete selected texts, or all local texts when no list is supplied."""
        return self._storage.cleanup(filenames)
