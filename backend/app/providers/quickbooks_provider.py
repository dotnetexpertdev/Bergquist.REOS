"""QuickBooks Online integration (unpaid vendor bills and attachments only).

Payment APIs are never called.

Reliability notes
-----------------
* Idempotency: every create/upload carries Intuit's ``requestid`` query
  parameter. Intuit returns the original response when the same requestid is
  repeated, so retries of the *same operation* reuse the operation's ID.
* Every write returns a :class:`WriteResult` whose ``outcome`` is one of
  ``ok`` (provider confirmed), ``failed`` (provider definitely did not apply it,
  e.g. a 4xx validation error or a connection that never opened) or ``unknown``
  (the request may have been applied: timeout, dropped connection, 5xx).
* Lookups return a :class:`BillLookup` that is only ``complete`` when every
  page was read successfully.
"""

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..encryption import decrypt_token, encrypt_token
from ..models import ProviderConnection

QUICKBOOKS_SCOPES = ["com.intuit.quickbooks.accounting"]
PAGE_SIZE = 100  # QBO allows up to 1000; small pages keep responses light.
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass
class BillLookup:
    complete: bool
    bills: List[Dict[str, Any]] = field(default_factory=list)
    pages_read: int = 0
    error: Optional[str] = None


@dataclass
class WriteResult:
    outcome: str  # "ok" | "failed" | "unknown"
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


@dataclass
class ReadResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None


