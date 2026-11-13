"""POC gaps: document classification, same-invoice copies, looser search, chat corrections, Todoist."""

from datetime import date
from decimal import Decimal

import pytest

from app.database import async_session_maker
from app.models import classify_document_type
from app.services import todoist as todoist_service
from app.services.assistant import local_interpret, match_category, parse_criteria

from .helpers import approve, detail, event_types, history, upload, upload_fixture
from .make_fixtures import FIXTURES, make_pdf


async def ask(owner, query, context=None, select=None):
    r = await owner.post("/api/search", json={"query": query, "context_document_id": context,
                                              "select_document_id": select})
    assert r.status_code == 200, r.text
    return r.json()


def receipt_pdf() -> bytes:
    values = {"document_type": "receipt", "vendor_name": "Ace Hardware", "invoice_number": "R-1001",
              "invoice_date": "2026-10-02", "due_date": None, "total_amount": "38.17", "currency": "USD",
              "service_address": None, "confidence": 0.9, "source_text": "PAID 38.17", "source_page": 1,
              "uncertain_fields": []}
    return make_pdf(["Ace Hardware", "Receipt R-1001", "PAID USD 38.17"], values)


# ---------------------------------------------------------------------------
# A. Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("utility bill", "bill"), ("invoice", "bill"), ("Electricity Service Statement", "bill"),
    ("property tax bill", "bill"), ("insurance premium bill", "bill"), (None, "bill"), ("", "bill"),
    ("something unusual", "bill"), ("receipt", "receipt"), ("payment confirmation", "receipt"),
    ("bank statement", "bank_statement"), ("credit card statement", "bank_statement"),
    ("residential lease", "lease"), ("tax assessment notice", "tax_notice"),
    ("insurance policy", "insurance"), ("contract", "other"), ("letter", "other"),
])
def test_classify_document_type(text, expected):
    assert classify_document_type(text) == expected


async def test_non_bill_is_filed_without_accounting(seeded, owner):
    doc_id = (await upload(owner, receipt_pdf(), "ace-receipt.pdf")).json()["document_id"]
    d = await detail(owner, doc_id)
    assert d["status"] == "filed" and d["document_category"] == "receipt"
    assert d["missing_fields"] == [] and d["pending_question"] is None
    assert "approve" not in d["available_actions"] and "update_fields" in d["available_actions"]
    assert (await approve(owner, doc_id)).status_code == 409
    assert seeded["qbo"].create_bill_calls == []
    review = (await owner.get("/api/documents/needs-review")).json()
    assert doc_id not in [x["id"] for x in review]


async def test_reclassify_between_bill_and_filed(seeded, owner):
    doc_id = (await upload(owner, receipt_pdf(), "ace-receipt.pdf")).json()["document_id"]
    d = await detail(owner, doc_id)
    r = await owner.post(f"/api/documents/{doc_id}/update",
                         json={"revision": d["revision"], "reviewed_document_type": "bill"})
    assert r.status_code == 200 and r.json()["status"] == "needs_review"
    d = await detail(owner, doc_id)
    assert d["document_category"] == "bill" and "property" in d["missing_fields"]
    r = await owner.post(f"/api/documents/{doc_id}/update",
                         json={"revision": d["revision"], "reviewed_document_type": "receipt"})
    assert r.status_code == 200 and r.json()["status"] == "filed"
    corrected = [e for e in await history(owner, doc_id) if e["event_type"] == "field_corrected"]
    assert corrected[0]["summary"] == "Corrected document type: Receipt (already paid) → Bill / invoice"
    bad = await owner.post(f"/api/documents/{doc_id}/update",
                           json={"revision": r.json()["revision"], "reviewed_document_type": "banana"})
    assert bad.status_code == 400


async def test_reclassifying_a_recorded_bill_flags_quickbooks_follow_up(seeded, owner):
    doc_id = await upload_fixture(owner, "A")
    assert (await approve(owner, doc_id)).json()["status"] == "recorded"
    d = await detail(owner, doc_id)
    r = await owner.post(f"/api/documents/{doc_id}/update",
                         json={"revision": d["revision"], "reviewed_document_type": "receipt"})
    assert r.status_code == 200 and r.json()["status"] == "recorded"  # posted records never move
    d = await detail(owner, doc_id)
    assert d["qb_discrepancy"] and "document_type" in d["qb_discrepancy"]
    assert seeded["qbo"].bill_count() == 1


# ---------------------------------------------------------------------------
# B. Same invoice, different file
# ---------------------------------------------------------------------------

def photo_of(key: str, **changes) -> bytes:
    return make_pdf(["phone photo of the paper bill"], dict(FIXTURES[key]["values"], **changes))


