"""Deterministic TEST-ONLY provider adapters.

Nothing under ``app/`` imports this module. It is installed only by the pytest
fixtures and by ``tests/e2e_server.py``.

* :class:`FakeQBOServer` emulates the QuickBooks Online REST endpoints at the
  HTTP level. The production :class:`QuickBooksAPI` client talks to it through an
  ``httpx.MockTransport``, so the real request building, pagination, ``requestid``
  idempotency handling and outcome classification are what get tested.
* :class:`FakeGmail` serves labeled raw messages.
* :class:`FakeAI` returns fixture-defined extraction values, the deterministic
  local question interpreter, and scripted transcripts.
"""

import asyncio
import email
import json
import re
from datetime import date
from decimal import Decimal
from email import policy
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from app.providers import openai_provider
from app.providers.quickbooks_provider import QuickBooksAPI

FIXTURE_MARKER = re.compile(rb"%MA-FIXTURE (\{.*?\})\s*\n")


class FakeQBOServer:
    def __init__(self, realm_id: str = "TEST-REALM", company_name: str = "Sandbox Test Company"):
        self.realm_id = realm_id
        self.company_name = company_name
        self.vendors = {"56": "Metro Power & Light", "57": "City Water Dept", "58": "Northside Electric Supply"}
        self.accounts = [{"Id": "7", "Name": "Utilities", "AccountType": "Expense", "FullyQualifiedName": "Utilities"},
                         {"Id": "8", "Name": "Repairs", "AccountType": "Expense", "FullyQualifiedName": "Repairs"}]
        self.bills: Dict[str, Dict[str, Any]] = {}
        self.attachables: Dict[str, Dict[str, Any]] = {}
        self.contents: Dict[str, bytes] = {}
        self._next = 100
        self.request_log: List[Dict[str, Any]] = []
        self.idempotency: Dict[Tuple[str, str], Tuple[int, Dict[str, Any]]] = {}
        # Fault injection
        self.fail_bill_query_starts: Set[int] = set()  # STARTPOSITION values that fail for Bill queries
        self.fail_attachable_query = False
        self.create_mode = "ok"  # ok | timeout_after_success | timeout_before | reject | server_error_after_success
        self.upload_mode = "ok"  # ok | timeout_after_success | reject | crash
        self.download_fail = False
        self.company_info_fail = False
        self.create_delay = 0.0

    # -- helpers for tests ---------------------------------------------------
    def _id(self) -> str:
        self._next += 1
        return str(self._next)

    def add_bill(self, vendor_id: str, doc_number: Optional[str], total: str, txn_date: str,
                 private_note: str = "", currency: str = "USD") -> str:
        bill_id = self._id()
        self.bills[bill_id] = {
            "Id": bill_id, "DocNumber": doc_number, "TxnDate": txn_date, "TotalAmt": float(total),
            "Balance": float(total), "VendorRef": {"value": vendor_id, "name": self.vendors.get(vendor_id)},
            "CurrencyRef": {"value": currency}, "PrivateNote": private_note,
            "Line": [{"Amount": float(total), "DetailType": "AccountBasedExpenseLineDetail"}],
        }
        return bill_id

    def add_attachment(self, bill_id: str, content: bytes, filename: str, note: str = "",
                       content_type: str = "application/pdf", report_size: bool = True) -> str:
        att_id = self._id()
        att = {"Id": att_id, "FileName": filename, "ContentType": content_type, "Note": note,
               "AttachableRef": [{"EntityRef": {"type": "Bill", "value": bill_id}}],
               "TempDownloadUri": f"https://download.fake.test/att/{att_id}"}
        if report_size:
            att["Size"] = float(len(content))
        self.attachables[att_id] = att
        self.contents[att_id] = content
        return att_id

    def bill_count(self, vendor_id: Optional[str] = None, doc_number: Optional[str] = None) -> int:
        return sum(1 for b in self.bills.values()
                   if (vendor_id is None or b["VendorRef"]["value"] == vendor_id)
                   and (doc_number is None or b["DocNumber"] == doc_number))

    def attachments_for(self, bill_id: str) -> List[Dict[str, Any]]:
        return [a for a in self.attachables.values() if a["AttachableRef"][0]["EntityRef"]["value"] == bill_id]

    def calls(self, method: str, path_suffix: str) -> List[Dict[str, Any]]:
        return [r for r in self.request_log if r["method"] == method and r["path"].endswith(path_suffix)]

    @property
    def create_bill_calls(self) -> List[Dict[str, Any]]:
        return self.calls("POST", "/bill")

    @property
    def upload_calls(self) -> List[Dict[str, Any]]:
        return self.calls("POST", "/upload")

    def client(self, access_token: str = "fake-token") -> QuickBooksAPI:
        return QuickBooksAPI(access_token, self.realm_id, transport=httpx.MockTransport(self.handler))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "bills": list(self.bills.values()),
            "attachments": [{**a, "sha256": __import__("hashlib").sha256(self.contents[a["Id"]]).hexdigest()}
                            for a in self.attachables.values()],
            "create_bill_calls": len(self.create_bill_calls),
            "upload_calls": len(self.upload_calls),
        }

    # -- HTTP emulation -----------------------------------------------------
    @staticmethod
    def _fault(status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"Fault": {"Error": [{"Message": message, "Detail": message}]}})

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "download.fake.test":
            att_id = url.path.rsplit("/", 1)[-1]
            if self.download_fail or att_id not in self.contents:
                return httpx.Response(503, text="download unavailable")
            return httpx.Response(200, content=self.contents[att_id])

        body = request.content
        entry = {"method": request.method, "path": url.path, "params": dict(url.params),
                 "headers": dict(request.headers), "body": body}
        self.request_log.append(entry)
        m = re.match(r"^/v3/company/([^/]+)/(.*)$", url.path)
        if not m or m.group(1) != self.realm_id:
            return self._fault(403, "Wrong company")
        if request.headers.get("authorization", "") != "Bearer fake-token":
            return self._fault(401, "AuthenticationFailed")
        route = m.group(2)

        if request.method == "GET" and route.startswith("companyinfo/"):
            if self.company_info_fail:
                return self._fault(503, "Service unavailable")
            return httpx.Response(200, json={"CompanyInfo": {"Id": self.realm_id, "CompanyName": self.company_name}})
        if request.method == "GET" and route == "query":
            return self._query(url.params.get("query", ""))
        if request.method == "GET" and route.startswith("bill/"):
            bill = self.bills.get(route.split("/", 1)[1])
            return httpx.Response(200, json={"Bill": bill}) if bill else self._fault(400, "Object Not Found")
        if request.method == "GET" and route.startswith("download/"):
            att_id = route.split("/", 1)[1]
            return httpx.Response(200, text=f"https://download.fake.test/att/{att_id}")
        if request.method == "POST" and route == "bill":
            return await self._create_bill(url.params.get("requestid"), json.loads(body))
        if request.method == "POST" and route == "upload":
            return self._upload(url.params.get("requestid"), request.headers.get("content-type", ""), body)
        return self._fault(404, f"Unsupported fake route {request.method} {route}")

    def _query(self, query: str) -> httpx.Response:
        m = re.match(r"SELECT \* FROM (\w+)(?: WHERE (.*?))?(?: ORDERBY \w+)?(?: STARTPOSITION (\d+) MAXRESULTS (\d+))?$",
                     query.strip())
        if not m:
            return self._fault(400, f"Unparseable query {query}")
        entity, where, start, size = m.group(1), m.group(2) or "", int(m.group(3) or 1), int(m.group(4) or 100)
        conds = dict(re.findall(r"([\w.]+) = '?([^' ]+)'?", where))
        if entity == "Bill":
            if start in self.fail_bill_query_starts:
                return self._fault(503, "Temporary failure")
            rows = [b for b in sorted(self.bills.values(), key=lambda b: int(b["Id"]))
                    if "VendorRef" not in conds or b["VendorRef"]["value"] == conds["VendorRef"]]
        elif entity == "Attachable":
            if self.fail_attachable_query:
                return self._fault(503, "Temporary failure")
            rows = [a for a in self.attachables.values()
                    if a["AttachableRef"][0]["EntityRef"]["value"] == conds.get("AttachableRef.EntityRef.value")]
        elif entity == "Vendor":
            rows = [{"Id": k, "DisplayName": v, "Active": True} for k, v in self.vendors.items()]
        elif entity == "Account":
            rows = list(self.accounts)
        elif entity == "Class":
            rows = []
        else:
            return self._fault(400, "Unknown entity")
        page = rows[start - 1:start - 1 + size]
        return httpx.Response(200, json={"QueryResponse": {entity: page, "startPosition": start,
                                                           "maxResults": len(page)}})

    async def _create_bill(self, request_id: Optional[str], payload: Dict[str, Any]) -> httpx.Response:
        if request_id and ("bill", request_id) in self.idempotency:
            status, data = self.idempotency[("bill", request_id)]
            return httpx.Response(status, json=data)
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        mode = self.create_mode
        if mode == "timeout_before":
            raise httpx.ReadTimeout("simulated timeout before processing")
        if mode == "reject":
            return self._fault(400, "Invalid account reference")
        vendor_id = payload["VendorRef"]["value"]
        total = Decimal(str(payload["Line"][0]["Amount"]))
        bill_id = self.add_bill(vendor_id, payload.get("DocNumber"), str(total), payload["TxnDate"],
                                payload.get("PrivateNote", ""))
        if payload.get("DueDate"):
            self.bills[bill_id]["DueDate"] = payload["DueDate"]
        data = {"Bill": self.bills[bill_id]}
        if request_id:
            self.idempotency[("bill", request_id)] = (200, data)
        if mode == "timeout_after_success":
            self.create_mode = "ok"
            raise httpx.ReadTimeout("simulated timeout after QuickBooks created the bill")
        if mode == "server_error_after_success":
            self.create_mode = "ok"
            return self._fault(503, "Gateway timeout")
        return httpx.Response(200, json=data)

    def _upload(self, request_id: Optional[str], content_type: str, body: bytes) -> httpx.Response:
        if request_id and ("upload", request_id) in self.idempotency:
            status, data = self.idempotency[("upload", request_id)]
            return httpx.Response(status, json=data)
        mode = self.upload_mode
        if mode == "crash":
            raise SimulatedCrash("process stopped before the attachment was sent")
        if mode == "reject":
            return self._fault(400, "Attachment rejected")
        msg = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body,
                                       policy=policy.default)
        meta, content, filename = None, b"", "file"
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "file_metadata_01":
                meta = json.loads(part.get_content())
            elif name == "file_content_01":
                content = part.get_payload(decode=True)
                filename = part.get_filename() or filename
        bill_id = meta["AttachableRef"][0]["EntityRef"]["value"]
        if bill_id not in self.bills:
            return self._fault(400, "Bill not found")
        att_id = self.add_attachment(bill_id, content, meta.get("FileName", filename), meta.get("Note", ""),
                                     meta.get("ContentType", "application/pdf"))
        data = {"AttachableResponse": [{"Attachable": self.attachables[att_id]}]}
        if request_id:
            self.idempotency[("upload", request_id)] = (200, data)
        if mode == "timeout_after_success":
            self.upload_mode = "ok"
            raise httpx.ReadTimeout("simulated timeout after QuickBooks stored the attachment")
        return httpx.Response(200, json=data)


