"""Item 5: QuickBooks duplicate checks must be complete before any create."""

from sqlalchemy import func, select

from app.database import async_session_maker
from app.models import DuplicateCandidate
from app.providers.quickbooks_provider import PAGE_SIZE

from .helpers import approve, detail, event_types, history, upload_fixture


def _fill_vendor(qbo, vendor_id, n):
    for i in range(n):
        qbo.add_bill(vendor_id, f"OLD-{i:04d}", "10.00", "2025-01-01")


async def test_lookup_failure_blocks_creation_with_retryable_error(seeded, owner):
    qbo = seeded["qbo"]
    _fill_vendor(qbo, "56", PAGE_SIZE + 5)
    qbo.fail_bill_query_starts = {PAGE_SIZE + 1}  # page 2 fails
    doc_id = await upload_fixture(owner, "A")
    r = await approve(owner, doc_id)
    assert r.status_code == 502
    assert r.json()["detail"]["retryable"] is True
    assert "did not complete" in r.json()["detail"]["message"]
    assert qbo.create_bill_calls == []  # zero create calls
    d = await detail(owner, doc_id)
    assert d["status"] == "ready_for_approval" and d["last_accounting_check_ok"] is False
    assert d["pending_question"].startswith("The QuickBooks duplicate check failed")
    assert "accounting_check_failed" in event_types(await history(owner, doc_id))

    qbo.fail_bill_query_starts = set()  # retry succeeds once QuickBooks recovers
    d = await detail(owner, doc_id)
    r = await approve(owner, doc_id, d["revision"])
    assert r.status_code == 200 and r.json()["status"] == "recorded"
    assert len(qbo.create_bill_calls) == 1


async def test_first_page_failure_also_blocks(seeded, owner):
    qbo = seeded["qbo"]
    qbo.fail_bill_query_starts = {1}
    doc_id = await upload_fixture(owner, "A")
    r = await owner.post(f"/api/quickbooks/{doc_id}/check-duplicates")
    assert r.status_code == 502
    r = await approve(owner, doc_id)
    assert r.status_code == 502 and qbo.create_bill_calls == []


async def test_match_on_later_page_is_found(seeded, owner):
    qbo = seeded["qbo"]
    _fill_vendor(qbo, "56", PAGE_SIZE * 2 + 3)
    existing = qbo.add_bill("56", "MPL-2026-0415", "142.50", "2026-10-15")  # lands on page 3
    doc_id = await upload_fixture(owner, "A")
    r = await approve(owner, doc_id)
    assert r.status_code == 409
    assert qbo.create_bill_calls == []
    pages = {int(c["params"]["query"].split("STARTPOSITION ")[1].split()[0])
             for c in qbo.calls("GET", "/query") if "FROM Bill" in c["params"]["query"]}
    assert {1, PAGE_SIZE + 1, 2 * PAGE_SIZE + 1} <= pages
    cands = (await owner.get(f"/api/quickbooks/{doc_id}/duplicate-candidates")).json()
    assert [c["qb_bill_id"] for c in cands] == [existing]
    assert (await detail(owner, doc_id))["status"] == "duplicate_candidate"


async def test_bill_entered_manually_in_quickbooks_can_be_linked(seeded, owner):
    qbo = seeded["qbo"]
    manual = qbo.add_bill("58", "NES-5531", "389.00", "2026-09-30", private_note="typed in by the owner")
    before = qbo.bill_count()
    doc_id = await upload_fixture(owner, "C")
    r = await owner.post(f"/api/quickbooks/{doc_id}/check-duplicates")
    assert r.status_code == 200 and r.json()["candidates_found"] == 1
    d = await detail(owner, doc_id)
    assert d["status"] == "duplicate_candidate"
    assert d["pending_question"] == "Is this the same invoice as QuickBooks bill NES-5531?"
    assert "approve" not in d["available_actions"]
    cand = (await owner.get(f"/api/quickbooks/{doc_id}/duplicate-candidates")).json()[0]
    assert "Same vendor and invoice number NES-5531" in cand["match_reason"]
    assert cand["match_details"]["total"] == {"local": "389.00", "remote": "389.00", "match": True}
    assert cand["match_details"]["invoice_number"]["match"] is True

    r = await owner.post(f"/api/quickbooks/{doc_id}/resolve-duplicate",
                         json={"candidate_id": cand["id"], "resolution": "linked", "reason": "Same paper bill"})
    assert r.status_code == 200 and r.json()["status"] == "linked_existing"
    d = await detail(owner, doc_id)
    assert d["qb_bill_id"] == manual and d["status"] == "linked_existing"
    assert qbo.bill_count() == before and qbo.create_bill_calls == []
    h = await history(owner, doc_id)
    linked = [e for e in h if e["event_type"] == "linked_to_existing"][0]
    assert linked["actor"] == "owner" and "entered outside this app" in linked["summary"]
    assert "bill_created" not in event_types(h)
    r = await approve(owner, doc_id)
    assert r.status_code == 409 and qbo.create_bill_calls == []


