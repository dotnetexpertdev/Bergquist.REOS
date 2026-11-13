"""The demo story at API level: Gmail import, approval, phone re-upload, access control."""

import hashlib

from httpx import ASGITransport, AsyncClient

from app.main import app

from .conftest import login
from .helpers import approve, build_email, detail, event_types, history, upload
from .make_fixtures import fixture_bytes


async def test_email_then_phone_upload_gives_two_arrivals_one_document_one_bill(seeded, owner):
    qbo, gm = seeded["qbo"], seeded["gmail"]
    pdf = fixture_bytes("A")
    gm.add_message("msg-A", build_email("billing@metropower.example", "Statement",
                                        [("metro-power-statement.pdf", pdf, "application/pdf")]))
    gm.add_message("msg-unrelated", build_email("friend@example.test", "Lunch?", []), labeled=False)
    gm.add_message("msg-nopdf", build_email("x@example.test", "notes",
                                            [("notes.txt", b"hello", "text/plain")]))

    first = (await owner.post("/api/gmail/import")).json()
    again = (await owner.post("/api/gmail/import")).json()
    assert first["messages_found"] == 2 and first["imported"] == 1 and first["skipped"] == 1
    assert again["imported"] == 0 and again["already_processed"] == 1
    doc_id = first["results"][0]["document_id"] if first["results"][0]["document_id"] else first["results"][1]["document_id"]
    docs = (await owner.get("/api/documents")).json()
    assert [d["id"] for d in docs] == [doc_id] and docs[0]["arrival_count"] == 1

    original = await owner.get(f"/api/documents/{doc_id}/file")
    assert original.status_code == 200 and original.content == pdf

    d = await detail(owner, doc_id)
    assert d["status"] == "ready_for_approval" and d["property_name"] == "Demo Property"
    r = await approve(owner, doc_id, d["revision"])
    assert r.json()["status"] == "recorded"
    assert (await approve(owner, doc_id, d["revision"])).status_code == 409  # repeated click

    up = await upload(owner, pdf, "phone-photo-copy.pdf")
    assert up.status_code == 200 and up.json()["document_id"] == doc_id and up.json()["is_duplicate"]
    d = await detail(owner, doc_id)
    assert [a["method"] for a in d["arrivals"]] == ["email", "upload"]
    assert d["arrivals"][1]["original_filename"] == "phone-photo-copy.pdf"
    assert len((await owner.get("/api/documents")).json()) == 1
    assert qbo.bill_count() == 1 and len(qbo.create_bill_calls) == 1
    atts = qbo.attachments_for(d["qb_bill_id"])
    assert len(atts) == 1 and hashlib.sha256(qbo.contents[atts[0]["Id"]]).hexdigest() == hashlib.sha256(pdf).hexdigest()
    assert len(seeded["ai"].extraction_calls) == 1  # repeat arrival is not re-read

    # After a "restart" (new client, new login) the record and history persist.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c2:
        assert (await c2.get(f"/api/documents/{doc_id}/file")).status_code == 401  # signed out: denied
        assert (await c2.get(f"/api/documents/{doc_id}")).status_code == 401
        c2 = await login(c2)
        h = event_types(await history(c2, doc_id))
        assert h.count("arrival_recorded") == 2 and h.count("bill_created") == 1


async def test_upload_retry_with_same_attempt_counts_once(seeded, owner):
    pdf = fixture_bytes("A")
    r1 = await upload(owner, pdf, "a.pdf", attempt_id="7d5b0b5e-2a4e-4a4f-9a51-0d2c1f0e7a11")
    r2 = await upload(owner, pdf, "a.pdf", attempt_id="7d5b0b5e-2a4e-4a4f-9a51-0d2c1f0e7a11")
    assert r2.json()["retried_attempt"] is True and r2.json()["arrival_id"] == r1.json()["arrival_id"]
    assert len((await detail(owner, r1.json()["document_id"]))["arrivals"]) == 1


async def test_upload_validation(seeded, owner):
    assert (await upload(owner, b"", "empty.pdf")).status_code == 400
    assert (await upload(owner, b"MZ\x90\x00 not a pdf", "evil.pdf")).status_code == 400
    assert (await upload(owner, b"%PDF-1.4", "x.exe")).status_code == 400
    # Same name, different bytes: two separate documents (no overwrite)
    a = (await upload(owner, fixture_bytes("A"), "bill.pdf")).json()["document_id"]
    b = (await upload(owner, fixture_bytes("B"), "bill.pdf")).json()["document_id"]
    assert a != b
    fa = await owner.get(f"/api/documents/{a}/file")
    assert fa.content == fixture_bytes("A")
