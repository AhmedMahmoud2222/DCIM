from app.infrastructure.storage.backend import StorageBackend
from app.infrastructure.storage.local import LocalFileSystemStorageBackend

__all__ = ["StorageBackend", "LocalFileSystemStorageBackend", "get_storage_backend", "get_document_storage_backend"]


def get_storage_backend() -> StorageBackend:
    """The single construction point every caller (routes, tests) should use instead of
    instantiating a backend directly — keeps the choice of implementation (local disk
    today; S3-compatible or similar later) swappable in one place, per the PR-5
    architectural plan's storage-abstraction directive."""
    from app.core.config import get_settings

    settings = get_settings()
    return LocalFileSystemStorageBackend(root_dir=settings.catalog_graphics_storage_root)


def get_document_storage_backend() -> StorageBackend:
    """Catalog datasheet PDFs live in their own root, apart from graphics, so retention and
    volume policy can differ. Same swap point: an object-storage backend replaces this
    function's return value without touching any caller."""
    from app.core.config import get_settings

    return LocalFileSystemStorageBackend(root_dir=get_settings().catalog_documents_storage_root)
