"""Read-only assistant: find a bill from everyday language and explain its history.

OpenAI (or the deterministic local interpreter when OpenAI is unavailable) only
turns the question into validated criteria. Candidate selection, the answer
text, links and status wording are all produced here from stored records, so
every answer is grounded in the saved document and its history. Corrections are
returned as *proposals*; nothing is changed until the owner confirms through the
normal versioned update endpoint.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..models import Document, DocumentStatus, Property
from ..providers.openai_provider import parse_currency, parse_date, parse_decimal
from ..providers.quickbooks_provider import bill_record_url
from ..providers.registry import ai
from .documents import POSTED_STATUSES, POSTING_STATUSES, get_document_history, get_pending_question
from .history import describe

SYNONYMS: Dict[str, List[str]] = {
    "power": ["electric", "electricity", "energy", "power"],
    "electric": ["electric", "electricity", "energy", "power"],
    "electricity": ["electric", "electricity", "energy", "power"],
    "energy": ["electric", "electricity", "energy", "power"],
    "water": ["water", "sewer"],
    "sewer": ["water", "sewer"],
    "gas": ["gas"],
    "internet": ["internet", "broadband", "cable", "fiber"],
    "trash": ["trash", "waste", "garbage", "refuse"],
    "garbage": ["trash", "waste", "garbage", "refuse"],
}
STOP_WORDS = set("""
a an and are about any bill bills by can could did do does for from had has have how i if in is it its
me my of on or our please show tell that the their them there this to was we what whats what's when where
which who why with you your happened happen status latest last recent invoice please paid pay
""".split())

FIELD_LABELS = {
    "property": "Property", "vendor_name": "Vendor", "invoice_number": "Invoice number",
    "invoice_date": "Invoice date", "due_date": "Due date", "total": "Total", "currency": "Currency",
}
UPDATE_KEYS = {
    "property": "property_id", "vendor_name": "reviewed_vendor_name", "invoice_number": "reviewed_invoice_number",
    "invoice_date": "reviewed_invoice_date", "due_date": "reviewed_due_date", "total": "reviewed_total",
    "currency": "reviewed_currency",
}


class Interpretation(BaseModel):
    intent: str = "lookup"
    search_terms: List[str] = []
    asked_field: Optional[str] = None
    correction_field: Optional[str] = None
    correction_value: Optional[str] = None


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------

_CORRECTION = re.compile(
    r"^\s*(?:please\s+)?(?:change|set|update|correct|make)\s+(?:the\s+)?"
    r"(?P<field>property|vendor(?:\s+name)?|invoice\s+number|invoice\s+date|due\s+date|amount|total|currency)"
    r"(?:\s+(?:on|for|of)\s+(?:this|that|the)\s+bill)?\s+(?:to|as|=)\s+(?P<value>.+?)[.!]?\s*$",
    re.IGNORECASE,
)
_FIELD_ALIASES = {
    "property": "property", "vendor": "vendor_name", "vendor name": "vendor_name",
    "invoice number": "invoice_number", "invoice date": "invoice_date", "due date": "due_date",
    "amount": "total", "total": "total", "currency": "currency",
}


def _asked_field(q: str) -> Optional[str]:
    q = q.lower()
    if re.search(r"\b(property|building|which address|what address)\b", q):
        return "property"
    if re.search(r"\b(how much|amount|total|cost)\b", q):
        return "amount"
    if re.search(r"\bdue\b", q):
        return "due_date"
    if re.search(r"\bvendor|who sent\b", q):
        return "vendor"
    if re.search(r"\b(happened|status|history)\b", q):
        return "history"
    return None


def local_interpret(query: str, has_context: bool) -> Dict[str, Any]:
    """Deterministic interpreter (used when OpenAI is not configured or fails)."""
    m = _CORRECTION.match(query)
    if m:
        field = _FIELD_ALIASES[re.sub(r"\s+", " ", m.group("field").lower())]
        return {"intent": "correction", "search_terms": [], "asked_field": None,
                "correction_field": field, "correction_value": m.group("value").strip()}
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]*", query.lower())]
    terms: List[str] = []
    for w in words:
        if w in STOP_WORDS:
            continue
        for t in SYNONYMS.get(w, [w]):
            if t not in terms:
                terms.append(t)
    refers_back = bool(re.search(r"\b(it|that bill|this bill|that one|this one)\b", query.lower()))
    intent = "followup" if has_context and (refers_back or not terms) else "lookup"
    return {"intent": intent, "search_terms": terms, "asked_field": _asked_field(query),
            "correction_field": None, "correction_value": None}


async def interpret(query: str, has_context: bool) -> Tuple[Interpretation, str]:
    data, error = await ai().interpret_query(query, has_context)
    source = "openai"
    if error or not data:
        data, source = local_interpret(query, has_context), "local"
    try:
        interp = Interpretation.model_validate(data)
    except ValidationError:
        interp, source = Interpretation.model_validate(local_interpret(query, has_context)), "local"
    if interp.intent not in ("lookup", "followup", "correction"):
        interp.intent = "lookup"
    if interp.correction_field and interp.correction_field not in FIELD_LABELS:
        interp.correction_field = None
        interp.intent = "lookup"
    # Always include synonyms so "power" finds an electricity bill.
    expanded: List[str] = []
    for t in interp.search_terms:
        for s in SYNONYMS.get(t.lower(), [t.lower()]):
            if s not in expanded and s not in STOP_WORDS:
                expanded.append(s)
    interp.search_terms = expanded
    return interp, source


# ---------------------------------------------------------------------------
# Retrieval and grounded answers
# ---------------------------------------------------------------------------

def _score(doc: Document, terms: List[str]) -> Tuple[int, int]:
    """(score on bill facts, score on filenames/subjects). Facts decide; names only break a zero."""
    facts = [
        (doc.effective_vendor_name, 3), (doc.extracted_document_type, 2), (doc.effective_invoice_number, 3),
        (doc.extracted_service_address, 1), (doc.property_name, 2),
    ]
    names = [(a.original_filename, 1) for a in doc.arrivals] + [(a.email_subject, 1) for a in doc.arrivals]

    def score(fields):
        total = 0
        for term in terms:
            pattern = re.compile(rf"\b{re.escape(term)}", re.IGNORECASE)
            total += sum(weight for text, weight in fields if text and pattern.search(text))
        return total
    return score(facts), score(names)


def _local_time(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown time"
    try:
        tz = ZoneInfo(settings.owner_timezone)
    except Exception:
        tz = timezone.utc
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%b %d, %Y %I:%M %p %Z")


def _money(doc: Document) -> str:
    if doc.effective_total is None:
        return "an unknown amount"
    return f"{doc.effective_currency or '(currency unknown)'} {doc.effective_total}"


def _status_sentence(doc: Document) -> str:
    ref = doc.qb_bill_doc_number or doc.qb_bill_id
    s = doc.status
    if s == DocumentStatus.RECORDED:
        return (f"An unpaid bill was created in QuickBooks (bill {ref}) and the original PDF is attached. "
                "That is not a payment; paying it happens outside this app.")
    if s == DocumentStatus.LINKED_EXISTING:
        return (f"It is linked to QuickBooks bill {ref}, which was already in QuickBooks; this app did not create "
                "another bill. Linking does not mean it is paid.")
    if s == DocumentStatus.ATTACHMENT_PENDING:
        return f"An unpaid bill was created in QuickBooks (bill {ref}), but the PDF is not attached yet."
    if s == DocumentStatus.REMOTE_OUTCOME_UNKNOWN:
        return "QuickBooks did not confirm whether the bill was created; it needs to be checked before anything else."
    if s in (DocumentStatus.APPROVED, DocumentStatus.BILL_CREATED):
        return "It was approved and is being recorded in QuickBooks."
    if s == DocumentStatus.READY_FOR_APPROVAL:
        return "It has been reviewed and is waiting for your approval. Nothing has been posted to QuickBooks."
    if s == DocumentStatus.DUPLICATE_CANDIDATE:
        return "A possibly matching bill was found in QuickBooks and needs your decision. Nothing has been posted."
    if s == DocumentStatus.EXTRACTION_FAILED:
        return "It could not be read automatically and is waiting for review. Nothing has been posted."
    question = get_pending_question(doc)
    return f"It needs your review{': ' + question if question else ''}. Nothing has been posted to QuickBooks."


def _arrivals_sentence(doc: Document) -> str:
    parts = []
    for a in sorted(doc.arrivals, key=lambda x: x.arrived_at):
        if a.method.value == "email":
            parts.append(f"by email from {a.email_from or 'unknown sender'} on {_local_time(a.arrived_at)}")
        else:
            parts.append(f"by upload as {a.original_filename or 'a file'} on {_local_time(a.arrived_at)}")
    if not parts:
        return ""
    n = len(parts)
    head = "It arrived once" if n == 1 else f"It arrived {n} times (one stored original)"
    return f"{head}: " + "; ".join(parts) + "."


def _doc_title(doc: Document) -> str:
    vendor = doc.effective_vendor_name or "Unknown vendor"
    inv = f" invoice {doc.effective_invoice_number}" if doc.effective_invoice_number else ""
    return f"{vendor}{inv}"


def result_document(doc: Document) -> Dict[str, Any]:
    return {
        "id": doc.id, "vendor_name": doc.effective_vendor_name, "invoice_number": doc.effective_invoice_number,
        "total": doc.effective_total, "currency": doc.effective_currency, "status": doc.status.value,
        "property_name": doc.property_name, "qb_bill_id": doc.qb_bill_id,
        "qb_bill_doc_number": doc.qb_bill_doc_number,
        "invoice_date": doc.effective_invoice_date, "arrival_count": len(doc.arrivals),
        "links": {
            "record": f"/documents/{doc.id}", "original": f"/api/documents/{doc.id}/file",
            "quickbooks": bill_record_url(doc.qb_bill_id) if doc.qb_bill_id else None,
        },
    }


def grounded_answer(doc: Document, asked: Optional[str]) -> str:
    title = _doc_title(doc)
    lead = ""
    if asked == "property":
        lead = (f"The {title} bill is for {doc.property_name}. " if doc.property_name
                else f"No property has been assigned to the {title} bill yet. ")
    elif asked == "amount":
        lead = f"The {title} bill is for {_money(doc)}. "
    elif asked == "due_date":
        lead = (f"The {title} bill is due {doc.effective_due_date.isoformat()}. " if doc.effective_due_date
                else f"No due date is recorded for the {title} bill. ")
    elif asked == "vendor":
        lead = f"The vendor is {doc.effective_vendor_name or 'not known yet'}. "
    body = (f"{'' if lead else f'The {title} bill, '}{_money(doc)}"
            f"{', for ' + doc.property_name if doc.property_name else ', with no property assigned'}. ")
    if lead:
        body = ""
    return (lead + body + _status_sentence(doc) + " " + _arrivals_sentence(doc)).strip()


async def _property_matches(db: AsyncSession, value: str) -> List[Property]:
    needle = re.sub(r"\s+", " ", value.strip().lower())
    props = (await db.execute(select(Property).order_by(Property.name))).scalars().all()
    exact = [p for p in props if p.name.lower() == needle]
    if exact:
        return exact
    return [p for p in props if needle in p.name.lower() or (p.address and needle in p.address.lower())]


async def propose_correction(db: AsyncSession, doc: Document, field: str, value: str) -> Tuple[Optional[Dict[str, Any]], str]:
    label = FIELD_LABELS[field]
    title = _doc_title(doc)
    if doc.status in POSTING_STATUSES:
        return None, f"The {title} bill is being recorded in QuickBooks right now, so it can't be edited yet."
    if doc.status in (DocumentStatus.RECEIVED, DocumentStatus.READING):
        return None, f"The {title} bill is still being read. Try the correction again in a moment."

    current_display: Optional[str]
    proposed_raw: Any
    proposed_display: str
    if field == "property":
        matches = await _property_matches(db, value)
        if not matches:
            names = ", ".join(p.name for p in (await db.execute(select(Property))).scalars().all()) or "none"
            return None, f"No property matches “{value}”. Known properties: {names}. Nothing was changed."
        if len(matches) > 1:
            return None, (f"“{value}” matches several properties ({', '.join(p.name for p in matches)}). "
                          "Say the full property name. Nothing was changed.")
        current_display, proposed_raw, proposed_display = doc.property_name, matches[0].id, matches[0].name
        if doc.property_id == proposed_raw:
            return None, f"The {title} bill is already assigned to {proposed_display}. Nothing to change."
    else:
        current = {
            "vendor_name": doc.effective_vendor_name, "invoice_number": doc.effective_invoice_number,
            "invoice_date": doc.effective_invoice_date, "due_date": doc.effective_due_date,
            "total": doc.effective_total, "currency": doc.effective_currency,
        }[field]
        current_display = None if current is None else str(current)
        if field in ("invoice_date", "due_date"):
            parsed = parse_date(value)
        elif field == "total":
            parsed = parse_decimal(value)
        elif field == "currency":
            parsed = parse_currency(value)
        else:
            parsed = value.strip() or None
        if parsed is None:
            return None, f"I couldn't understand “{value}” as a {label.lower()}. Nothing was changed."
        proposed_raw = parsed.isoformat() if hasattr(parsed, "isoformat") else (
            str(parsed) if isinstance(parsed, Decimal) else parsed)
        proposed_display = str(proposed_raw)
    proposal = {
        "document_id": doc.id, "revision": doc.revision, "field": field, "field_label": label,
        "update_key": UPDATE_KEYS[field], "current_value": current_display, "proposed_value": proposed_display,
        "proposed_raw": proposed_raw,
    }
    text = (f"Proposed change to the {title} bill: {label} from {current_display or 'empty'} to "
            f"{proposed_display}. Nothing changes until you confirm.")
    if doc.status in POSTED_STATUSES:
        text += " QuickBooks will not be changed; the difference will be flagged for follow-up."
    return proposal, text


async def ask(db: AsyncSession, query: str, context_document_id: Optional[str],
              select_document_id: Optional[str]) -> Dict[str, Any]:
    docs = (await db.execute(
        select(Document).options(selectinload(Document.arrivals), selectinload(Document.assigned_property))
        .order_by(Document.created_at.desc()).limit(500)
    )).scalars().all()
    await db.commit()  # read-only; release the transaction before the AI call
    by_id = {d.id: d for d in docs}
    context = by_id.get(context_document_id) if context_document_id else None
    interp, source = await interpret(query, context is not None)
    base: Dict[str, Any] = {"success": True, "interpreted_by": source, "intent": interp.intent,
                            "documents": [], "history": [], "needs_clarification": False}

    target: Optional[Document] = None
    if select_document_id:
        target = by_id.get(select_document_id)
        if not target:
            return {**base, "success": False, "answer": "That bill no longer exists.", "error": "not_found"}
    elif interp.intent in ("followup", "correction") and context:
        target = context
    else:
        raw = [(_score(d, interp.search_terms), d) for d in docs]
        if any(s[0] > 0 for s, _ in raw):
            scored = [(s[0], d) for s, d in raw if s[0] > 0]
        else:
            scored = [(s[1], d) for s, d in raw if s[1] > 0]
        scored.sort(key=lambda x: -x[0])
        if not scored:
            if interp.intent == "correction":
                return {**base, "answer": "Which bill should I change? Ask about the bill first, then give the "
                                          "correction.", "needs_clarification": True}
            terms = ", ".join(interp.search_terms) or query
            return {**base, "answer": f"I couldn't find a bill matching “{terms}”."}
        best = scored[0][0]
        top = [d for s, d in scored if s == best]
        if len(top) > 1:
            return {**base, "needs_clarification": True,
                    "clarification_question": f"{len(top)} bills could match. Which one do you mean?",
                    "answer": f"I found {len(top)} bills that could match. Choose one so I can answer.",
                    "documents": [result_document(d) for d in top[:5]],
                    "pending_query": query}
        target = top[0]

    events = await get_document_history(db, target.id)
    await db.commit()
    history = [describe(e, target) for e in events]
    result = {**base, "selected_document_id": target.id, "documents": [result_document(target)],
              "history": history}

    if interp.intent == "correction" and interp.correction_field and interp.correction_value:
        proposal, text = await propose_correction(db, target, interp.correction_field, interp.correction_value)
        await db.commit()
        return {**result, "answer": text, "proposed_edit": proposal}
    return {**result, "answer": grounded_answer(target, interp.asked_field)}
