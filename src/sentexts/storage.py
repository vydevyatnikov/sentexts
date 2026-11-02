"""A dedicated text directory with cached reads and explicit manual deletion."""

from __future__ import annotations

from pathlib import Path
import stat
from typing import Iterable

from .cloud import MAX_UPLOAD_SIZE, download, upload


class UnsafePathError(ValueError):
    """A filename or filesystem object is unsafe for text storage."""


def validate_filename(filename: str, *, allow_stem: bool = False) -> str:
    """Validate a portable .txt basename, optionally accepting an extensionless name."""
    is_text = isinstance(filename, str) and filename.lower().endswith(".txt")
    if (
        not isinstance(filename, str)
        or not filename
        or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in filename)
        or filename.endswith((" ", "."))
        or (not is_text and (not allow_stem or Path(filename).suffix))
        or not (filename[:-4] if is_text else filename).strip(". ")
        or filename.split(".")[0].rstrip(" ").upper()
        in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
            *(f"COM{i}" for i in "¹²³"), *(f"LPT{i}" for i in "¹²³")}
    ):
        raise UnsafePathError("Use a single portable .txt filename without path components.")
    return filename


def upload_files(
    paths: str | Path | Iterable[str | Path], folder: str, *, overwrite: bool = False,
) -> dict[str, str]:
    """Validate an explicit batch before uploading, without changing local data."""
    if not isinstance(overwrite, bool):
        raise TypeError("overwrite must be True or False.")
    if isinstance(paths, (str, Path)):
        paths = [paths]
    elif isinstance(paths, (bytes, bytearray)):
        raise TypeError("Pass local file paths, not bytes.")
    selected: dict[str, Path] = {}
    for value in paths:
        if not isinstance(value, (str, Path)):
            raise TypeError("Each upload source must be a string or pathlib.Path.")
        source = Path(value).expanduser().absolute()
        name = validate_filename(source.name)
        if source.is_symlink():
            raise UnsafePathError("Refusing to upload a symbolic link.")
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePathError("Upload sources must be regular .txt files.")
        if info.st_size > MAX_UPLOAD_SIZE:
            raise ValueError("A single upload is limited to 5 GiB.")
        if name in selected and selected[name] != source:
            raise ValueError("Different upload sources must have unique filenames.")
        selected[name] = source
    if not selected:
        return {}
    return upload(selected.values(), folder, overwrite=overwrite)


class LocalStorage:
    """For one caller at a time; do not modify the directory concurrently."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().absolute()

    def path(self, filename: str) -> Path:
        if self.root.is_symlink():
            raise UnsafePathError("Storage directory was replaced by a symlink.")
        return self.root / validate_filename(filename)

    def exists(self, filename: str) -> bool:
        return self.path(filename).exists()

    def list_local_files(self) -> list[str]:
        if self.root.is_symlink():
            raise UnsafePathError("Storage directory was replaced by a symlink.")
        return sorted(
            path.name for path in self.root.iterdir()
            if path.suffix.lower() == ".txt" and not path.is_symlink() and path.is_file()
        )

    def read(self, filenames: Iterable[str], folder: str, *, encoding: str = "utf-8") -> dict[str, str]:
        texts = {}
        for filename in filenames:
            filename = validate_filename(filename, allow_stem=True)
            if not filename.lower().endswith(".txt"):
                filename += ".txt"
            path = self.path(filename)
            if path.is_symlink():
                raise UnsafePathError("Refusing to read or overwrite a symbolic link.")
            if not path.exists():
                if not download(filename, path, folder):
                    continue
            if not path.is_file():
                raise UnsafePathError("The local text is not a regular file.")
            # Decode directly to preserve original CRLF/LF line endings.
            texts[filename] = path.read_bytes().decode(encoding, errors="strict")
        return texts

    def cleanup(self, filenames: Iterable[str] | None = None) -> list[Path]:
        """Delete only on demand, validating the entire request before any deletion.

        None selects all regular .txt files. An empty list selects nothing.
        Duplicate/missing names and non-regular objects are skipped.
        """
        if isinstance(filenames, (str, bytes)):
            raise TypeError("Pass an iterable of filenames, not a single string.")
        names = self.list_local_files() if filenames is None else filenames
        paths = [self.path(name) for name in sorted({validate_filename(n) for n in names})]
        removed = []
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed.append(path)
        return removed
