import pytest

from sentexts.storage import UnsafePathError


@pytest.mark.parametrize("name", [
    "../escape.txt", "sub/file.txt", "sub\\file.txt", "/absolute.txt", "C:\\absolute.txt",
    "C:relative.txt", "bad\x00.txt", "bad\n.txt", "CON.txt", "con .txt", "LPT1.txt",
    "COM¹.txt", "bad?.txt", "bad*.txt", "file.txt.", "file.txt ", "document.pdf", ".txt", "", None, 42,
])
def test_reject_unsafe_names_without_network(client, sdk, name):
    with pytest.raises(UnsafePathError if isinstance(name, str) else TypeError):
        client.read(name)
    with pytest.raises(UnsafePathError):
        client.exists(name)
    with pytest.raises(UnsafePathError):
        client.cleanup([name])
    sdk[1].assert_not_called()


def test_local_read_preserves_line_endings_without_credentials(client):
    target = client.settings.storage_dir / "Решение 1.TXT"
    target.write_bytes("Решение\r\n".encode("utf-8"))
    assert client.exists(target.name)
    assert client.read(target.name) == {target.name: "Решение\r\n"}


def test_batch_returns_texts_by_name_and_only_downloads_missing(client, sdk):
    existing = client.settings.storage_dir / "existing.txt"
    existing.write_text("cached", encoding="utf-8")
    result = client.read([existing.name, "new.txt", "new.txt", "ещё один.txt"])
    assert result == {"existing.txt": "cached", "new.txt": "Суд постановил.\n", "ещё один.txt": "Суд постановил.\n"}
    assert sdk[0].get_object.call_count == 2


def test_strict_decoding_and_explicit_encoding(client):
    target = client.settings.storage_dir / "old.txt"
    target.write_bytes("Решение".encode("cp1251"))
    with pytest.raises(UnicodeDecodeError):
        client.read(target.name)
    assert client.read(target.name, encoding="cp1251") == {target.name: "Решение"}


def test_list_is_sorted_and_accepts_uppercase_extension(client):
    root = client.settings.storage_dir
    for name in ("b.txt", "a.TXT", "other.json"):
        (root / name).write_text("local", encoding="utf-8")
    (root / "directory.txt").mkdir()
    assert client.list_local_files() == ["a.TXT", "b.txt"]


def test_existing_symlink_is_not_read_or_overwritten(client, tmp_path, sdk):
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        (client.settings.storage_dir / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(UnsafePathError):
        client.read("link.txt")
    sdk[1].assert_not_called()
    assert outside.read_text(encoding="utf-8") == "private"


def test_directory_cannot_be_read_as_text(client):
    (client.settings.storage_dir / "directory.txt").mkdir()
    with pytest.raises(UnsafePathError):
        client.read("directory.txt")
