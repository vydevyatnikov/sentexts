from pathlib import Path

import pytest

from sentexts import Sentexts
from sentexts.storage import UnsafePathError


def populate(client, names):
    paths = [client.settings.storage_dir / name for name in names]
    for path in paths:
        path.write_text("text", encoding="utf-8")
    return paths


def test_cleanup_selected_deduplicates_skips_missing_and_returns_paths(client):
    first, second = populate(client, ["first.txt", "second.txt"])
    result = client.cleanup(iter([first.name, first.name, "missing.txt"]))
    assert result == [first]
    assert not first.exists() and second.exists()
    assert client.cleanup([first.name]) == []


def test_empty_list_is_not_all_files(client):
    paths = populate(client, ["a.txt", "b.txt"])
    assert client.cleanup([]) == []
    assert all(path.exists() for path in paths)


def test_cleanup_all_only_selects_txt_without_recursion(client):
    txt = populate(client, ["a.txt", "b.TXT"])
    other = client.settings.storage_dir / "readme.md"
    other.write_text("keep", encoding="utf-8")
    nested = client.settings.storage_dir / "nested.txt"
    nested.mkdir()
    (nested / "child.txt").write_text("keep", encoding="utf-8")
    assert client.cleanup() == txt
    assert other.exists() and (nested / "child.txt").exists()
    assert client.cleanup() == []


def test_entire_request_is_validated_before_deletion(client, tmp_path):
    first, second = populate(client, ["a.txt", "b.txt"])
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    with pytest.raises(UnsafePathError):
        client.cleanup([first.name, "z/escape.txt", second.name])
    assert first.exists() and second.exists() and outside.exists()


def test_bare_string_is_rejected_without_deletion(client):
    (path,) = populate(client, ["one.txt"])
    with pytest.raises(TypeError):
        client.cleanup(path.name)
    assert path.exists()


def test_symlinks_are_skipped_and_external_target_is_preserved(client, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = client.settings.storage_dir / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert client.cleanup([link.name]) == []
    assert client.cleanup() == []
    assert link.is_symlink() and outside.read_text(encoding="utf-8") == "private"


def test_initialization_and_read_never_clean_files(client):
    (path,) = populate(client, ["old.txt"])
    restored = Sentexts()
    assert restored.read(path.name) == {path.name: "text"}
    assert path.exists()


def test_os_error_stops_cleanup_without_hiding_it(client, monkeypatch):
    first, second, third = populate(client, ["a.txt", "b.txt", "c.txt"])
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == second:
            raise PermissionError("cannot delete")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(PermissionError):
        client.cleanup([first.name, second.name, third.name])
    assert not first.exists() and second.exists() and third.exists()
