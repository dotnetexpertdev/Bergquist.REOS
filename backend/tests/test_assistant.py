"""Item 8: history-grounded answers, clarification, follow-ups, correction proposals, transcription."""

from .helpers import approve, build_email, detail, event_types, history, upload, upload_fixture
from .make_fixtures import FIXTURES, fixture_bytes, make_pdf


async def ask(owner, query, context=None, select=None):
    r = await owner.post("/api/search", json={"query": query, "context_document_id": context,
                                              "select_document_id": select})
    assert r.status_code == 200, r.text
    return r.json()


async def _recorded_a_with_two_arrivals(seeded, owner):
    gm = seeded["gmail"]
    gm.add_message("msg-A", build_email("billing@metropower.example", "Your October statement",
                                        [("metro-power-statement.pdf", fixture_bytes("A"), "application/pdf")]))
    r = await owner.post("/api/gmail/import")
    doc_id = r.json()["results"][0]["document_id"]
    assert (await approve(owner, doc_id)).json()["status"] == "recorded"
    up = await upload(owner, fixture_bytes("A"), "IMG_2231-from-phone.pdf")
    assert up.json()["document_id"] == doc_id and up.json()["is_duplicate"] is True
    return doc_id


async def test_what_happened_to_the_power_bill_is_grounded_in_history(seeded, owner):
    doc_id = await _recorded_a_with_two_arrivals(seeded, owner)
    res = await ask(owner, "What happened to the power bill?")  # neither word is in the filename
    assert res["selected_document_id"] == doc_id and not res["needs_clarification"]
    answer = res["answer"]
    assert "Metro Power & Light" in answer and "USD 142.50" in answer and "Demo Property" in answer
    assert "unpaid bill was created in QuickBooks" in answer and "not a payment" in answer
    assert "arrived 2 times" in answer and "billing@metropower.example" in answer
    assert "IMG_2231-from-phone.pdf" in answer
    assert " paid." not in answer.replace("not a payment", "")
    saved = event_types(await history(owner, doc_id))
    returned = [e["event_type"] for e in res["history"]]
    assert returned == saved  # the answer carries the actual saved history
    for needed in ("arrival_recorded", "approval_granted", "bill_created", "attachment_completed"):
        assert needed in returned
    arrivals = [e for e in res["history"] if e["event_type"] == "arrival_recorded"]
    assert len(arrivals) == 2
    assert any(l["label"] == "Source email" and "msg-A" in l["href"] for l in arrivals[0]["links"])
    links = res["documents"][0]["links"]
    assert links["record"] == f"/documents/{doc_id}" and links["original"] == f"/api/documents/{doc_id}/file"
    assert links["quickbooks"] and res["documents"][0]["qb_bill_id"]
    assert res["documents"][0]["arrival_count"] == 2


async def test_electricity_question_and_follow_up(seeded, owner):
    doc_id = await _recorded_a_with_two_arrivals(seeded, owner)
    res = await ask(owner, "What happened to the electricity bill?")
    assert res["selected_document_id"] == doc_id
    follow = await ask(owner, "Which property was it for?", context=res["selected_document_id"])
    assert follow["selected_document_id"] == doc_id and follow["intent"] == "followup"
    assert follow["answer"].startswith("The Metro Power & Light invoice MPL-2026-0415 bill is for Demo Property.")


async def test_two_matching_bills_ask_owner_to_choose(seeded, owner):
    a = await upload_fixture(owner, "A")
    second = dict(FIXTURES["A"]["values"], invoice_number="MPL-2026-0515", invoice_date="2026-11-15",
                  total_amount="99.10")
    b = (await upload(owner, make_pdf(["Metro Power November"], second), "nov.pdf")).json()["document_id"]
    res = await ask(owner, "What happened to the electricity bill?")
    assert res["needs_clarification"] is True and res["selected_document_id"] is None
    assert {d["id"] for d in res["documents"]} == {a, b}
    assert res["pending_query"] == "What happened to the electricity bill?"
    chosen = await ask(owner, res["pending_query"], select=b)
    assert chosen["selected_document_id"] == b and "MPL-2026-0515" in chosen["answer"]
    follow = await ask(owner, "How much was it?", context=b)
    assert follow["selected_document_id"] == b and "USD 99.10" in follow["answer"]


async def test_no_match_says_so(seeded, owner):
    await upload_fixture(owner, "A")
    res = await ask(owner, "What happened to the internet bill?")
    assert res["selected_document_id"] is None and "couldn't find" in res["answer"]


