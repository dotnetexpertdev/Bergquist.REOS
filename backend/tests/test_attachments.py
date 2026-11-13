"""Item 7: attachment recovery verifies the actual PDF before marking a bill complete."""

import hashlib

from .helpers import approve, detail, event_types, history, upload_fixture
from .make_fixtures import fixture_bytes


async def _bill_with_pending_attachment(seeded, owner):
    qbo = seeded["qbo"]
    qbo.upload_mode = "reject"
    doc_id = await upload_fixture(owner, "A")
    r = await approve(owner, doc_id)
    assert r.json()["status"] == "attachment_pending"
    qbo.upload_mode = "ok"
    return doc_id, r.json()["qb_bill_id"]


async def test_unrelated_attachment_is_not_treated_as_success(seeded, owner):
    qbo = seeded["qbo"]
    doc_id, bill_id = await _bill_with_pending_attachment(seeded, owner)
    qbo.add_attachment(bill_id, b"%PDF-1.4 some other document", "receipt.pdf")
    uploads_before = len(qbo.upload_calls)
    r = await owner.post(f"/api/quickbooks/{doc_id}/retry-attachment")
    assert r.json()["status"] == "recorded"
    assert len(qbo.upload_calls) == uploads_before + 1  # ours was uploaded
    ours = [a for a in qbo.attachments_for(bill_id) if a["FileName"] != "receipt.pdf"]
    assert len(ours) == 1
    d = await detail(owner, doc_id)
    assert d["qb_attachment_id"] == ours[0]["Id"]
    sha = hashlib.sha256(qbo.contents[ours[0]["Id"]]).hexdigest()
    assert sha == hashlib.sha256(fixture_bytes("A")).hexdigest()


async def test_bill_already_containing_correct_pdf_is_verified_by_content(seeded, owner):
    qbo = seeded["qbo"]
    doc_id, bill_id = await _bill_with_pending_attachment(seeded, owner)
    # Same bytes attached manually in QuickBooks: no note, size not reported -> content download.
    att = qbo.add_attachment(bill_id, fixture_bytes("A"), "scanned-by-hand.pdf", report_size=False)
    uploads_before = len(qbo.upload_calls)
    r = await owner.post(f"/api/quickbooks/{doc_id}/retry-attachment")
    assert r.json()["status"] == "recorded" and r.json()["qb_attachment_id"] == att
    assert len(qbo.upload_calls) == uploads_before  # nothing uploaded again
    verified = [e for e in await history(owner, doc_id) if e["event_type"] == "attachment_verified"][0]
    assert "downloaded attachment bytes match" in verified["summary"]


async def test_timed_out_upload_that_succeeded_is_found_on_retry(seeded, owner):
    qbo = seeded["qbo"]
    qbo.upload_mode = "timeout_after_success"
    doc_id = await upload_fixture(owner, "A")
    r = await approve(owner, doc_id)
    body = r.json()
    assert body["status"] == "attachment_pending" and body["retry_action"] == "retry_attachment"
    bill_id = body["qb_bill_id"]
    assert len(qbo.attachments_for(bill_id)) == 1  # QuickBooks stored it
    r = await owner.post(f"/api/quickbooks/{doc_id}/retry-attachment")
    assert r.json()["status"] == "recorded" and r.json()["qb_bill_id"] == bill_id
    assert len(qbo.upload_calls) == 1 and len(qbo.attachments_for(bill_id)) == 1
    h = event_types(await history(owner, doc_id))
    assert "attachment_outcome_unknown" in h and "attachment_verified" in h


async def test_failed_attachment_lookup_never_marks_complete(seeded, owner):
    qbo = seeded["qbo"]
    doc_id, bill_id = await _bill_with_pending_attachment(seeded, owner)
    qbo.fail_attachable_query = True
    r = await owner.post(f"/api/quickbooks/{doc_id}/retry-attachment")
    assert r.json()["status"] == "attachment_pending" and r.json()["retry_action"] == "retry_attachment"
    assert len(qbo.attachments_for(bill_id)) == 0
    assert (await detail(owner, doc_id))["qb_attachment_id"] is None


async def test_unverifiable_attachment_leaves_item_in_review(seeded, owner):
    qbo = seeded["qbo"]
    doc_id, bill_id = await _bill_with_pending_attachment(seeded, owner)
    qbo.add_attachment(bill_id, fixture_bytes("A"), "maybe.pdf", report_size=False)
    qbo.download_fail = True
    uploads_before = len(qbo.upload_calls)
    r = await owner.post(f"/api/quickbooks/{doc_id}/retry-attachment")
    assert r.json()["status"] == "attachment_pending"
    assert "could not be checked" in r.json()["error"]
    assert len(qbo.upload_calls) == uploads_before


async def test_retry_resumes_against_same_bill(seeded, owner):
    qbo = seeded["qbo"]
    doc_id, bill_id = await _bill_with_pending_attachment(seeded, owner)
    qbo.add_bill("56", "OTHER", "1.00", "2026-01-01")  # another bill exists; must not be touched
    r = await owner.post(f"/api/quickbooks/{doc_id}/retry-attachment")
    assert r.json()["qb_bill_id"] == bill_id
    listing = [c for c in qbo.calls("GET", "/query") if "Attachable" in c["params"]["query"]]
    assert all(f"'{bill_id}'" in c["params"]["query"] for c in listing)
    assert all(a["AttachableRef"][0]["EntityRef"]["value"] == bill_id
               for a in qbo.attachables.values() if a["FileName"].startswith("MPL"))
