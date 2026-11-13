"""Shared helpers for workflow tests."""

import uuid
from email.message import EmailMessage
from typing import List, Optional, Tuple

from httpx import AsyncClient

from .make_fixtures import FIXTURES, fixture_bytes


async def upload(owner: AsyncClient, data: bytes, filename: str, attempt_id: Optional[str] = None,
                 content_type: str = "application/pdf"):
    files = {"file": (filename, data, content_type)}
    form = {"attempt_id": attempt_id or str(uuid.uuid4())}
    return await owner.post("/api/documents/upload", files=files, data=form)


async def upload_fixture(owner: AsyncClient, key: str, filename: Optional[str] = None) -> str:
    r = await upload(owner, fixture_bytes(key), filename or FIXTURES[key]["filename"])
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def detail(owner: AsyncClient, doc_id: str) -> dict:
    r = await owner.get(f"/api/documents/{doc_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def history(owner: AsyncClient, doc_id: str) -> List[dict]:
    r = await owner.get(f"/api/documents/{doc_id}/history")
    assert r.status_code == 200, r.text
    return r.json()


async def approve(owner: AsyncClient, doc_id: str, revision: Optional[int] = None):
    if revision is None:
        revision = (await detail(owner, doc_id))["revision"]
    return await owner.post(f"/api/quickbooks/{doc_id}/approve", json={"revision": revision})


def event_types(entries: List[dict]) -> List[str]:
    return [e["event_type"] for e in entries]


def build_email(sender: str, subject: str, attachments: List[Tuple[str, bytes, str]],
                body: str = "Please find your bill attached.") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "owner@example.test"
    msg["Subject"] = subject
    msg["Date"] = "Thu, 12 Nov 2026 09:30:00 -0500"
    msg.set_content(body)
    msg.add_alternative(f"<p>{body}</p>", subtype="html")
    for filename, data, ctype in attachments:
        maintype, subtype = ctype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()
