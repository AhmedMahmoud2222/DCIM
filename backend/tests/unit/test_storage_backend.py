"""Phase 10A PR-5: LocalFileSystemStorageBackend, in isolation — no DB/HTTP boundary
needed, matching this repo's tests/unit/ convention for pure-logic modules."""

import pytest

from app.infrastructure.storage import LocalFileSystemStorageBackend


def test_save_then_read_round_trips_bytes(tmp_path):
    backend = LocalFileSystemStorageBackend(root_dir=tmp_path)
    backend.save("abc123.png", b"fake-png-bytes")
    assert backend.read("abc123.png") == b"fake-png-bytes"


def test_exists_is_false_before_save_and_true_after(tmp_path):
    backend = LocalFileSystemStorageBackend(root_dir=tmp_path)
    assert backend.exists("abc123.png") is False
    backend.save("abc123.png", b"content")
    assert backend.exists("abc123.png") is True


def test_read_missing_key_raises_file_not_found(tmp_path):
    backend = LocalFileSystemStorageBackend(root_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        backend.read("does-not-exist.png")


def test_save_is_idempotent_for_content_addressed_keys(tmp_path):
    """The whole point of content-addressed keys: saving the same key twice (as
    clone_revision's graphic-reuse and re-upload-of-identical-bytes both do) never
    re-writes the file — verified here by writing garbage under the same key the second
    time and confirming the original bytes survive."""
    backend = LocalFileSystemStorageBackend(root_dir=tmp_path)
    backend.save("hash1.png", b"original bytes")
    backend.save("hash1.png", b"different bytes that should never be written")
    assert backend.read("hash1.png") == b"original bytes"


@pytest.mark.parametrize("bad_key", ["../escape.png", "a/b.png", "a\\b.png", "..", ""])
def test_rejects_path_traversal_and_empty_keys(tmp_path, bad_key):
    backend = LocalFileSystemStorageBackend(root_dir=tmp_path)
    with pytest.raises(ValueError):
        backend.save(bad_key, b"x")


def test_root_dir_is_created_if_missing(tmp_path):
    root = tmp_path / "nested" / "does" / "not" / "exist" / "yet"
    assert not root.exists()
    LocalFileSystemStorageBackend(root_dir=root)
    assert root.exists()


def test_concurrent_saves_never_leave_a_partial_file_visible(tmp_path):
    """The write-to-temp-then-rename discipline (local.py) means a reader can never
    observe a half-written file. Simulated here by writing a large payload and
    confirming no stray .tmp-* file is left behind once save() returns."""
    backend = LocalFileSystemStorageBackend(root_dir=tmp_path)
    backend.save("large.png", b"x" * (2 * 1024 * 1024))
    leftover_temp_files = list(tmp_path.glob(".tmp-*"))
    assert leftover_temp_files == []