class SimulatedCrash(BaseException):
    """Stands in for the process dying (not an Exception, so nothing catches it)."""


class FakeGmail:
    def __init__(self, label_name: str = "Property Demo"):
        self.label_name = label_name
        self.messages: Dict[str, Dict[str, Any]] = {}
        self.fail_message_ids: Set[str] = set()

    def add_message(self, message_id: str, raw: bytes, labeled: bool = True) -> None:
        self.messages[message_id] = {"raw": raw, "labeled": labeled}

    async def get_label_id(self, label_name: str):
        if label_name.lower() != self.label_name.lower():
            return None, f"Gmail label '{label_name}' was not found."
        return "Label_42", None

    async def list_labeled_messages(self, label_id: str):
        return [{"id": mid} for mid, m in self.messages.items() if m["labeled"]], None

    async def get_message_raw(self, message_id: str):
        if message_id in self.fail_message_ids:
            return None, "Temporary Gmail error"
        return self.messages[message_id]["raw"], None


class FakeAI:
    """Deterministic stand-in for app.providers.openai_provider."""

    EXTRACTION_PROMPT_VERSION = "test-fixture"

    def __init__(self):
        self.fail_extraction: Optional[str] = None
        self.transcripts: List[Any] = []  # str -> transcript; Exception/str starting "ERROR:" -> failure
        self.extraction_calls: List[str] = []

    async def extract_document_data(self, file_path, content_type, filename=None):
        self.extraction_calls.append(str(file_path))
        if self.fail_extraction:
            return None, self.fail_extraction
        data = open(file_path, "rb").read()
        m = FIXTURE_MARKER.search(data)
        if not m:
            return None, "The model could not read this document (no fixture data)"
        return openai_provider.validate_extraction(json.loads(m.group(1))), None

    async def interpret_query(self, query: str, has_context: bool):
        return None, "test adapter uses the local interpreter"

    async def transcribe_audio(self, content: bytes, filename: str, content_type: str):
        if not self.transcripts:
            return None, "Transcription failed: no scripted transcript"
        nxt = self.transcripts.pop(0)
        if isinstance(nxt, str) and nxt.startswith("ERROR:"):
            return None, nxt[6:].strip()
        return nxt, None


def install(qbo: Optional[FakeQBOServer], gmail: Optional[FakeGmail], fake_ai: Optional[FakeAI]) -> None:
    from app.providers import registry

    if qbo is not None:
        async def open_qb(db):
            return qbo.client(), None
        registry.providers.open_quickbooks = open_qb
    if gmail is not None:
        async def open_gm(db):
            return gmail, None
        registry.providers.open_gmail = open_gm
    if fake_ai is not None:
        registry.providers.ai = lambda: fake_ai
