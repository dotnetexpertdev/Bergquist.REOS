"""Demo reset: clears this app's data, its files and only the QuickBooks bills it created."""

from sqlalchemy import text

from app.config import settings
from app.database import async_session_maker

from .helpers import approve, detail, upload_fixture


def _stored_files() -> int:
    return sum(1 for sub in ("documents", "emails", "temp")
               for f in (settings.data_path / sub).rglob("*") if f.is_file())


async def _count(table: str) -> int:
    async with async_session_maker() as db:
        return (await db.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar()


async def _recorded_bill(owner) -> str:
    doc_id = await upload_fixture(owner, "A")
    r = await approve(owner, doc_id)
    assert r.json()["status"] == "recorded", r.text
    return doc_id


async def test_reset_removes_app_data_and_only_app_created_bills(seeded, owner):
    qbo = seeded["qbo"]
    manual = qbo.add_bill("58", "NES-5531", "389.00", "2026-09-30", private_note="Entered manually in QuickBooks")
    doc_id = await _recorded_bill(owner)
    app_bill = (await detail(owner, doc_id))["qb_bill_id"]
    assert len(qbo.attachments_for(app_bill)) == 1 and _stored_files() >= 1

    preview = (await owner.get("/api/admin/reset-demo")).json()
    assert preview["documents"] == 1 and preview["quickbooks"]["available"]
    assert [b["id"] for b in preview["quickbooks"]["bills"]] == [app_bill]

    r = await owner.post("/api/admin/reset-demo", json={"confirm": "RESET"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["documents_deleted"] == 1 and body["quickbooks"]["bills_deleted"] == 1
    assert body["quickbooks"]["attachments_deleted"] == 1 and body["backup"]

    assert list(qbo.bills) == [manual] and not qbo.attachables  # the hand-entered bill is untouched
    assert (await owner.get("/api/documents")).json() == []
    assert _stored_files() == 0
    for table in ("documents", "arrivals", "audit_events", "operations", "accounting_claims"):
        assert await _count(table) == 0
    assert await _count("properties") == 2 and await _count("vendor_mappings") == 3
    assert await _count("provider_connections") == 2
    backup = settings.data_path.parent / body["backup"]
    assert backup.exists()
    backup.unlink()

    # The same bill can be run through the demo again from scratch.
    again = await _recorded_bill(owner)
    assert again != doc_id and qbo.bill_count("56", "MPL-2026-0415") == 1


async def test_reset_needs_the_confirmation_word(seeded, owner):
    await _recorded_bill(owner)
    r = await owner.post("/api/admin/reset-demo", json={"confirm": "yes"})
    assert r.status_code == 400
    assert await _count("documents") == 1 and seeded["qbo"].bill_count() == 1


async def test_quickbooks_failure_leaves_local_data_untouched(seeded, owner):
    qbo = seeded["qbo"]
    await _recorded_bill(owner)
    qbo.delete_mode = "reject"
    r = await owner.post("/api/admin/reset-demo", json={"confirm": "RESET"})
    assert r.status_code == 502 and "did not delete" in r.json()["detail"]
    assert await _count("documents") == 1 and qbo.bill_count() == 1 and _stored_files() >= 1


async def test_reset_can_skip_quickbooks(seeded, owner):
    qbo = seeded["qbo"]
    await _recorded_bill(owner)
    r = await owner.post("/api/admin/reset-demo", json={"confirm": "RESET", "delete_quickbooks": False})
    assert r.status_code == 200 and r.json()["quickbooks"]["skipped"]
    assert await _count("documents") == 0 and qbo.bill_count() == 1
    (settings.data_path.parent / r.json()["backup"]).unlink()


async def test_reset_requires_sign_in(client):
    assert (await client.get("/api/admin/reset-demo")).status_code == 401
    assert (await client.post("/api/admin/reset-demo", json={"confirm": "RESET"})).status_code in (401, 403)