async def test_photo_of_stored_bill_is_flagged_and_linked(seeded, owner):
    original = await upload_fixture(owner, "A")
    assert (await approve(owner, original)).json()["status"] == "recorded"
    photo = (await upload(owner, photo_of("A"), "IMG_4410.pdf")).json()
    assert photo["is_duplicate"] is False and photo["document_id"] != original
    copy_id = photo["document_id"]
    d = await detail(owner, copy_id)
    assert d["status"] == "possible_copy" and d["copy_of"]["id"] == original
    assert d["copy_of"]["status"] == "recorded" and d["copy_of"]["confirmed"] is False
    assert "same invoice" in d["pending_question"] and "resolve_copy" in d["available_actions"]
    assert copy_id in [x["id"] for x in (await owner.get("/api/documents/needs-review")).json()]
    assert (await approve(owner, copy_id)).status_code == 409

    r = await owner.post(f"/api/documents/{copy_id}/resolve-copy",
                         json={"revision": d["revision"], "resolution": "same"})
    assert r.status_code == 200 and r.json()["status"] == "linked_copy"
    d = await detail(owner, copy_id)
    assert d["copy_of"]["confirmed"] is True and d["available_actions"] == ["view_original", "view_history"]
    assert "copy_linked" in event_types(await history(owner, copy_id))
    assert "copy_linked" in event_types(await history(owner, original))
    edit = await owner.post(f"/api/documents/{copy_id}/update", json={"revision": d["revision"],
                                                                      "reviewed_total": "1.00"})
    assert edit.status_code == 409
    assert seeded["qbo"].bill_count() == 1 and len(seeded["qbo"].create_bill_calls) == 1

    # Questions are answered from the original; the confirmed copy never competes with it.
    res = await ask(owner, "What happened to the power bill?")
    assert res["selected_document_id"] == original and not res["needs_clarification"]
    res = await ask(owner, "tell me about it", select=copy_id)
    assert "confirmed copy" in res["answer"] and "unpaid bill was created" in res["answer"]


async def test_dismissed_copy_is_not_asked_again(seeded, owner):
    original = await upload_fixture(owner, "A")
    copy_id = (await upload(owner, photo_of("A"), "scan.pdf")).json()["document_id"]
    d = await detail(owner, copy_id)
    r = await owner.post(f"/api/documents/{copy_id}/resolve-copy",
                         json={"revision": d["revision"], "resolution": "different"})
    assert r.status_code == 200 and r.json()["status"] == "ready_for_approval"
    d = await detail(owner, copy_id)
    assert d["copy_of"] is None
    r = await owner.post(f"/api/documents/{copy_id}/update", json={"revision": d["revision"], "reviewed_total": "142.51"})
    assert r.json()["status"] == "ready_for_approval"  # still not re-flagged against the dismissed original
    assert "copy_dismissed" in event_types(await history(owner, copy_id))
    stale = await owner.post(f"/api/documents/{copy_id}/resolve-copy",
                             json={"revision": d["revision"], "resolution": "same"})
    assert stale.status_code == 409
    assert original != copy_id


async def test_same_invoice_number_from_another_vendor_is_not_a_copy(seeded, owner):
    await upload_fixture(owner, "A")
    other = (await upload(owner, photo_of("A", vendor_name="City Water Dept", total_amount="61.20"),
                          "water.pdf")).json()["document_id"]
    assert (await detail(owner, other))["status"] != "possible_copy"


async def test_copy_without_invoice_number_needs_vendor_amount_and_date(seeded, owner):
    await upload_fixture(owner, "A")
    no_number = (await upload(owner, photo_of("A", invoice_number=None), "nonum.pdf")).json()["document_id"]
    assert (await detail(owner, no_number))["status"] == "possible_copy"
    other_day = (await upload(owner, photo_of("A", invoice_number=None, invoice_date="2026-11-15"),
                              "nov.pdf")).json()["document_id"]
    assert (await detail(owner, other_day))["status"] != "possible_copy"


async def test_photo_with_misread_invoice_number_is_still_flagged(seeded, owner):
    original = await upload_fixture(owner, "A")
    number = FIXTURES["A"]["values"]["invoice_number"]
    misread = number[:-2] + number[-1]  # one digit dropped, as a screenshot read can do
    copy_id = (await upload(owner, photo_of("A", invoice_number=misread), "shot.png.pdf")).json()["document_id"]
    d = await detail(owner, copy_id)
    assert d["status"] == "possible_copy" and d["copy_of"]["id"] == original


async def test_near_invoice_number_with_different_amount_is_not_a_copy(seeded, owner):
    await upload_fixture(owner, "A")
    number = FIXTURES["A"]["values"]["invoice_number"]
    misread = number[:-2] + number[-1]
    other = (await upload(owner, photo_of("A", invoice_number=misread, total_amount="99.00"),
                          "other.pdf")).json()["document_id"]
    assert (await detail(owner, other))["status"] != "possible_copy"