async def test_same_invoice_number_different_vendor_is_not_a_duplicate(seeded, owner):
    qbo = seeded["qbo"]
    other_vendor = qbo.add_bill("57", "MPL-2026-0415", "142.50", "2026-10-15")
    doc_id = await upload_fixture(owner, "A")
    r = await approve(owner, doc_id)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "recorded"
    assert len(qbo.create_bill_calls) == 1
    assert qbo.bill_count("56", "MPL-2026-0415") == 1
    assert qbo.bills[other_vendor]["VendorRef"]["value"] == "57"
    cands = (await owner.get(f"/api/quickbooks/{doc_id}/duplicate-candidates")).json()
    assert cands == []


async def test_equal_amount_alone_is_not_a_match(seeded, owner):
    qbo = seeded["qbo"]
    qbo.add_bill("56", "MPL-OTHER", "142.50", "2026-07-01")  # same vendor+amount, different number and date
    doc_id = await upload_fixture(owner, "A")
    r = await owner.post(f"/api/quickbooks/{doc_id}/check-duplicates")
    assert r.json()["candidates_found"] == 0


async def test_repeat_checks_update_candidate_and_dismissal_is_preserved(seeded, owner):
    qbo = seeded["qbo"]
    first = qbo.add_bill("56", "MPL-2026-0415", "150.00", "2026-10-15")
    doc_id = await upload_fixture(owner, "A")
    for _ in range(3):
        assert (await owner.post(f"/api/quickbooks/{doc_id}/check-duplicates")).status_code == 200
    async with async_session_maker() as db:
        n = (await db.execute(select(func.count()).select_from(DuplicateCandidate)
                              .where(DuplicateCandidate.document_id == doc_id))).scalar()
    assert n == 1
    cand = (await owner.get(f"/api/quickbooks/{doc_id}/duplicate-candidates")).json()[0]
    assert cand["seen_count"] == 3 and "total" in cand["match_reason"] and "differ" in cand["match_reason"]

    no_reason = await owner.post(f"/api/quickbooks/{doc_id}/resolve-duplicate",
                                 json={"candidate_id": cand["id"], "resolution": "dismissed"})
    assert no_reason.status_code == 400
    r = await owner.post(f"/api/quickbooks/{doc_id}/resolve-duplicate",
                         json={"candidate_id": cand["id"], "resolution": "dismissed",
                               "reason": "Different month; vendor reuses numbers"})
    assert r.status_code == 200 and r.json()["status"] == "ready_for_approval"

    # Re-check: the dismissed bill stays dismissed; a newly appeared bill is a new, blocking match.
    newer = qbo.add_bill("56", "MPL-2026-0415", "142.50", "2026-10-15")
    r = await owner.post(f"/api/quickbooks/{doc_id}/check-duplicates")
    assert r.json()["new_candidates"] == [newer]
    assert r.json()["dismissed_still_present"] == [first]
    cands = {c["qb_bill_id"]: c for c in (await owner.get(f"/api/quickbooks/{doc_id}/duplicate-candidates")).json()}
    assert cands[first]["resolution"] == "dismissed"
    assert cands[first]["resolution_reason"] == "Different month; vendor reuses numbers"
    assert cands[newer]["resolved"] is False and cands[newer]["seen_count"] == 1
    assert (await detail(owner, doc_id))["status"] == "duplicate_candidate"
    r = await approve(owner, doc_id)
    assert r.status_code == 409 and qbo.create_bill_calls == []
    dismissed = [e for e in await history(owner, doc_id) if e["event_type"] == "duplicate_dismissed"][0]
    assert "vendor reuses numbers" in dismissed["summary"]


async def test_unverifiable_company_shows_problem_and_keeps_bill(seeded, owner, monkeypatch):
    qbo = seeded["qbo"]
    doc_id = await upload_fixture(owner, "A")
    qbo.company_info_fail = True
    r = await approve(owner, doc_id)
    assert r.status_code == 503 and "Could not verify the QuickBooks company" in r.json()["detail"]["message"]
    assert (await detail(owner, doc_id))["status"] == "ready_for_approval"
    qbo.company_info_fail = False

    from app.config import settings
    monkeypatch.setattr(settings, "quickbooks_expected_realm_id", "SOME-OTHER-COMPANY")
    r = await approve(owner, doc_id)
    assert r.status_code == 409 and "not the configured test company" in r.json()["detail"]["message"]
    monkeypatch.setattr(settings, "quickbooks_expected_realm_id", "")
    r = await approve(owner, doc_id)
    assert r.status_code == 400 and "QUICKBOOKS_EXPECTED_REALM_ID" in r.json()["detail"]["message"]
    assert qbo.create_bill_calls == []
    assert (await detail(owner, doc_id))["status"] == "ready_for_approval"
