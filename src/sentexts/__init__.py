"""Create missing local templates and expose the public Sentexts class."""

import json
import os
from pathlib import Path

_default_root = Path(__file__).resolve().parent.parent
os.environ.setdefault("CONFIG_PATH", str(_default_root / "config.json"))
_config_path = Path(os.environ["CONFIG_PATH"]).expanduser().resolve()
os.environ.setdefault("ENV_PATH", str(_config_path.parent / ".env"))

# Exclusive creation preserves existing configuration and credential files.
for _path, _contents in (
    (_config_path, json.dumps({"storage_dir": None, "cloud_folder": None}, indent=2) + "\n"),
    (Path(os.environ["ENV_PATH"]).expanduser(), "# Enter your personal S3 keys with the required permissions; never commit this file.\nAWS_ACCESS_KEY_ID=\nAWS_SECRET_ACCESS_KEY=\n"),
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

from .api import Sentexts

__all__ = ["Sentexts"]
