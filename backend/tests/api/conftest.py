import pytest_asyncio

from app.api.v1.catalog_documents import get_malware_scanner
from app.core.config import get_settings
from app.main import app
from tests.api._document_helpers import FakeScanner


@pytest_asyncio.fixture
async def scanner():
    fake = FakeScanner()
    app.dependency_overrides[get_malware_scanner] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_malware_scanner, None)


@pytest_asyncio.fixture
def settings_override():
    """Overrides individual Settings fields for one test."""
    base = get_settings()

    def _apply(**changes):
        app.dependency_overrides[get_settings] = lambda: base.model_copy(update=changes)

    yield _apply
    app.dependency_overrides.pop(get_settings, None)