def test_near_invoice_key():
    from app.services.documents import _near_invoice_key
    assert _near_invoice_key("SM26118667", "SM2611667")
    assert _near_invoice_key("MPL20260415", "MPL20260416")
    assert not _near_invoice_key("MPL20260415", "MPL20270516")
    assert not _near_invoice_key("1001", "1002")  # too short to guess


async def test_earlier_document_is_never_flagged_as_copy_of_later_one(seeded, owner):
    first = await upload_fixture(owner, "A")
    await upload(owner, photo_of("A"), "scan.pdf")
    d = await detail(owner, first)
    r = await owner.post(f"/api/documents/{first}/update", json={"revision": d["revision"], "reviewed_total": "142.50",
                                                                 "property_id": seeded["properties"]["oak"]})
    assert r.json()["status"] == "ready_for_approval"


# ---------------------------------------------------------------------------
# C. Looser descriptions
# ---------------------------------------------------------------------------

def test_parse_amounts_and_dates():
    today = date(2026, 11, 13)
    c = parse_criteria("the $142 bill", today)
    assert c.amounts == [(Decimal("142"), False)] and not c.has_date
    assert parse_criteria("what was the 142.50 one", today).amounts == [(Decimal("142.50"), True)]
    assert parse_criteria("bill for 1,250 dollars", today).amounts == [(Decimal("1250"), False)]
    assert parse_criteria("invoice MPL-2026-0415", today).amounts == []
    c = parse_criteria("last month's water bill", today)
    assert (c.date_from, c.date_to) == (date(2026, 10, 1), date(2026, 10, 31))
    c = parse_criteria("the bill from December", today)  # most recent December
    assert (c.date_from, c.date_to) == (date(2025, 12, 1), date(2025, 12, 31))
    c = parse_criteria("bills in October 2026", today)
    assert c.date_label == "October 2026"
    assert not parse_criteria("you may show it", today).has_date
    assert parse_criteria("bills from may", today).date_from == date(2026, 5, 1)


async def test_find_by_amount(seeded, owner):
    a = await upload_fixture(owner, "A")
    b = await upload_fixture(owner, "B")
    res = await ask(owner, "What happened to the $142 bill?")
    assert res["selected_document_id"] == a
    res = await ask(owner, "the one for 61.20")
    assert res["selected_document_id"] == b
    res = await ask(owner, "the $9999 bill")
    assert res["selected_document_id"] is None and "$9999" in res["answer"]


async def test_amount_and_date_pick_between_similar_bills(seeded, owner):
    a = await upload_fixture(owner, "A")
    nov = dict(FIXTURES["A"]["values"], invoice_number="MPL-2026-0515", invoice_date="2026-11-15",
               due_date="2026-12-05", total_amount="99.10")
    b = (await upload(owner, make_pdf(["Metro Power November"], nov), "nov.pdf")).json()["document_id"]
    assert (await ask(owner, "What happened to the electricity bill?"))["needs_clarification"] is True
    assert (await ask(owner, "the electricity bill for $99"))["selected_document_id"] == b
    assert (await ask(owner, "the electricity bill from October 2026"))["selected_document_id"] == a


async def test_misspelled_question_still_finds_the_bill(seeded, owner):
    a = await upload_fixture(owner, "A")
    await upload_fixture(owner, "B")
    assert (await ask(owner, "What happened to the electrisity bill?"))["selected_document_id"] == a
    assert (await ask(owner, "what about the Metro Powr bill"))["selected_document_id"] == a
    assert (await ask(owner, "the watter bill"))["selected_document_id"] != a


# ---------------------------------------------------------------------------
# D. Corrections by chat
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,field,value", [
    ("This is a receipt", "document_type", "receipt"),
    ("it's actually a lease, not a bill", "document_type", "lease"),
    ("Mark this as a bank statement", "document_type", "bank statement"),
    ("change the document type to other", "document_type", "other"),
    ("Put this under Repairs", "qb_account", "Repairs"),
    ("book it to Utilities.", "qb_account", "Utilities"),
    ("categorize this as Repairs", "qb_account", "Repairs"),
    ("change the expense account to Repairs", "qb_account", "Repairs"),
    ("set the QuickBooks vendor to City Water Dept", "qb_vendor", "City Water Dept"),
    ("change the vendor to City Water Dept", "vendor_name", "City Water Dept"),
])
def test_local_interpreter_reads_corrections(text, field, value):
    out = local_interpret(text, has_context=True)
    assert out["intent"] == "correction" and out["correction_field"] == field and out["correction_value"] == value


