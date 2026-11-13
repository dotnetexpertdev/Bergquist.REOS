"""Item 4: OpenAI extraction request shapes (PDF/JPEG/PNG) and failure handling.

The real AsyncOpenAI SDK is used with an httpx MockTransport, so the assertions
check the JSON actually sent over the wire.
"""

import base64
import hashlib
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.providers import openai_provider
from app.config import settings

from .helpers import detail, history, upload, upload_fixture
from .make_fixtures import fixture_bytes

JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00\x10JFIF\x00" + b"\x00" * 64 + bytes([0xFF, 0xD9])


def _completion(content: str) -> dict:
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-4o",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content, "refusal": None}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


class Recorder:
    def __init__(self, status=200, body=None, exc=None):
        self.requests = []
        self.status, self.body, self.exc = status, body, exc

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def openai_recorder(monkeypatch):
    def make(status=200, body=None, exc=None):
        rec = Recorder(status, body if body is not None else _completion(json.dumps({
            "document_type": "utility bill", "vendor_name": "Metro Power & Light", "invoice_number": "MPL-1",
            "invoice_date": "2026-10-15", "due_date": None, "total_amount": "142.50", "currency": None,
            "service_address": None, "confidence": 0.8, "source_text": "Amount due 142.50", "source_page": 1,
            "uncertain_fields": ["currency", "service_address"]})), exc)
        client = AsyncOpenAI(api_key="sk-test-key-not-real", max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)))
        monkeypatch.setattr(openai_provider, "get_openai_client", lambda: client)
        return rec
    return make


def _sent(rec: Recorder) -> dict:
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.url.path.endswith("/chat/completions")
    assert req.headers["authorization"] == "Bearer sk-test-key-not-real"
    return json.loads(req.content)


async def test_pdf_is_sent_as_file_content_part(openai_recorder, tmp_path):
    rec = openai_recorder()
    pdf = fixture_bytes("A")
    path = tmp_path / "bill.pdf"
    path.write_bytes(pdf)
    values, error = await openai_provider.extract_document_data(path, "application/pdf", "vendor-bill.pdf")
    assert error is None
    body = _sent(rec)
    part = body["messages"][1]["content"][0]
    assert part == {"type": "file", "file": {
        "filename": "vendor-bill.pdf",
        "file_data": "data:application/pdf;base64," + base64.standard_b64encode(pdf).decode()}}
    assert not any(p.get("type") == "image_url" for p in body["messages"][1]["content"])
    assert body["model"] == settings.openai_extraction_model
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "untrusted" in body["messages"][0]["content"]
    # Nullable values stay unknown; no default currency is invented.
    assert values["currency"] is None and values["service_address"] is None
    assert str(values["total"]) == "142.50"


@pytest.mark.parametrize("ctype,data", [("image/jpeg", JPEG), ("image/png", bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de")) ])
async def test_images_are_sent_as_image_url(openai_recorder, tmp_path, ctype, data):
    rec = openai_recorder()
    path = tmp_path / "photo.bin"
    path.write_bytes(data)
    _values, error = await openai_provider.extract_document_data(path, ctype, "photo")
    assert error is None
    part = _sent(rec)["messages"][1]["content"][0]
    assert part == {"type": "image_url", "image_url": {
        "url": f"data:{ctype};base64," + base64.standard_b64encode(data).decode(), "detail": "high"}}


def test_api_key_never_leaves_server_side_code():
    import pathlib
    frontend = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"
    for f in frontend.rglob("*.ts*"):
        text = f.read_text(encoding="utf-8")
        assert "api.openai.com" not in text and "import.meta.env" not in text and "sk-" not in text, f


def test_validation_rejects_bad_values():
    v = openai_provider.validate_extraction({"total_amount": "-5", "currency": "dollars", "invoice_date": "soon",
                                             "vendor_name": "  "})
    assert v["total"] is None and v["currency"] is None and v["invoice_date"] is None and v["vendor_name"] is None


# --- workflow-level failure tests (real provider module, failing HTTP) ------------

@pytest.mark.parametrize("mode", ["http_error", "unreadable_json", "network"])
async def test_failed_extraction_keeps_original_and_offers_retry(seeded, owner, monkeypatch, openai_recorder, mode):
    from app.providers import registry
    registry.providers.ai = lambda: openai_provider  # real adapter for this test
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-key-not-real")
    if mode == "http_error":
        openai_recorder(status=500, body={"error": {"message": "server exploded"}})
    elif mode == "unreadable_json":
        openai_recorder(body=_completion("this is not json"))
    else:
        openai_recorder(exc=httpx.ConnectError("no route"))

    pdf = fixture_bytes("A")
    doc_id = await upload_fixture(owner, "A")
    d = await detail(owner, doc_id)
    assert d["status"] == "extraction_failed"
    assert d["status_reason"]
    assert "retry_extraction" in d["available_actions"] and "update_fields" in d["available_actions"]
    assert set(d["missing_fields"]) >= {"vendor_name", "total", "currency", "property"}
    f = await owner.get(f"/api/documents/{doc_id}/file")
    assert f.status_code == 200 and hashlib.sha256(f.content).hexdigest() == hashlib.sha256(pdf).hexdigest()
    assert "extraction_failed" in [e["event_type"] for e in await history(owner, doc_id)]
    listed = (await owner.get("/api/documents/needs-review")).json()
    assert any(item["id"] == doc_id for item in listed)
    assert not seeded["qbo"].create_bill_calls  # reading never posts


async def test_missing_values_remain_visibly_uncertain(seeded, owner, fake_ai):
    """Bill B has no service address: property stays unknown and is asked for."""
    doc_id = await upload_fixture(owner, "B")
    d = await detail(owner, doc_id)
    assert d["status"] == "needs_review"
    assert d["property_id"] is None and "property" in d["missing_fields"]
    assert d["pending_question"] == "Which property is this for?"
    assert "service_address" in d["extraction_uncertain_fields"]
    assert "approve" not in d["available_actions"]
    r = await owner.post(f"/api/quickbooks/{doc_id}/approve", json={"revision": d["revision"]})
    assert r.status_code == 409
    assert not seeded["qbo"].create_bill_calls


async def test_unknown_currency_blocks_approval(seeded, owner, fake_ai, tmp_path):
    from .make_fixtures import FIXTURES, make_pdf
    values = dict(FIXTURES["A"]["values"], currency=None, invoice_number="MPL-NOCUR")
    r = await upload(owner, make_pdf(["no currency"], values), "nocur.pdf")
    doc_id = r.json()["document_id"]
    d = await detail(owner, doc_id)
    assert "currency" in d["missing_fields"] and d["status"] == "needs_review"
    assert d["pending_question"] == "What currency is this bill in?"


async def test_retry_extraction_rereads_stored_original(seeded, owner, fake_ai):
    fake_ai.fail_extraction = "OpenAI extraction request failed: APIConnectionError"
    doc_id = await upload_fixture(owner, "A")
    assert (await detail(owner, doc_id))["status"] == "extraction_failed"
    fake_ai.fail_extraction = None
    r = await owner.post(f"/api/documents/{doc_id}/retry-extraction")
    assert r.status_code == 200
    d = await detail(owner, doc_id)
    assert d["status"] == "ready_for_approval" and d["effective_vendor_name"] == "Metro Power & Light"
