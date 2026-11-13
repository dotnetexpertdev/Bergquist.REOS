"""Human-readable history entries with source links (US-11)."""

from typing import Any, Dict, List, Optional

from ..models import AuditEvent, Document, EventType
from ..providers.quickbooks_provider import bill_record_url


def _money(details: Dict[str, Any]) -> str:
    total = details.get("total")
    cur = details.get("currency") or ""
    return f"{cur} {total}".strip() if total not in (None, "None") else "unknown amount"


def gmail_message_url(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


def describe(event: AuditEvent, doc: Optional[Document] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = event.details or {}
    t = event.event_type
    links: List[Dict[str, str]] = []
    doc_id = event.document_id
    text = t.value.replace("_", " ").capitalize()

    if t == EventType.DOCUMENT_CREATED:
        size = d.get("file_size")
        text = f"Original stored (SHA-256 {str(d.get('sha256_hash', ''))[:12]}…, {size} bytes)"
        links.append({"label": "Open original", "href": f"/api/documents/{doc_id}/file"})
    elif t == EventType.ARRIVAL_RECORDED:
        name = d.get("original_filename") or "file"
        if d.get("method") == "email":
            text = f"Arrived by email from {d.get('email_from') or 'unknown sender'} as {name}"
            if d.get("gmail_message_id"):
                links.append({"label": "Source email", "href": gmail_message_url(d["gmail_message_id"])})
        else:
            text = f"Uploaded by {event.actor} as {name}"
        links.append({"label": "Open original", "href": f"/api/documents/{doc_id}/file"})
    elif t == EventType.EXTRACTION_STARTED:
        text = f"Reading started ({d.get('model')})"
    elif t == EventType.EXTRACTION_COMPLETED:
        text = (f"Read the bill: vendor {d.get('vendor') or 'unknown'}, invoice {d.get('invoice_number') or 'unknown'}, "
                f"total {_money(d)}")
        if d.get("uncertain_fields"):
            text += f" (uncertain: {', '.join(d['uncertain_fields'])})"
    elif t == EventType.EXTRACTION_FAILED:
        text = f"Reading failed: {d.get('error') or d.get('reason')}"
    elif t == EventType.FIELD_CORRECTED:
        field = d.get("field", "").replace("_", " ")
        old = d.get("old_display", d.get("old_value")) or "empty"
        new = d.get("new_display", d.get("new_value")) or "empty"
        via = " by voice" if d.get("source") == "voice" else ""
        text = f"Corrected {field}{via}: {old} → {new}"
    elif t == EventType.STATUS_CHANGED:
        if d.get("action") == "retry_extraction":
            text = "Asked to read the bill again"
        else:
            text = (f"Status changed: {str(d.get('old_status', '')).replace('_', ' ')} → "
                    f"{str(d.get('new_status', '')).replace('_', ' ')}")
    elif t == EventType.DUPLICATE_CHECK_COMPLETED:
        text = (f"QuickBooks duplicate check completed: {d.get('bills_scanned', 0)} bill(s) for this vendor on "
                f"{d.get('pages_read', 0)} page(s)")
        if d.get("new_matches"):
            text += f"; new possible match(es): {', '.join(d['new_matches'])}"
        if d.get("previously_dismissed_still_present"):
            text += f"; previously dismissed still present: {', '.join(d['previously_dismissed_still_present'])}"
    elif t == EventType.ACCOUNTING_CHECK_FAILED:
        text = f"QuickBooks check failed (nothing posted): {d.get('error')}"
    elif t == EventType.DUPLICATE_DISMISSED:
        text = f"Dismissed possible match with bill {d.get('qb_doc_number') or d.get('qb_bill_id')}: {d.get('reason')}"
    elif t == EventType.LINKED_TO_EXISTING:
        text = f"Linked to existing QuickBooks bill {d.get('qb_doc_number') or d.get('qb_bill_id')} (entered outside this app)"
        if d.get("qb_bill_id"):
            links.append({"label": "QuickBooks bill", "href": bill_record_url(d["qb_bill_id"])})
    elif t == EventType.APPROVAL_GRANTED:
        text = (f"Approved by {event.actor} for {d.get('company_name') or d.get('company_realm_id')}: "
                f"{d.get('vendor_name')}, invoice {d.get('invoice_number')}, {_money(d)}")
    elif t == EventType.BILL_CREATION_STARTED:
        text = "Sending the unpaid bill to QuickBooks"
    elif t == EventType.BILL_CREATED:
        text = f"Unpaid bill created in QuickBooks: bill {d.get('doc_number') or ''} (ID {d.get('qb_bill_id')})"
        links.append({"label": "QuickBooks bill", "href": bill_record_url(d["qb_bill_id"])})
    elif t == EventType.BILL_CREATION_FAILED:
        text = f"QuickBooks did not create the bill: {d.get('error')}"
    elif t == EventType.BILL_OUTCOME_UNKNOWN:
        text = f"QuickBooks did not confirm the bill (outcome unknown): {d.get('error')}"
    elif t == EventType.BILL_RECONCILED:
        if d.get("found"):
            text = f"Checked QuickBooks: found the bill created by this approval (ID {d.get('qb_bill_id')})"
        elif d.get("matching_existing"):
            text = "Checked QuickBooks: not created by this app, but a matching bill exists"
        else:
            text = "Checked QuickBooks: bill not found; re-sending the same request"
    elif t == EventType.ATTACHMENT_STARTED:
        text = f"Uploading the original to bill {d.get('qb_bill_id')}"
    elif t in (EventType.ATTACHMENT_COMPLETED, EventType.ATTACHMENT_VERIFIED):
        verb = "attached" if t == EventType.ATTACHMENT_COMPLETED else "verified on the bill"
        text = f"Original PDF {verb} (attachment {d.get('attachment_id')}; {d.get('evidence')})"
        if d.get("qb_bill_id"):
            links.append({"label": "QuickBooks bill", "href": bill_record_url(d["qb_bill_id"])})
    elif t == EventType.ATTACHMENT_FAILED:
        text = f"Attachment not completed: {d.get('error') or d.get('reason')}"
    elif t == EventType.ATTACHMENT_OUTCOME_UNKNOWN:
        text = f"Attachment upload not confirmed: {d.get('error')}"
    elif t == EventType.OPERATION_INTERRUPTED:
        text = f"App restarted during {str(d.get('operation_type', '')).replace('_', ' ')}; marked for safe resume"
    elif t == EventType.QB_DISCREPANCY_FLAGGED:
        text = f"Local correction differs from QuickBooks bill {d.get('qb_bill_id')}: {', '.join(d.get('fields', []))}"
    elif t == EventType.APPROVAL_RELEASED:
        text = f"Approval released: {d.get('reason')}"
    elif t in (EventType.TASK_CREATED, EventType.TASK_UPDATED, EventType.TASK_CREATION_FAILED):
        text = f"Todoist task {t.value.split('_', 1)[1].replace('_', ' ')}" + (
            f": {d.get('error')}" if d.get("error") else "")

    return {
        "id": event.id,
        "event_type": t.value,
        "occurred_at": event.occurred_at,
        "actor": event.actor,
        "actor_kind": "system" if event.actor == "system" else "owner",
        "summary": text,
        "links": links,
        "details": d or None,
    }