def test_match_category():
    assert match_category("a receipt") == "receipt" and match_category("bank statement") == "bank_statement"
    assert match_category("something else") == "other" and match_category("Repairs") is None


async def test_chat_reclassifies_document(seeded, owner):
    doc_id = await upload_fixture(owner, "A")
    res = await ask(owner, "This is a receipt", context=doc_id)
    edit = res["proposed_edit"]
    assert edit["field"] == "document_type" and edit["proposed_raw"] == "receipt"
    assert edit["current_value"] == "Bill / invoice" and "filed with no accounting" in res["answer"]
    assert (await detail(owner, doc_id))["status"] == "ready_for_approval"  # nothing changed yet
    r = await owner.post(f"/api/documents/{doc_id}/update", json={"revision": edit["revision"], **edit["updates"],
                                                                  "source": "voice"})
    assert r.status_code == 200 and r.json()["status"] == "filed"
    again = await ask(owner, "What happened to the power bill?")
    assert "filed as a receipt" in again["answer"]


async def test_chat_changes_expense_account(seeded, owner):
    doc_id = await upload_fixture(owner, "A")
    assert (await detail(owner, doc_id))["qb_account_id"] == "7"
    res = await ask(owner, "Put this under Repairs", context=doc_id)
    edit = res["proposed_edit"]
    assert edit["field"] == "qb_account" and edit["proposed_raw"] == "8" and edit["proposed_value"] == "Repairs"
    assert edit["current_value"] == "Utilities"
    r = await owner.post(f"/api/documents/{doc_id}/update", json={"revision": edit["revision"], **edit["updates"]})
    assert r.status_code == 200 and (await detail(owner, doc_id))["qb_account_id"] == "8"
    none = await ask(owner, "Put this under Marketing", context=doc_id)
    assert none["proposed_edit"] is None and "No QuickBooks account" in none["answer"]


async def test_chat_vendor_change_also_sets_quickbooks_vendor(seeded, owner):
    doc_id = await upload_fixture(owner, "B")
    res = await ask(owner, "change the vendor to Northside Electric Supply", context=doc_id)
    edit = res["proposed_edit"]
    assert edit["updates"] == {"reviewed_vendor_name": "Northside Electric Supply", "qb_vendor_id": "58"}
    res = await ask(owner, "set the QuickBooks vendor to Metro Power", context=doc_id)
    assert res["proposed_edit"]["updates"] == {"qb_vendor_id": "56"}


# ---------------------------------------------------------------------------
# E. Todoist: no second task after a lost reply
# ---------------------------------------------------------------------------

async def test_todoist_adopts_existing_task_and_uses_stable_request_id(seeded, owner, monkeypatch):
    doc_id = await upload_fixture(owner, "B")
    created = []

    async def fake_create(**kwargs):
        created.append(kwargs)
        return {"id": "T-1"}, None

    async def fake_find_none(text):
        return None, None

    monkeypatch.setattr(todoist_service, "is_todoist_enabled", lambda: True)
    monkeypatch.setattr(todoist_service, "create_task", fake_create)
    monkeypatch.setattr(todoist_service, "find_task_by_text", fake_find_none)
    async with async_session_maker() as db:
        ok, task_id, _ = await todoist_service.create_review_task(db, doc_id, "Property not assigned")
    assert ok and task_id == "T-1"
    assert created[0]["request_id"] == todoist_service.task_request_id(doc_id)

    # A second document whose first attempt's reply was lost: the open task is adopted.
    other = await upload_fixture(owner, "C")

    async def fake_find_existing(text):
        assert other in text
        return {"id": "T-LOST", "description": f"**Document:** {text}"}, None

    monkeypatch.setattr(todoist_service, "find_task_by_text", fake_find_existing)
    async with async_session_maker() as db:
        ok, task_id, _ = await todoist_service.create_review_task(db, other, "Review needed")
    assert ok and task_id == "T-LOST" and len(created) == 1
    assert (await detail(owner, other))["todoist_task_id"] == "T-LOST"


def test_todoist_project_id_accepts_url_slug(monkeypatch):
    from app.providers import todoist_provider
    for configured, expected in [
        ("property-demo-6j277vMvXWm8RXgr", "6j277vMvXWm8RXgr"),
        ("https://app.todoist.com/app/project/property-demo-6j277vMvXWm8RXgr", "6j277vMvXWm8RXgr"),
        ("6j277vMvXWm8RXgr", "6j277vMvXWm8RXgr"),
        ("2203306141", "2203306141"),
    ]:
        monkeypatch.setattr(todoist_provider.settings, "todoist_project_id", configured)
        assert todoist_provider.project_id() == expected
    assert todoist_provider.TODOIST_API_URL.endswith("/api/v1")
