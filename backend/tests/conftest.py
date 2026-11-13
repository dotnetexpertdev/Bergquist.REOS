"""Test configuration: isolated settings, a migrated temporary database and test adapters.

The environment is fixed *before* any ``app`` import, and
MORNING_ASSISTANT_ENV_FILE is set to empty so the owner's real .env, data and
credentials are never read or touched.
"""

import os
import shutil
import tempfile
from pathlib import Path

import bcrypt
from cryptography.fernet import Fernet

TEST_ROOT = Path(tempfile.mkdtemp(prefix="ma-tests-"))
TEST_PASSWORD = "correct horse battery staple"

os.environ.update({
    "MORNING_ASSISTANT_ENV_FILE": "",
    "SESSION_SECRET": "test-secret-key-for-testing-only-0123456789",
    "DATA_PATH": str(TEST_ROOT / "data"),
    "OWNER_USERNAME": "owner",
    "OWNER_PASSWORD_HASH": bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
    "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "PUBLIC_ORIGIN": "http://testserver",
    "GMAIL_CLIENT_ID": "test-gmail-client",
    "GMAIL_CLIENT_SECRET": "test-gmail-secret",
    "GMAIL_OAUTH_CALLBACK": "/api/gmail/callback",
    "QUICKBOOKS_CLIENT_ID": "test-qb-client",
    "QUICKBOOKS_CLIENT_SECRET": "test-qb-secret",
    "QUICKBOOKS_OAUTH_CALLBACK": "/api/quickbooks/callback",
    "QUICKBOOKS_EXPECTED_REALM_ID": "TEST-REALM",
    "QUICKBOOKS_ENVIRONMENT": "sandbox",
    "OPENAI_API_KEY": "",
    "TODOIST_ENABLED": "false",
    "OWNER_TIMEZONE": "UTC",
})

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.migrations import migrate  # noqa: E402

assert settings.data_path == (TEST_ROOT / "data").resolve(), "tests must never use the real data folder"
migrate()  # the real Alembic migrations build the test schema

from app.database import async_session_maker, engine  # noqa: E402
from app.encryption import encrypt_token  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AccountMapping, Property, ProviderConnection, VendorMapping  # noqa: E402
from app.providers import registry  # noqa: E402

from .fakes import FakeAI, FakeGmail, FakeQBOServer, install  # noqa: E402

TABLES = ["accounting_claims", "audit_events", "duplicate_candidates", "operations", "arrivals", "documents",
          "vendor_mappings", "account_mappings", "provider_connections", "properties"]


@pytest_asyncio.fixture(autouse=True)
async def clean_state():
    """Every test starts with empty tables, an empty file store and real adapters."""
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))
    for sub in ("documents", "emails", "temp"):
        shutil.rmtree(settings.data_path / sub, ignore_errors=True)
        (settings.data_path / sub).mkdir(parents=True, exist_ok=True)
    registry.reset()
    yield
    registry.reset()


@pytest_asyncio.fixture
async def db_session():
    async with async_session_maker() as session:
        yield session


@pytest.fixture
def qbo():
    return FakeQBOServer()


@pytest.fixture
def gmail():
    return FakeGmail()


@pytest.fixture
def fake_ai():
    return FakeAI()


@pytest_asyncio.fixture
async def seeded(qbo, gmail, fake_ai):
    """A connected test company, one property with a known address, and mappings."""
    install(qbo, gmail, fake_ai)
    async with async_session_maker() as db:
        db.add(ProviderConnection(provider="quickbooks", realm_id=qbo.realm_id, company_name=qbo.company_name,
                                  is_valid=True, access_token_encrypted=encrypt_token("fake-token"),
                                  refresh_token_encrypted=encrypt_token("fake-refresh")))
        db.add(ProviderConnection(provider="gmail", connected_account="owner@example.test", is_valid=True,
                                  access_token_encrypted=encrypt_token("g"), refresh_token_encrypted=encrypt_token("r")))
        main = Property(name="Demo Property", address="123 Main St, Springfield")
        oak = Property(name="Oak Street Duplex", address="45 Oak Street, Springfield")
        db.add_all([main, oak])
        db.add_all([
            VendorMapping(extracted_name="Metro Power & Light", qb_vendor_id="56",
                          qb_vendor_name="Metro Power & Light", realm_id=qbo.realm_id),
            VendorMapping(extracted_name="City Water Dept", qb_vendor_id="57",
                          qb_vendor_name="City Water Dept", realm_id=qbo.realm_id),
            VendorMapping(extracted_name="Northside Electric Supply", qb_vendor_id="58",
                          qb_vendor_name="Northside Electric Supply", realm_id=qbo.realm_id),
            AccountMapping(category="utility", qb_account_id="7", qb_account_name="Utilities", realm_id=qbo.realm_id),
            AccountMapping(category="invoice", qb_account_id="8", qb_account_name="Repairs", realm_id=qbo.realm_id),
        ])
        await db.commit()
        ids = {"main": main.id, "oak": oak.id}
    return {"qbo": qbo, "gmail": gmail, "ai": fake_ai, "properties": ids}


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def login(c: AsyncClient, password: str = TEST_PASSWORD) -> AsyncClient:
    r = await c.post("/api/auth/login", json={"username": "owner", "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


@pytest_asyncio.fixture
async def owner(client):
    return await login(client)


@pytest.fixture
def sample_pdf_path(tmp_path):
    from .make_fixtures import fixture_bytes
    path = tmp_path / "sample.pdf"
    path.write_bytes(fixture_bytes("A"))
    return path


@pytest.fixture
def sample_image_path(tmp_path):
    png = bytes([
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
        0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41, 0x54, 0x08, 0xD7, 0x63, 0xF8, 0xFF, 0xFF, 0xFF,
        0x00, 0x05, 0xFE, 0x02, 0xFE, 0xDC, 0xCC, 0x59, 0xE7, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E,
        0x44, 0xAE, 0x42, 0x60, 0x82,
    ])
    path = tmp_path / "sample.png"
    path.write_bytes(png)
    return path
