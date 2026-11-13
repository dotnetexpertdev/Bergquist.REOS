"""Item 6: approval is durable and safe across double clicks, concurrency, timeouts and restarts."""

import asyncio

import pytest
from sqlalchemy import select

from app.database import async_session_maker
from app.models import AccountingClaim, Document, Operation, OperationType
from app.services import accounting

from .fakes import SimulatedCrash
from .helpers import approve, detail, event_types, history, upload, upload_fixture
from .make_fixtures import FIXTURES, fixture_bytes, make_pdf


async def _ops(doc_id, op_type=None):
    async with async_session_maker() as db:
        q = select(Operation).where(Operation.document_id == doc_id)
        if op_type:
            q = q.where(Operation.operation_type == op_type)
        return list((await db.execute(q.order_by(Operation.created_at))).scalars().all())


async def test_happy_path_persists_before_and_after_remote_write(seeded, owner):
    qbo = seeded["qbo"]
    doc_id = await upload_fixture(owner, "A")
    d = await detail(owner, doc_id)
    assert d["status"] == "ready_for_approval" and d["company"]["verified"] is True
    r = await approve(owner, doc_id, d["revision"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] and body["status"] == "recorded" and body["qb_attachment_id"]
    assert len(qbo.create_bill_calls) == 1
    call = qbo.create_bill_calls[0]
    op = (await _ops(doc_id, OperationType.CREATE_BILL))[0]
    assert call["params"]["requestid"] == op.request_id == op.id  # Intuit idempotency key
    assert op.realm_id == "TEST-REALM" and op.approved_revision == d["revision"]
    assert op.accounting_identity == "TEST-REALM|56|mpl20260415"
    bill = qbo.bills[body["qb_bill_id"]]
    assert bill["Balance"] == bill["TotalAmt"] == 142.5  # unpaid
    assert f"operation {op.id}" in bill["PrivateNote"]
    assert not any("payment" in c["path"].lower() for c in qbo.request_log)
    h = event_types(await history(owner, doc_id))
    assert h.index("approval_granted") < h.index("bill_creation_started") < h.index("bill_created") \
        < h.index("attachment_completed")


async def test_double_click_creates_one_bill(seeded, owner):
    qbo = seeded["qbo"]
    doc_id = await upload_fixture(owner, "A")
    rev = (await detail(owner, doc_id))["revision"]
    first = await approve(owner, doc_id, rev)
    second = await approve(owner, doc_id, rev)
    assert first.status_code == 200 and second.status_code == 409
    assert qbo.bill_count("56", "MPL-2026-0415") == 1 and len(qbo.create_bill_calls) == 1
    assert event_types(await history(owner, doc_id)).count("approval_granted") == 1


async def test_concurrent_approvals_from_two_tabs(seeded, client):
    from .conftest import login
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    qbo = seeded["qbo"]
    qbo.create_delay = 0.2
    tab1 = await login(client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c2:
        tab2 = await login(c2)
        doc_id = await upload_fixture(tab1, "A")
        rev = (await detail(tab1, doc_id))["revision"]
        r1, r2 = await asyncio.gather(approve(tab1, doc_id, rev), approve(tab2, doc_id, rev))
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], (r1.text, r2.text)
    assert qbo.bill_count("56", "MPL-2026-0415") == 1 and len(qbo.create_bill_calls) == 1
    assert len(await _ops(doc_id, OperationType.CREATE_BILL)) == 1


async def test_two_files_for_same_vendor_invoice_cannot_both_post(seeded, owner):
    """A scan and the original PDF of the same invoice: database claim blocks the second."""
    qbo = seeded["qbo"]
    qbo.create_mode = "timeout_before"  # first doc's outcome stays unknown (bill not visible remotely)
    a = await upload_fixture(owner, "A")
    r = await approve(owner, a)
    assert r.status_code == 200 and r.json()["status"] == "remote_outcome_unknown"
    qbo.create_mode = "ok"
    scan = make_pdf(["scanned copy"], dict(FIXTURES["A"]["values"]))  # different bytes, same invoice
    b = (await upload(owner, scan, "scan.pdf")).json()["document_id"]
    assert b != a
    r = await approve(owner, b)
    assert r.status_code == 409 and a in r.json()["detail"]["message"]
    assert qbo.bill_count() == 0 and len(qbo.create_bill_calls) == 1  # only A's (timed-out) attempt
    async with async_session_maker() as db:
        claims = (await db.execute(select(AccountingClaim))).scalars().all()
    assert [c.document_id for c in claims] == [a]
    assert (await detail(owner, b))["status"] == "ready_for_approval"


async def test_timeout_after_remote_success_reconciles_without_second_bill(seeded, owner):
    qbo = seeded["qbo"]
    qbo.create_mode = "timeout_after_success"
    doc_id = await upload_fixture(owner, "A")
    r = await approve(owner, doc_id)
    body = r.json()
    assert body["success"] is False and body["status"] == "remote_outcome_unknown"
    assert body["retry_action"] == "reconcile"
    assert qbo.bill_count() == 1  # QuickBooks did create it
    d = await detail(owner, doc_id)
    assert d["available_actions"].count("reconcile") == 1 and "approve" not in d["available_actions"]
    assert (await approve(owner, doc_id)).status_code == 409  # no blind second create

    r = await owner.post(f"/api/quickbooks/{doc_id}/reconcile")
    assert r.status_code == 200 and r.json()["status"] == "recorded", r.text
    assert qbo.bill_count() == 1 and len(qbo.create_bill_calls) == 1
    assert len(qbo.attachments_for(r.json()["qb_bill_id"])) == 1
    h = event_types(await history(owner, doc_id))
    assert "bill_outcome_unknown" in h and "bill_reconciled" in h and h.count("bill_created") == 1


async def test_timeout_before_processing_resends_same_request_id(seeded, owner):
    qbo = seeded["qbo"]
    qbo.create_mode = "timeout_before"
    doc_id = await upload_fixture(owner, "A")
    assert (await approve(owner, doc_id)).json()["status"] == "remote_outcome_unknown"
    qbo.create_mode = "ok"
    r = await owner.post(f"/api/quickbooks/{doc_id}/reconcile")
    assert r.json()["status"] == "recorded"
    ids = [c["params"]["requestid"] for c in qbo.create_bill_calls]
    assert len(ids) == 2 and ids[0] == ids[1]  # retried with the same operation identity
    assert qbo.bill_count() == 1


async def test_server_error_after_success_is_uncertain_and_idempotent(seeded, owner):
    qbo = seeded["qbo"]
    qbo.create_mode = "server_error_after_success"
    doc_id = await upload_fixture(owner, "A")
    assert (await approve(owner, doc_id)).json()["status"] == "remote_outcome_unknown"
    assert (await owner.post(f"/api/quickbooks/{doc_id}/reconcile")).json()["status"] == "recorded"
    assert qbo.bill_count() == 1


async def test_definite_rejection_releases_approval(seeded, owner):
    qbo = seeded["qbo"]
    qbo.create_mode = "reject"
    doc_id = await upload_fixture(owner, "A")
    r = await approve(owner, doc_id)
    assert r.status_code == 200 and r.json()["success"] is False
    d = await detail(owner, doc_id)
    assert d["status"] == "ready_for_approval" and "did not create" in d["status_reason"]
    async with async_session_maker() as db:
        assert (await db.execute(select(AccountingClaim))).scalars().all() == []
    qbo.create_mode = "ok"
    r = await approve(owner, doc_id, d["revision"])
    assert r.json()["status"] == "recorded" and qbo.bill_count() == 1


async def test_restart_between_bill_creation_and_attachment(seeded, owner):
    qbo = seeded["qbo"]
    qbo.upload_mode = "crash"
    doc_id = await upload_fixture(owner, "A")
    with pytest.raises(SimulatedCrash):
        await approve(owner, doc_id)
    # The bill ID was committed before the upload started.
    async with async_session_maker() as db:
        doc = await db.get(Document, doc_id)
        assert doc.qb_bill_id and doc.status.value == "bill_created"
        bill_id = doc.qb_bill_id
    assert qbo.bill_count() == 1 and qbo.attachments_for(bill_id) == []

    # "Restart": startup recovery with a fresh session.
    qbo.upload_mode = "ok"
    async with async_session_maker() as db:
        assert await accounting.recover_interrupted_operations(db) == 1
    d = await detail(owner, doc_id)
    assert d["status"] == "attachment_pending" and "retry_attachment" in d["available_actions"]
    assert "restarted" in d["status_reason"]

    r = await owner.post(f"/api/quickbooks/{doc_id}/retry-attachment")
    assert r.status_code == 200 and r.json()["status"] == "recorded"
    assert r.json()["qb_bill_id"] == bill_id
    assert [c for c in qbo.upload_calls][-1]["body"].count(f'"value": "{bill_id}"'.encode()) == 1
    assert qbo.bill_count() == 1 and len(qbo.attachments_for(bill_id)) == 1
    h = event_types(await history(owner, doc_id))
    assert "operation_interrupted" in h and h.count("bill_created") == 1 and "attachment_completed" in h


async def test_restart_during_bill_creation_requires_reconcile(seeded, owner):
    """Crash after the operation was committed but before any response: outcome unknown on restart."""
    qbo = seeded["qbo"]
    doc_id = await upload_fixture(owner, "A")
    # Simulate: approval committed, remote call sent, process died before recording the result.
    qbo.create_mode = "ok"
    original = qbo._create_bill

    async def die_after_create(request_id, payload):
        await original(request_id, payload)
        raise SimulatedCrash("died before reading the response")
    qbo._create_bill = die_after_create
    with pytest.raises(SimulatedCrash):
        await approve(owner, doc_id)
    qbo._create_bill = original
    async with async_session_maker() as db:
        assert await accounting.recover_interrupted_operations(db) == 1
    d = await detail(owner, doc_id)
    assert d["status"] == "remote_outcome_unknown" and "reconcile" in d["available_actions"]
    r = await owner.post(f"/api/quickbooks/{doc_id}/reconcile")
    assert r.json()["status"] == "recorded" and qbo.bill_count() == 1
