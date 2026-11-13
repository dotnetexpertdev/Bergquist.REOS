"""TEST-ONLY server for the Playwright end-to-end workflow.

Runs the real FastAPI application under uvicorn with:
* a fresh temporary data folder (SQLite + file store) built by the real migrations,
* the deterministic adapters from tests/fakes.py (QuickBooks over HTTP emulation,
  Gmail with one labeled vendor email, fixture-driven extraction),
* a /__test__ router for inspecting the fake QuickBooks company.

It never reads the owner's .env, database, documents or credentials.

    venv\\Scripts\\python.exe backend\\tests\\e2e_server.py --port 8765
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

import bcrypt
from cryptography.fernet import Fernet

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

E2E_PASSWORD = "e2e-owner-password"


def configure_environment(data_dir: Path, port: int) -> None:
    os.environ.update({
        "MORNING_ASSISTANT_ENV_FILE": "",
        "SESSION_SECRET": "e2e-session-secret-0123456789abcdef0123",
        "DATA_PATH": str(data_dir),
        "OWNER_USERNAME": "owner",
        "OWNER_PASSWORD_HASH": bcrypt.hashpw(E2E_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "PUBLIC_ORIGIN": f"http://127.0.0.1:{port}",
        "GMAIL_CLIENT_ID": "e2e", "GMAIL_CLIENT_SECRET": "e2e",
        "QUICKBOOKS_CLIENT_ID": "e2e", "QUICKBOOKS_CLIENT_SECRET": "e2e",
        "QUICKBOOKS_EXPECTED_REALM_ID": "TEST-REALM",
        "OPENAI_API_KEY": "",
        "OWNER_TIMEZONE": "UTC",
        "TODOIST_ENABLED": "false",
    })


async def seed(qbo) -> None:
    from app.database import get_db_context
    from app.encryption import encrypt_token
    from app.models import AccountMapping, Property, ProviderConnection, VendorMapping

    async with get_db_context() as db:
        db.add(ProviderConnection(provider="quickbooks", realm_id=qbo.realm_id, company_name=qbo.company_name,
                                  is_valid=True, access_token_encrypted=encrypt_token("fake-token"),
                                  refresh_token_encrypted=encrypt_token("fake-refresh")))
        db.add(ProviderConnection(provider="gmail", connected_account="owner@example.test", is_valid=True,
                                  access_token_encrypted=encrypt_token("g"), refresh_token_encrypted=encrypt_token("r")))
        db.add_all([Property(name="Demo Property", address="123 Main St, Springfield"),
                    Property(name="Oak Street Duplex", address="45 Oak Street, Springfield")])
        for name, vid in (("Metro Power & Light", "56"), ("City Water Dept", "57"), ("Northside Electric Supply", "58")):
            db.add(VendorMapping(extracted_name=name, qb_vendor_id=vid, qb_vendor_name=name, realm_id=qbo.realm_id))
        db.add(AccountMapping(category="utility", qb_account_id="7", qb_account_name="Utilities", realm_id=qbo.realm_id))
        db.add(AccountMapping(category="invoice", qb_account_id="8", qb_account_name="Repairs", realm_id=qbo.realm_id))


def build_app(qbo, gmail, fake_ai):
    from fastapi import APIRouter
    from fastapi.responses import Response

    from app.main import app
    from tests.fakes import install
    from tests.make_fixtures import FIXTURES, fixture_bytes

    install(qbo, gmail, fake_ai)
    test_router = APIRouter(prefix="/__test__")

    @test_router.get("/quickbooks")
    async def quickbooks_state():
        return qbo.snapshot()

    @test_router.get("/fixtures/{key}")
    async def fixture(key: str):
        return Response(fixture_bytes(key), media_type="application/pdf",
                        headers={"X-Filename": FIXTURES[key]["filename"]})

    # Must be registered before the SPA catch-all route.
    app.router.routes.insert(0, test_router.routes[0])
    app.router.routes.insert(0, test_router.routes[1])
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    data_dir = Path(tempfile.mkdtemp(prefix="ma-e2e-")) / "data"
    configure_environment(data_dir, args.port)

    import asyncio

    import uvicorn

    from app.migrations import migrate
    from tests.fakes import FakeAI, FakeGmail, FakeQBOServer
    from tests.helpers import build_email
    from tests.make_fixtures import fixture_bytes

    migrate()
    qbo = FakeQBOServer()
    # Fixture C was entered manually in the test company before the run.
    qbo.add_bill("58", "NES-5531", "389.00", "2026-09-30", private_note="Entered manually in QuickBooks")
    gmail = FakeGmail()
    gmail.add_message("18c-demo-A", build_email("Metro Power & Light <billing@metropower.example>",
                                                "Your October electricity statement",
                                                [("metro-power-statement.pdf", fixture_bytes("A"), "application/pdf")]))
    gmail.add_message("18c-unrelated", build_email("friend@example.test", "Lunch on Friday?", []), labeled=False)
    asyncio.run(seed(qbo))
    app = build_app(qbo, gmail, FakeAI())
    print(f"E2E server data folder: {data_dir}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
