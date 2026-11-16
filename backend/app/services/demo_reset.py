"""Reset the application to a clean state for a demo.

Removes, in this order:

1. QuickBooks bills **created by this app** (recognised by the marker the app writes
   into each bill's memo) together with their attachments. Bills entered in
   QuickBooks by hand, including ones a document was merely linked to, are never
   touched. Only the verified test company can be cleaned.
2. Todoist review tasks the app opened (closed, best effort).
3. Every stored document, arrival, history entry and accounting record, and the
   stored original files and raw emails.

Connections, properties and vendor/account mappings are kept. A zip backup of the
data folder is written first. If the QuickBooks step cannot be completed, the local
data is left untouched so the app and QuickBooks never drift apart silently.
"""

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Document
from ..providers.todoist_provider import close_task, is_todoist_enabled
from .accounting import AccountingError, open_verified_company

CONFIRM_WORD = "RESET"

# Rows that belong to stored documents, children first (foreign keys are enforced).
DOCUMENT_TABLES = ("duplicate_candidates", "accounting_claims", "operations", "audit_events", "arrivals", "documents")


class ResetError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _folders() -> List[Path]:
    return [settings.documents_path, settings.emails_path, settings.temp_path]


def _count_files() -> int:
    return sum(1 for folder in _folders() if folder.exists() for f in folder.rglob("*") if f.is_file())


def _bill_summary(bill: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(bill.get("Id")),
        "doc_number": bill.get("DocNumber"),
        "vendor_name": (bill.get("VendorRef") or {}).get("name"),
        "total": str(bill.get("TotalAmt")) if bill.get("TotalAmt") is not None else None,
        "txn_date": bill.get("TxnDate"),
    }


async def _local_counts(db: AsyncSession) -> Dict[str, int]:
    counts = {}
    for table in DOCUMENT_TABLES:
        counts[table] = (await db.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0
    return counts


async def _todoist_task_ids(db: AsyncSession) -> List[str]:
    return list((await db.execute(
        select(Document.todoist_task_id).where(Document.todoist_task_id.is_not(None))
    )).scalars().all())


async def preview(db: AsyncSession) -> Dict[str, Any]:
    """What a reset would remove. Read-only."""
    counts = await _local_counts(db)
    task_ids = await _todoist_task_ids(db)
    await db.commit()

    quickbooks: Dict[str, Any] = {"available": False, "problem": None, "company_name": None, "bills": []}
    try:
        client, company_name = await open_verified_company(db)
        lookup = await client.find_app_bills()
        if lookup.complete:
            quickbooks.update(available=True, company_name=company_name,
                              bills=[_bill_summary(b) for b in lookup.bills])
        else:
            quickbooks["problem"] = f"Could not list QuickBooks bills ({lookup.error})"
    except AccountingError as e:
        quickbooks["problem"] = e.message

    return {
        "confirm_word": CONFIRM_WORD,
        "documents": counts["documents"],
        "arrivals": counts["arrivals"],
        "history_events": counts["audit_events"],
        "stored_files": _count_files(),
        "todoist_tasks": len(task_ids) if is_todoist_enabled() else 0,
        "quickbooks": quickbooks,
    }


async def _delete_quickbooks_bills(db: AsyncSession) -> Dict[str, Any]:
    """Delete the app-created bills and their attachments. Raises if any bill remains."""
    try:
        client, _company = await open_verified_company(db)
    except AccountingError as e:
        raise ResetError(e.status_code, f"{e.message} Nothing was deleted. To clean only this app, untick "
                                        "the QuickBooks option.")
    lookup = await client.find_app_bills()
    if not lookup.complete:
        raise ResetError(502, f"Could not list QuickBooks bills ({lookup.error}). Nothing was deleted; try again.")

    deleted: List[str] = []
    attachments_deleted = 0
    failures: List[str] = []
    for bill in lookup.bills:
        bill_id = str(bill.get("Id"))
        label = bill.get("DocNumber") or bill_id
        # Attachments first: deleting a bill leaves its files behind in QuickBooks otherwise.
        listing = await client.list_attachments("Bill", bill_id)
        for att in (listing.data or []) if listing.ok else []:
            result = await client.delete_entity("attachable", att.get("Id"), att.get("SyncToken", "0"))
            attachments_deleted += int(result.ok)
        result = await client.delete_entity("bill", bill_id, bill.get("SyncToken", "0"))
        if result.ok:
            deleted.append(bill_id)
        else:
            failures.append(f"bill {label}: {result.error}")
    if failures:
        raise ResetError(502, f"QuickBooks did not delete {len(failures)} bill(s): {'; '.join(failures)}. "
                              f"{len(deleted)} bill(s) were deleted. This app's data was not changed; "
                              "fix the bill in QuickBooks and reset again.")
    return {"bills_deleted": len(deleted), "attachments_deleted": attachments_deleted}


async def _close_todoist_tasks(task_ids: List[str]) -> int:
    closed = 0
    for task_id in task_ids:
        ok, _error = await close_task(task_id)
        closed += int(ok)
    return closed


def _backup() -> Optional[str]:
    if not settings.data_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return shutil.make_archive(str(settings.data_path.parent / f"backup-{stamp}"), "zip",
                               root_dir=str(settings.data_path))


def _delete_files() -> int:
    """Remove stored originals, raw emails and temp files. Returns how many could not be removed."""
    left = 0
    for folder in _folders():
        if not folder.exists():
            continue
        for child in folder.iterdir():
            try:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            except OSError:
                left += 1
        folder.mkdir(parents=True, exist_ok=True)
    return left


async def reset(db: AsyncSession, delete_quickbooks: bool) -> Dict[str, Any]:
    counts = await _local_counts(db)
    task_ids = await _todoist_task_ids(db)
    await db.commit()  # never hold the write lock across network calls

    quickbooks = {"bills_deleted": 0, "attachments_deleted": 0, "skipped": not delete_quickbooks}
    if delete_quickbooks:
        quickbooks.update(await _delete_quickbooks_bills(db))

    tasks_closed = await _close_todoist_tasks(task_ids) if is_todoist_enabled() else 0
    files = _count_files()
    backup = await asyncio.to_thread(_backup)

    await db.execute(text("UPDATE documents SET copy_of_document_id = NULL"))
    for table in DOCUMENT_TABLES:
        await db.execute(text(f"DELETE FROM {table}"))
    await db.commit()
    files_left = await asyncio.to_thread(_delete_files)

    return {
        "success": True,
        "documents_deleted": counts["documents"],
        "arrivals_deleted": counts["arrivals"],
        "history_events_deleted": counts["audit_events"],
        "files_deleted": files - files_left,
        "files_left": files_left,
        "todoist_tasks_closed": tasks_closed,
        "quickbooks": quickbooks,
        "backup": Path(backup).name if backup else None,
    }
