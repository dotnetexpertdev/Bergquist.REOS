"""QuickBooks is checked for an existing bill automatically, without the owner asking."""

from sqlalchemy import delete

from app.database import async_session_maker
from app.models import VendorMapping

from .helpers import approve, detail, event_types, history, upload_fixture


async def test_bill_already_in_quickbooks_goes_straight_to_review(seeded, owner):
    qbo = seeded["qbo"]
    qbo.add_bill("58", "NES-5531", "389.00", "2026-09-30", private_note="typed in by the owner")
    doc_id = await upload_fixture(owner, "C")  # no check button pressed
    d = await detail(owner, doc_id)
    assert d["status"] == "duplicate_candidate"
    assert d["pending_question"] == "Is this the same invoice as QuickBooks bill NES-5531?"
    assert "approve" not in d["available_actions"] and d["last_accounting_check_ok"] is True
    listed = (await owner.get("/api/documents/needs-review")).json()
    assert [x["id"] for x in listed] == [doc_id]
    checks = [e for e in await history(owner, doc_id) if e["event_type"] == "duplicate_check_completed"]
    assert len(checks) == 1 and checks[0]["actor"] == "system"
    assert qbo.create_bill_calls == []


async def test_new_bill_is_checked_and_stays_ready(seeded, owner):
    qbo = seeded["qbo"]
    doc_id = await upload_fixture(owner, "A")
    d = await detail(owner, doc_id)
    assert d["status"] == "ready_for_approval" and d["last_accounting_check_ok"] is True
    assert "duplicate_check_completed" in event_types(await history(owner, doc_id))
    assert (await approve(owner, doc_id)).json()["status"] == "recorded" and qbo.bill_count() == 1


async def test_intake_succeeds_when_quickbooks_lookup_fails(seeded, owner):
    qbo = seeded["qbo"]
    qbo.fail_bill_query_starts = {1}
    doc_id = await upload_fixture(owner, "A")
    d = await detail(owner, doc_id)
    assert d["status"] == "ready_for_approval" and d["last_accounting_check_ok"] is False
    assert d["pending_question"] == "The QuickBooks duplicate check failed. Retry the check before approving."
    assert "accounting_check_failed" in event_types(await history(owner, doc_id))
    # Approval repeats the check, so a failed lookup can never lead to a bill.
    assert (await approve(owner, doc_id)).status_code == 502 and qbo.create_bill_calls == []


async def test_check_runs_when_the_vendor_is_chosen_later(seeded, owner):
    qbo = seeded["qbo"]
    qbo.add_bill("58", "NES-5531", "389.00", "2026-09-30")
    async with async_session_maker() as db:
        await db.execute(delete(VendorMapping).where(VendorMapping.qb_vendor_id == "58"))
        await db.commit()
    doc_id = await upload_fixture(owner, "C")
    d = await detail(owner, doc_id)
    assert d["status"] == "needs_review" and d["last_accounting_check_ok"] is None  # vendor unknown: nothing to compare

    r = await owner.post(f"/api/documents/{doc_id}/update", json={"revision": d["revision"], "qb_vendor_id": "58"})
    assert r.status_code == 200 and r.json()["status"] == "duplicate_candidate", r.text
    assert (await detail(owner, doc_id))["status"] == "duplicate_candidate"
    assert qbo.create_bill_calls == []