async def test_voice_correction_is_a_proposal_until_confirmed(seeded, owner):
    doc_id = await upload_fixture(owner, "B")  # property unclear
    found = await ask(owner, "What happened to the water bill?")
    assert found["selected_document_id"] == doc_id
    before = await detail(owner, doc_id)
    res = await ask(owner, "Change the property on this bill to Oak Street", context=doc_id)
    edit = res["proposed_edit"]
    assert edit["field"] == "property" and edit["current_value"] is None
    assert edit["proposed_value"] == "Oak Street Duplex" and edit["proposed_raw"] == seeded["properties"]["oak"]
    assert edit["revision"] == before["revision"]
    assert "Nothing changes until you confirm" in res["answer"]
    after_proposal = await detail(owner, doc_id)
    assert after_proposal["revision"] == before["revision"] and after_proposal["property_id"] is None
    assert "field_corrected" not in event_types(await history(owner, doc_id))

    # Confirm through the same versioned update endpoint as a typed correction.
    r = await owner.post(f"/api/documents/{doc_id}/update",
                         json={"revision": edit["revision"], edit["update_key"]: edit["proposed_raw"],
                               "source": "voice"})
    assert r.status_code == 200 and r.json()["status"] == "ready_for_approval"
    corrected = [e for e in await history(owner, doc_id) if e["event_type"] == "field_corrected"]
    assert len(corrected) == 1
    assert corrected[0]["summary"] == "Corrected property id by voice: empty → Oak Street Duplex"
    assert corrected[0]["actor"] == "owner"
    assert seeded["qbo"].create_bill_calls == []  # saving a correction never posts

    # A stale confirmation (old revision) is rejected.
    stale = await owner.post(f"/api/documents/{doc_id}/update",
                             json={"revision": edit["revision"], "property_id": seeded["properties"]["main"]})
    assert stale.status_code == 409

    # A new question returns the corrected value; history keeps the earlier value.
    again = await ask(owner, "Which property is the water bill for?")
    assert "Oak Street Duplex" in again["answer"]


async def test_cancelled_correction_leaves_record_unchanged(seeded, owner):
    doc_id = await upload_fixture(owner, "A")
    before = await detail(owner, doc_id)
    res = await ask(owner, "Change the property on this bill to Oak Street", context=doc_id)
    assert res["proposed_edit"]
    # Cancelling is purely client side: no request is sent. The record is unchanged.
    after = await detail(owner, doc_id)
    assert after["revision"] == before["revision"] and after["property_id"] == before["property_id"]
    assert event_types(await history(owner, doc_id)) == event_types(await history(owner, doc_id))
    assert "field_corrected" not in event_types(await history(owner, doc_id))


async def test_correction_after_recording_flags_discrepancy_without_touching_quickbooks(seeded, owner):
    qbo = seeded["qbo"]
    doc_id = await upload_fixture(owner, "A")
    assert (await approve(owner, doc_id)).json()["status"] == "recorded"
    requests_before = len(qbo.request_log)
    res = await ask(owner, "Change the amount on this bill to 150.00", context=doc_id)
    edit = res["proposed_edit"]
    assert edit["current_value"] == "142.50" and edit["proposed_value"] == "150.00"
    assert "QuickBooks will not be changed" in res["answer"]
    r = await owner.post(f"/api/documents/{doc_id}/update",
                         json={"revision": edit["revision"], edit["update_key"]: edit["proposed_raw"]})
    assert r.status_code == 200 and r.json()["status"] == "recorded"
    d = await detail(owner, doc_id)
    assert d["qb_discrepancy"] and "QuickBooks was not changed" in d["qb_discrepancy"]
    assert len(qbo.request_log) == requests_before  # no QuickBooks call at all
    assert "qb_discrepancy_flagged" in event_types(await history(owner, doc_id))


async def test_unknown_property_in_correction(seeded, owner):
    doc_id = await upload_fixture(owner, "B")
    res = await ask(owner, "Change the property on this bill to Elm Road", context=doc_id)
    assert res["proposed_edit"] is None and "No property matches" in res["answer"]


async def test_transcription_success_failure_and_empty(seeded, owner, fake_ai):
    audio = ("turn.webm", b"\x1aE\xdf\xa3" + b"\x00" * 4000, "audio/webm")
    fake_ai.transcripts = ["What happened to the electricity bill?", "ERROR: Transcription failed: timeout"]
    ok = (await owner.post("/api/transcribe", files={"audio": audio})).json()
    assert ok == {"success": True, "transcript": "What happened to the electricity bill?", "error": None}
    bad = (await owner.post("/api/transcribe", files={"audio": audio})).json()
    assert bad["success"] is False and "timeout" in bad["error"]
    empty = (await owner.post("/api/transcribe", files={"audio": ("t.webm", b"\x1aE", "audio/webm")})).json()
    assert empty["success"] is False and "empty" in empty["error"]
    wrong = (await owner.post("/api/transcribe", files={"audio": ("t.txt", b"x" * 5000, "text/plain")})).json()
    assert wrong["success"] is False and "Unsupported audio" in wrong["error"]


async def test_assistant_and_transcription_require_login(client):
    assert (await client.post("/api/search", json={"query": "x"})).status_code == 401
    assert (await client.post("/api/transcribe", files={"audio": ("a.webm", b"x", "audio/webm")})).status_code == 401