def qbo_quote(value: str) -> str:
    """Escape a value for a QBO query string literal."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


# Every bill this app creates carries this text in its PrivateNote (memo).
APP_BILL_MARKER_PREFIX = "Morning Assistant document "


def bill_operation_marker(document_id: str, operation_id: str) -> str:
    return f"{APP_BILL_MARKER_PREFIX}{document_id} operation {operation_id}"


def attachment_note(sha256_hash: str, document_id: str) -> str:
    return f"Morning Assistant original sha256:{sha256_hash} document:{document_id}"


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def get_quickbooks_auth_url(state: str) -> str:
    """Generate the QuickBooks OAuth authorization URL."""
    params = {
        "client_id": settings.quickbooks_client_id,
        "redirect_uri": settings.quickbooks_redirect_uri,
        "response_type": "code",
        "scope": " ".join(QUICKBOOKS_SCOPES),
        "state": state,
    }
    return f"{settings.quickbooks_auth_url}?{urlencode(params)}"


def _basic_auth() -> str:
    raw = f"{settings.quickbooks_client_id}:{settings.quickbooks_client_secret}"
    return base64.b64encode(raw.encode()).decode()


async def exchange_quickbooks_code(code: str, realm_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Exchange authorization code for tokens (same redirect_uri as the authorize step)."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            response = await client.post(
                settings.quickbooks_token_url,
                headers={
                    "Authorization": f"Basic {_basic_auth()}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "code": code,
                    "redirect_uri": settings.quickbooks_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        except httpx.HTTPError as e:
            return None, f"Token exchange error: {type(e).__name__}"
    if response.status_code != 200:
        return None, f"Token exchange failed (HTTP {response.status_code})"
    tokens = response.json()
    tokens["realm_id"] = realm_id
    return tokens, None


async def refresh_quickbooks_token(refresh_token: str) -> Tuple[Optional[Dict], Optional[str]]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            response = await client.post(
                settings.quickbooks_token_url,
                headers={
                    "Authorization": f"Basic {_basic_auth()}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"refresh_token": refresh_token, "grant_type": "refresh_token"},
            )
        except httpx.HTTPError as e:
            return None, f"Token refresh error: {type(e).__name__}"
    if response.status_code != 200:
        return None, f"QuickBooks authorization expired (HTTP {response.status_code}). Reconnect QuickBooks."
    return response.json(), None


async def get_quickbooks_tokens(db: AsyncSession) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (access_token, realm_id, error); refreshes and persists tokens when needed."""
    result = await db.execute(select(ProviderConnection).where(ProviderConnection.provider == "quickbooks"))
    conn = result.scalar_one_or_none()
    if not conn or not conn.is_valid:
        return None, None, "QuickBooks not connected"

    access_token = decrypt_token(conn.access_token_encrypted) if conn.access_token_encrypted else None
    refresh_token = decrypt_token(conn.refresh_token_encrypted) if conn.refresh_token_encrypted else None
    if not access_token or not refresh_token:
        return None, None, "QuickBooks tokens not available. Reconnect QuickBooks."

    if conn.token_expiry and datetime.utcnow() >= (conn.token_expiry - timedelta(minutes=5)):
        tokens, error = await refresh_quickbooks_token(refresh_token)
        if error:
            conn.is_valid = False
            conn.last_error = error
            await db.commit()
            return None, None, error
        conn.access_token_encrypted = encrypt_token(tokens["access_token"])
        conn.token_expiry = datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))
        conn.last_refreshed_at = datetime.utcnow()
        if "refresh_token" in tokens:
            conn.refresh_token_encrypted = encrypt_token(tokens["refresh_token"])
        access_token = tokens["access_token"]
        await db.commit()  # persist refreshed credentials immediately
    return access_token, conn.realm_id, None


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class QuickBooksAPI:
    """Thin async client bound to one access token and one company (realm)."""

    def __init__(self, access_token: str, realm_id: str, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.access_token = access_token
        self.realm_id = realm_id
        self._transport = transport

    # -- helpers ------------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT, transport=self._transport)

    def _url(self, path: str) -> str:
        return f"{settings.quickbooks_base_url}/v3/company/{self.realm_id}/{path}"

    def _headers(self, content_type: Optional[str] = "application/json") -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _params(self, request_id: Optional[str] = None) -> Dict[str, str]:
        params = {"minorversion": settings.quickbooks_minor_version}
        if request_id:
            params["requestid"] = request_id
        return params

    @staticmethod
    def _fault(response: httpx.Response) -> str:
        try:
            errors = response.json().get("Fault", {}).get("Error", [])
            if errors:
                e = errors[0]
                return f"{e.get('Message', 'Error')}: {e.get('Detail', '')}".strip(": ")
        except Exception:
            pass
        return f"HTTP {response.status_code}"

    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> ReadResult:
        try:
            async with self._client() as client:
                response = await client.get(self._url(path), headers=self._headers(None),
                                            params={**self._params(), **(params or {})})
        except httpx.HTTPError as e:
            return ReadResult(False, error=f"QuickBooks request failed: {type(e).__name__}")
        if response.status_code != 200:
            return ReadResult(False, error=f"QuickBooks returned {self._fault(response)}")
        return ReadResult(True, data=response.json())

    async def _query(self, query: str) -> ReadResult:
        return await self._get("query", {"query": query})

    async def _write(self, path: str, request_id: Optional[str], extra_params: Optional[Dict[str, str]] = None,
                     **kwargs) -> WriteResult:
        try:
            async with self._client() as client:
                response = await client.post(self._url(path), params={**self._params(request_id),
                                                                      **(extra_params or {})}, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # The connection never opened: QuickBooks cannot have received the request.
            return WriteResult("failed", error=f"Could not reach QuickBooks ({type(e).__name__})")
        except httpx.HTTPError as e:
            return WriteResult("unknown", error=f"No response from QuickBooks ({type(e).__name__})")
        if response.status_code in (200, 201):
            return WriteResult("ok", data=response.json())
        if response.status_code >= 500:
            return WriteResult("unknown", error=f"QuickBooks server error {self._fault(response)}")
        return WriteResult("failed", error=f"QuickBooks rejected the request: {self._fault(response)}")

    # -- reads --------------------------------------------------------------
    async def get_company_info(self) -> ReadResult:
        result = await self._get(f"companyinfo/{self.realm_id}")
        if result.ok:
            result.data = result.data.get("CompanyInfo")
        return result

    async def _paged(self, base_query: str, entity: str) -> Tuple[List[Dict], Optional[str], int]:
        rows: List[Dict] = []
        start = 1
        pages = 0
        while True:
            result = await self._query(f"{base_query} STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}")
            if not result.ok:
                return rows, f"{result.error} (page {pages + 1})", pages
            pages += 1
            batch = result.data.get("QueryResponse", {}).get(entity, [])
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                return rows, None, pages
            start += PAGE_SIZE

    async def get_vendors(self) -> Tuple[List[Dict], Optional[str]]:
        rows, err, _ = await self._paged("SELECT * FROM Vendor WHERE Active = true", "Vendor")
        return rows, err

    async def get_accounts(self) -> Tuple[List[Dict], Optional[str]]:
        rows, err, _ = await self._paged("SELECT * FROM Account WHERE AccountType = 'Expense'", "Account")
        return rows, err

    async def get_classes(self) -> Tuple[List[Dict], Optional[str]]:
        rows, err, _ = await self._paged("SELECT * FROM Class", "Class")
        return rows, err

    async def find_vendor_bills(self, vendor_id: str) -> BillLookup:
        """All bills for one vendor, every page. Matching happens locally."""
        rows, err, pages = await self._paged(
            f"SELECT * FROM Bill WHERE VendorRef = '{qbo_quote(vendor_id)}' ORDERBY Id", "Bill"
        )
        if err:
            return BillLookup(complete=False, bills=[], pages_read=pages, error=err)
        return BillLookup(complete=True, bills=rows, pages_read=pages)

    async def find_app_bills(self) -> BillLookup:
        """Every bill in the company that this app created (recognised by its memo marker)."""
        rows, err, pages = await self._paged("SELECT * FROM Bill ORDERBY Id", "Bill")
        if err:
            return BillLookup(complete=False, bills=[], pages_read=pages, error=err)
        ours = [b for b in rows if APP_BILL_MARKER_PREFIX in (b.get("PrivateNote") or "")]
        return BillLookup(complete=True, bills=ours, pages_read=pages)

    async def get_bill(self, bill_id: str) -> ReadResult:
        result = await self._get(f"bill/{bill_id}")
        if result.ok:
            result.data = result.data.get("Bill")
        return result

    async def list_attachments(self, entity_type: str, entity_id: str) -> ReadResult:
        rows, err, _ = await self._paged(
            "SELECT * FROM Attachable WHERE AttachableRef.EntityRef.Type = "
            f"'{qbo_quote(entity_type)}' AND AttachableRef.EntityRef.value = '{qbo_quote(entity_id)}'",
            "Attachable",
        )
        if err:
            return ReadResult(False, error=err)
        return ReadResult(True, data=rows)

    async def download_attachment(self, attachable: Dict[str, Any]) -> ReadResult:
        """Return the attachment bytes (via the documented /download/{id} temporary URL)."""
        url = attachable.get("TempDownloadUri")
        try:
            async with self._client() as client:
                if not url:
                    response = await client.get(self._url(f"download/{attachable['Id']}"),
                                                headers=self._headers(None), params=self._params())
                    if response.status_code != 200:
                        return ReadResult(False, error=f"Download link failed: {self._fault(response)}")
                    url = response.text.strip()
                response = await client.get(url, follow_redirects=True)
        except httpx.HTTPError as e:
            return ReadResult(False, error=f"Attachment download failed: {type(e).__name__}")
        if response.status_code != 200:
            return ReadResult(False, error=f"Attachment download failed: HTTP {response.status_code}")
        return ReadResult(True, data=response.content)

    # -- writes -------------------------------------------------------------
    async def create_bill(self, payload: Dict[str, Any], request_id: str) -> WriteResult:
        result = await self._write("bill", request_id, headers=self._headers(), json=payload)
        if result.ok:
            bill = (result.data or {}).get("Bill")
            if not bill or not bill.get("Id"):
                return WriteResult("unknown", error="QuickBooks response did not include a bill ID")
            result.data = bill
        return result

    async def delete_entity(self, entity: str, entity_id: str, sync_token: str) -> WriteResult:
        """Delete a Bill or Attachable (demo reset only). ``entity`` is the lower-case endpoint name."""
        return await self._write(entity, None, extra_params={"operation": "delete"}, headers=self._headers(),
                                 json={"Id": str(entity_id), "SyncToken": str(sync_token)})

    async def upload_attachment(self, *, entity_type: str, entity_id: str, content: bytes, filename: str,
                                content_type: str, note: str, request_id: str) -> WriteResult:
        boundary = f"----MorningAssistant{uuid.uuid4().hex}"
        metadata = {
            "AttachableRef": [{"EntityRef": {"type": entity_type, "value": entity_id}}],
            "FileName": filename,
            "ContentType": content_type,
            "Note": note,
        }
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file_metadata_01"\r\n'
            "Content-Type: application/json\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file_content_01"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        result = await self._write(
            "upload", request_id,
            headers=self._headers(f"multipart/form-data; boundary={boundary}"), content=body,
        )
        if result.ok:
            items = (result.data or {}).get("AttachableResponse", [])
            attachable = items[0].get("Attachable") if items else None
            if not attachable:
                fault = items[0].get("Fault") if items else None
                if fault:
                    return WriteResult("failed", error=f"QuickBooks rejected the attachment: {fault}")
                return WriteResult("unknown", error="QuickBooks response did not include the attachment")
            result.data = attachable
        return result


def build_bill_payload(*, vendor_id: str, account_id: str, doc_number: str, txn_date: date,
                       due_date: Optional[date], total: Decimal, class_id: Optional[str],
                       currency: Optional[str], memo: str) -> Dict[str, Any]:
    line_detail: Dict[str, Any] = {"AccountRef": {"value": account_id}}
    if class_id:
        line_detail["ClassRef"] = {"value": class_id}
    payload: Dict[str, Any] = {
        "VendorRef": {"value": vendor_id},
        "DocNumber": doc_number,
        "TxnDate": txn_date.isoformat(),
        "PrivateNote": memo,
        "Line": [{
            "DetailType": "AccountBasedExpenseLineDetail",
            "Amount": float(Decimal(total).quantize(Decimal("0.01"))),
            "AccountBasedExpenseLineDetail": line_detail,
        }],
    }
    if due_date:
        payload["DueDate"] = due_date.isoformat()
    if currency and currency != settings.quickbooks_home_currency:
        payload["CurrencyRef"] = {"value": currency}
    return payload


def payload_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def bill_record_url(bill_id: str) -> str:
    """Company-specific link to the bill in the QuickBooks web app (opens the signed-in company)."""
    return f"{settings.quickbooks_app_url}/app/bill?txnId={bill_id}"


# Backwards-compatible helpers used by the settings screen.
async def get_company_info(access_token: str, realm_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    result = await QuickBooksAPI(access_token, realm_id).get_company_info()
    return (result.data, None) if result.ok else (None, result.error)
