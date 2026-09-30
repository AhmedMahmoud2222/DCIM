"""Default StorageBackend implementation: a plain directory on local disk. Every key this
codebase ever constructs is a content-addressed `sha256(...).ext` string it generates
itself (see app/application/catalog_graphics_service.py) — never a client-supplied
filename — but `_safe_path` still rejects any key containing a path separator or `..`
defensively, matching the "never trust a filename for anything beyond display" discipline
app/api/v1/floor_plans.py already established for uploads."""

import os
import tempfile
from pathlib import Path


class LocalFileSystemStorageBackend:
    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, key: str) -> Path:
        if not key or "/" in key or "\\" in key or ".." in key:
            raise ValueError(f"invalid storage key: {key!r}")
        return self._root / key

    def save(self, key: str, content: bytes) -> None:
        path = self._safe_path(key)
        if path.exists():
            return
        # Write-to-temp-then-rename: a concurrent reader of `key` never observes a
        # partially-written file, since `os.replace` is atomic on the same filesystem —
        # the temp file is created in the same directory specifically to guarantee that.
        fd, tmp_path = tempfile.mkstemp(dir=self._root, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def read(self, key: str) -> bytes:
        path = self._safe_path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise FileNotFoundError(f"storage key not found: {key}") from None

    def exists(self, key: str) -> bool:
        return self._safe_path(key).exists()

    def delete(self, key: str) -> None:
        self._safe_path(key).unlink(missing_ok=True)
