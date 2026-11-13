"""Read-only assistant: find a bill from everyday language and explain its history.

OpenAI (or the deterministic local interpreter when OpenAI is unavailable) only
turns the question into validated criteria. Amounts and dates are always read
from the question here, deterministically. Candidate selection, the answer
text, links and status wording are all produced from stored records, so every
answer is grounded in the saved document and its history. Corrections are
returned as *proposals*; nothing is changed until the owner confirms through the
normal versioned update endpoint.
"""

import calendar
import difflib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..models import (
    DOCUMENT_CATEGORIES, _CATEGORY_RULES, AccountMapping, Document, DocumentStatus, Property, VendorMapping,
)
from ..providers.openai_provider import parse_currency, parse_date, parse_decimal
from ..providers.quickbooks_provider import bill_record_url
from ..providers.registry import ai, open_quickbooks
from .documents import (
    POSTED_STATUSES, POSTING_STATUSES, get_document_history, get_pending_question, normalize_vendor_name,
)
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
    "repair": ["repair", "repairs", "maintenance", "plumbing", "handyman"],
    "repairs": ["repair", "repairs", "maintenance", "plumbing", "handyman"],
    "maintenance": ["repair", "repairs", "maintenance"],
    "plumber": ["plumbing", "plumber", "repair"],
    "insurance": ["insurance", "policy", "premium"],
    "tax": ["tax", "taxes", "assessment"],
    "taxes": ["tax", "taxes", "assessment"],
    "lawn": ["lawn", "landscaping", "landscape", "yard"],
    "landscaping": ["lawn", "landscaping", "landscape", "yard"],
    "rent": ["rent", "lease", "rental"],
    "lease": ["lease", "rental", "rent"],
    "hoa": ["hoa", "association", "homeowners"],
    "receipt": ["receipt", "paid"],
    "statement": ["statement"],
}
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9
STOP_WORDS = set("""
a an and are about any bill bills by can could did do does for from had has have how i if in is it its
me my of on or our please show tell that the their them there this to was we what whats what's when where
which who why with you your happened happen status latest last recent invoice please paid pay
month months week weeks year years ago today yesterday dollars dollar usd bucks around roughly approximately
came come got received sent arrived one s
""".split()) | set(MONTHS)

FIELD_LABELS = {
    "property": "Property", "vendor_name": "Vendor", "invoice_number": "Invoice number",
    "invoice_date": "Invoice date", "due_date": "Due date", "total": "Total", "currency": "Currency",
    "document_type": "Document type", "qb_account": "Expense account", "qb_vendor": "QuickBooks vendor",
}
UPDATE_KEYS = {
    "property": "property_id", "vendor_name": "reviewed_vendor_name", "invoice_number": "reviewed_invoice_number",
    "invoice_date": "reviewed_invoice_date", "due_date": "reviewed_due_date", "total": "reviewed_total",
    "currency": "reviewed_currency", "document_type": "reviewed_document_type", "qb_account": "qb_account_id",
    "qb_vendor": "qb_vendor_id",
}


class Interpretation(BaseModel):
    intent: str = "lookup"
    search_terms: List[str] = []
    asked_field: Optional[str] = None
    correction_field: Optional[str] = None
    correction_value: Optional[str] = None


# ---------------------------------------------------------------------------
# Amounts and dates in the question (always parsed locally)
# ---------------------------------------------------------------------------

@dataclass
class Criteria:
    amounts: List[Tuple[Decimal, bool]] = field(default_factory=list)  # (value, cents_given)
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    date_label: Optional[str] = None
    consumed: List[str] = field(default_factory=list)  # text spans used for amounts/dates

    @property
    def has_date(self) -> bool:
        return self.date_from is not None


_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?"
_AMOUNT_PATTERNS = [
    re.compile(r"(?:\$|\bus\$|\busd\s*)\s*" + _NUM, re.IGNORECASE),
    re.compile(r"\b" + _NUM + r"\s*(?:dollars?|usd|bucks)\b", re.IGNORECASE),
    re.compile(r"(?<![\w.\-])(\d+)\.(\d{2})(?![\w.\-])"),  # a bare 142.50
]


def _today() -> date:
    try:
        tz = ZoneInfo(settings.owner_timezone)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date()


def _month_range(year: int, month: int) -> Tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def parse_criteria(query: str, today: Optional[date] = None) -> Criteria:
    today = today or _today()
    c = Criteria()
    q = query.lower()

    for pattern in _AMOUNT_PATTERNS:
        for m in pattern.finditer(query):
            whole, cents = m.group(1), m.group(2)
            try:
                value = Decimal(whole.replace(",", "") + (f".{cents}" if cents else ""))
            except InvalidOperation:
                continue
            if all(value != v for v, _ in c.amounts):
                c.amounts.append((value, bool(cents)))
            c.consumed.append(m.group(0))

    def set_range(start: date, end: date, label: str, span: str) -> None:
        if c.date_from is None:
            c.date_from, c.date_to, c.date_label = start, end, label
        c.consumed.append(span)

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            set_range(d, d, d.isoformat(), m.group(0))
        except ValueError:
            pass
    if re.search(r"\btoday\b", q):
        set_range(today, today, "today", "today")
    if re.search(r"\byesterday\b", q):
        y = today - timedelta(days=1)
        set_range(y, y, "yesterday", "yesterday")
    m = re.search(r"\b(this|last|past)\s+(week|month|year)\b", q)
    if m:
        which, unit = m.group(1), m.group(2)
        if unit == "week":
            start = today - timedelta(days=today.weekday())
            if which == "this":
                set_range(start, today, "this week", m.group(0))
            else:
                set_range(start - timedelta(days=7), start - timedelta(days=1), "last week", m.group(0))
        elif unit == "month":
            if which == "this":
                set_range(today.replace(day=1), today, "this month", m.group(0))
            else:
                prev_end = today.replace(day=1) - timedelta(days=1)
                set_range(*_month_range(prev_end.year, prev_end.month), "last month", m.group(0))
        else:
            if which == "this":
                set_range(date(today.year, 1, 1), today, "this year", m.group(0))
            else:
                set_range(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "last year", m.group(0))
    m = re.search(r"\b(\d+)\s+(day|week)s?\s+ago\b", q)
    if m:
        n = int(m.group(1)) * (7 if m.group(2) == "week" else 1)
        d = today - timedelta(days=n)
        set_range(d - timedelta(days=3), d + timedelta(days=3), m.group(0), m.group(0))
    for m in re.finditer(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b\.?(?:\s+(\d{4}))?", q):
        name, year = m.group(1), m.group(2)
        if name == "may" and not year and not re.search(r"\b(in|from|for|during|of|since)\s+may\b", q):
            continue  # "may" is usually the verb
        month = MONTHS[name]
        if year:
            y = int(year)
        else:
            y = today.year if month <= today.month else today.year - 1  # the most recent such month
        set_range(*_month_range(y, month), f"{calendar.month_name[month]} {y}", m.group(0))
    return c


def _strip_consumed(query: str, criteria: Criteria) -> str:
    for span in criteria.consumed:
        query = re.sub(re.escape(span), " ", query, flags=re.IGNORECASE)
    return query


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------

_FIELD_ALTERNATIVES = (
    r"document\s+type|type\s+of\s+document|document\s+category|category|type|kind|"
    r"expense\s+account|account|quickbooks\s+vendor|qb\s+vendor|"
    r"property|vendor(?:\s+name)?|invoice\s+number|invoice\s+date|due\s+date|amount|total|currency"
)
_CORRECTION = re.compile(
    r"^\s*(?:please\s+)?(?:change|set|update|correct|make)\s+(?:the\s+)?"
    rf"(?P<field>{_FIELD_ALTERNATIVES})"
    r"(?:\s+(?:on|for|of)\s+(?:this|that|the)\s+(?:bill|document|one))?\s+(?:to|as|=)\s+(?P<value>.+?)[.!]?\s*$",
    re.IGNORECASE,
)
# "this is a receipt", "it's actually a lease", "mark this as a bank statement", "file it as other"
_CLASSIFY = re.compile(
    r"^\s*(?:please\s+)?(?:(?:this|it|that)(?:\s+(?:one|document|bill))?\s*(?:is|'s|’s)\s+(?:actually\s+|really\s+)?"
    r"(?:just\s+)?(?:a|an)\s+|(?:mark|file|classify|treat|categori[sz]e|tag)\s+(?:this|it|that)(?:\s+(?:one|document|bill))?"
    r"\s+as\s+(?:(?:a|an)\s+)?)(?P<value>.+?)(?:,?\s+not\s+an?\s+.+?)?[.!]?\s*$",
    re.IGNORECASE,
)
# "put this under Repairs", "book it to Utilities", "code this to Repairs and Maintenance"
_BOOK = re.compile(
    r"^\s*(?:please\s+)?(?:put|book|code|post|move|file)\s+(?:this|it|that)(?:\s+(?:one|bill|expense))?\s+"
    r"(?:under|to|in|into)\s+(?:the\s+)?(?P<value>.+?)(?:\s+(?:expense\s+)?account)?[.!]?\s*$",
    re.IGNORECASE,
)
_FIELD_ALIASES = {
    "property": "property", "vendor": "vendor_name", "vendor name": "vendor_name",
    "invoice number": "invoice_number", "invoice date": "invoice_date", "due date": "due_date",
    "amount": "total", "total": "total", "currency": "currency",
    "document type": "document_type", "type of document": "document_type", "document category": "document_type",
    "category": "document_type", "type": "document_type", "kind": "document_type",
    "expense account": "qb_account", "account": "qb_account",
    "quickbooks vendor": "qb_vendor", "qb vendor": "qb_vendor",
}
_OTHER_WORDS = {"other", "other document", "something else", "not a bill", "document"}


def match_category(value: str) -> Optional[str]:
    """The document category a spoken/typed value names, or None."""
    v = re.sub(r"\s+", " ", value.strip().lower()).strip(" .!")
    v = re.sub(r"^(a|an|the)\s+", "", v)
    if v in _OTHER_WORDS:
        return "other"
    key = v.replace(" ", "_")
    if key in DOCUMENT_CATEGORIES:
        return key
    for cat, label in DOCUMENT_CATEGORIES.items():
        if v == label.lower():
            return cat
    for cat, pattern in _CATEGORY_RULES:
        if re.search(pattern, v):
            return cat
    return None


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


def _correction(field_name: str, value: str) -> Dict[str, Any]:
    return {"intent": "correction", "search_terms": [], "asked_field": None,
            "correction_field": field_name, "correction_value": value.strip()}


def local_interpret(query: str, has_context: bool) -> Dict[str, Any]:
    """Deterministic interpreter (used when OpenAI is not configured or fails)."""
    m = _CORRECTION.match(query)
    if m:
        return _correction(_FIELD_ALIASES[re.sub(r"\s+", " ", m.group("field").lower())], m.group("value"))
    m = _CLASSIFY.match(query)
    if m:
        value = m.group("value")
        # "categorize this as Repairs" names an account, not a document type
        return _correction("document_type" if match_category(value) else "qb_account", value)
    m = _BOOK.match(query)
    if m:
        return _correction("qb_account", m.group("value"))
    text = _strip_consumed(query, parse_criteria(query))
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]*", text.lower())]
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


def _expand(term: str) -> List[str]:
    t = term.lower()
    if t in SYNONYMS:
        return SYNONYMS[t]
    close = difflib.get_close_matches(t, list(SYNONYMS), n=1, cutoff=0.8) if len(t) >= 4 else []
    if close:
        return SYNONYMS[close[0]]  # "electrisity" -> the electricity synonyms
    return [t]


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
    # Always include synonyms (and spelling-tolerant synonyms) so "power" finds an electricity bill.
    amount_digits = {d for v, _ in parse_criteria(query).amounts for d in re.findall(r"\d+", str(v))}
    expanded: List[str] = []
    for t in interp.search_terms:
        if t.lower() in amount_digits:
            continue
        for s in _expand(t):
            if s not in expanded and s not in STOP_WORDS:
                expanded.append(s)
    interp.search_terms = expanded
    return interp, source


# ---------------------------------------------------------------------------
# Retrieval and grounded answers
# ---------------------------------------------------------------------------

def _amount_matches(doc: Document, criteria: Criteria) -> bool:
    total = doc.effective_total
    if total is None:
        return False
    for value, cents_given in criteria.amounts:
        if cents_given and abs(total - value) < Decimal("0.005"):
            return True
        if not cents_given and abs(total - value) < 1:  # "$142" finds 142.50
            return True
    return False


def _local_date(dt: Optional[datetime]) -> Optional[date]:
    if not dt:
        return None
    try:
        tz = ZoneInfo(settings.owner_timezone)
    except Exception:
        tz = timezone.utc
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).date()


def _date_matches(doc: Document, criteria: Criteria) -> bool:
    days = [doc.effective_invoice_date, doc.effective_due_date] + [_local_date(a.arrived_at) for a in doc.arrivals]
    return any(d and criteria.date_from <= d <= criteria.date_to for d in days)


def _fuzzy_hit(term: str, text: str) -> bool:
    if len(term) < 4:
        return False
    return any(difflib.SequenceMatcher(None, term, w).ratio() >= 0.8
               for w in re.findall(r"[a-z0-9]+", text.lower()) if abs(len(w) - len(term)) <= 3)


def _score(doc: Document, terms: List[str], criteria: Optional[Criteria] = None) -> Tuple[int, int]:
    """(score on bill facts, score on filenames/subjects). Facts decide; names only break a zero."""
    facts = [
        (doc.effective_vendor_name, 3), (doc.extracted_document_type, 2), (doc.effective_invoice_number, 3),
        (doc.extracted_service_address, 1), (doc.property_name, 2),
        (DOCUMENT_CATEGORIES.get(doc.document_category), 1),
    ]
    names = [(a.original_filename, 1) for a in doc.arrivals] + [(a.email_subject, 1) for a in doc.arrivals]

    def score(fields):
        total = 0
        for term in terms:
            pattern = re.compile(rf"\b{re.escape(term)}", re.IGNORECASE)
            exact = sum(weight for text, weight in fields if text and pattern.search(text))
            total += exact or sum(weight for text, weight in fields if text and _fuzzy_hit(term, text))
        return total

    fact_score = score(facts)
    if criteria:
        if criteria.amounts and _amount_matches(doc, criteria):
            fact_score += 4
        if criteria.has_date and _date_matches(doc, criteria):
            fact_score += 2
    return fact_score, score(names)


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


def _status_sentence(doc: Document, original: Optional[Document] = None) -> str:
    ref = doc.qb_bill_doc_number or doc.qb_bill_id
    s = doc.status
    if s == DocumentStatus.FILED:
        label = DOCUMENT_CATEGORIES.get(doc.document_category, "document").lower()
        return f"It is filed as a {label}. It is not a bill, so nothing is sent to QuickBooks."
    if s == DocumentStatus.POSSIBLE_COPY:
        what = f"the stored {_doc_title(original)} bill" if original else "a bill that is already stored"
        return (f"It looks like another copy of {what} (same invoice, different file) and is waiting for you to "
                "confirm. Nothing has been posted.")
    if s == DocumentStatus.LINKED_COPY:
        if not original:
            return "It is a confirmed copy of another stored bill; that record carries the accounting."
        return (f"It is a confirmed copy of the {_doc_title(original)} bill, which carries the accounting: "
                + _status_sentence(original))
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
        "document_category": doc.document_category,
        "links": {
            "record": f"/documents/{doc.id}", "original": f"/api/documents/{doc.id}/file",
            "quickbooks": bill_record_url(doc.qb_bill_id) if doc.qb_bill_id else None,
        },
    }


def grounded_answer(doc: Document, asked: Optional[str], original: Optional[Document] = None) -> str:
    title = _doc_title(doc)
    noun = "bill" if doc.is_bill else DOCUMENT_CATEGORIES.get(doc.document_category, "document").lower()
    lead = ""
    if asked == "property":
        lead = (f"The {title} {noun} is for {doc.property_name}. " if doc.property_name
                else f"No property has been assigned to the {title} {noun} yet. ")
    elif asked == "amount":
        lead = f"The {title} {noun} is for {_money(doc)}. "
    elif asked == "due_date":
        lead = (f"The {title} {noun} is due {doc.effective_due_date.isoformat()}. " if doc.effective_due_date
                else f"No due date is recorded for the {title} {noun}. ")
    elif asked == "vendor":
        lead = f"The vendor is {doc.effective_vendor_name or 'not known yet'}. "
    body = (f"{'' if lead else f'The {title} {noun}, '}{_money(doc)}"
            f"{', for ' + doc.property_name if doc.property_name else ', with no property assigned'}. ")
    if lead:
        body = ""
    return (lead + body + _status_sentence(doc, original) + " " + _arrivals_sentence(doc)).strip()


async def _property_matches(db: AsyncSession, value: str) -> List[Property]:
    needle = re.sub(r"\s+", " ", value.strip().lower())
    props = (await db.execute(select(Property).order_by(Property.name))).scalars().all()
    exact = [p for p in props if p.name.lower() == needle]
    if exact:
        return exact
    return [p for p in props if needle in p.name.lower() or (p.address and needle in p.address.lower())]


async def _qb_choices(db: AsyncSession, kind: str) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """(id, name) pairs of QuickBooks vendors or expense accounts.

    Read live from the connected company; when it can't be reached, fall back to the
    names saved in the mappings so a correction can still be proposed.
    """
    client, error = await open_quickbooks(db)
    await db.commit()
    rows: List[Tuple[str, str]] = []
    if client and not error:
        if kind == "vendor":
            items, error = await client.get_vendors()
            rows = [(str(v["Id"]), v.get("DisplayName", "")) for v in items or []]
        else:
            items, error = await client.get_accounts()
            rows = [(str(a["Id"]), a.get("FullyQualifiedName") or a.get("Name", "")) for a in items or []]
    if not rows:
        if kind == "vendor":
            maps = (await db.execute(select(VendorMapping))).scalars().all()
            rows = list({(m.qb_vendor_id, m.qb_vendor_name) for m in maps})
        else:
            maps = (await db.execute(select(AccountMapping))).scalars().all()
            rows = list({(m.qb_account_id, m.qb_account_name) for m in maps})
        await db.commit()
    return sorted(rows, key=lambda r: r[1].lower()), (error if not rows else None)


def _pick(rows: List[Tuple[str, str]], value: str) -> List[Tuple[str, str]]:
    needle = re.sub(r"\s+", " ", value.strip().lower()).strip(" .!")
    needle = re.sub(r"^(the|a|an)\s+", "", needle)
    needle = re.sub(r"\s+(expense\s+)?account$", "", needle)
    exact = [r for r in rows if r[1].lower() == needle or r[1].lower().split(":")[-1] == needle]
    if exact:
        return exact
    contains = [r for r in rows if needle and needle in r[1].lower()]
    if contains:
        return contains
    close = difflib.get_close_matches(needle, [r[1].lower() for r in rows], n=2, cutoff=0.75)
    return [r for r in rows if r[1].lower() in close]


async def propose_correction(db: AsyncSession, doc: Document, field_name: str, value: str
                             ) -> Tuple[Optional[Dict[str, Any]], str]:
    label = FIELD_LABELS[field_name]
    title = _doc_title(doc)
    if doc.status in POSTING_STATUSES:
        return None, f"The {title} bill is being recorded in QuickBooks right now, so it can't be edited yet."
    if doc.status in (DocumentStatus.RECEIVED, DocumentStatus.READING):
        return None, f"The {title} bill is still being read. Try the correction again in a moment."
    if doc.status == DocumentStatus.LINKED_COPY:
        return None, f"This file is a confirmed copy of another bill. Make the change on the original. Nothing was changed."

    current_display: Optional[str]
    proposed_raw: Any
    proposed_display: str
    extra: Dict[str, Any] = {}
    extra_text = ""
    if field_name == "property":
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
    elif field_name == "document_type":
        category = match_category(value)
        if not category:
            kinds = ", ".join(DOCUMENT_CATEGORIES[c].lower() for c in DOCUMENT_CATEGORIES)
            return None, f"I couldn't tell what kind of document “{value}” is. Choose one of: {kinds}. Nothing was changed."
        current_display = DOCUMENT_CATEGORIES[doc.document_category]
        proposed_raw, proposed_display = category, DOCUMENT_CATEGORIES[category]
        if doc.document_category == category:
            return None, f"The {title} document is already classified as {proposed_display.lower()}. Nothing to change."
        if category == "bill":
            extra_text = " It will go back to the bill review queue."
        else:
            extra_text = " It will be filed with no accounting work."
    elif field_name in ("qb_account", "qb_vendor"):
        kind = "vendor" if field_name == "qb_vendor" else "account"
        rows, error = await _qb_choices(db, kind)
        if not rows:
            return None, (f"I couldn't load the QuickBooks {kind}s ({error or 'none found'}). "
                          "Choose it on the bill page instead. Nothing was changed.")
        matches = _pick(rows, value)
        if not matches:
            return None, f"No QuickBooks {kind} matches “{value}”. Nothing was changed."
        if len(matches) > 1:
            return None, (f"“{value}” matches several QuickBooks {kind}s ({', '.join(n for _, n in matches[:5])}). "
                          "Say the full name. Nothing was changed.")
        current_id = doc.qb_vendor_id if kind == "vendor" else doc.qb_account_id
        current_display = next((n for i, n in rows if i == current_id), current_id)
        proposed_raw, proposed_display = matches[0]
        if current_id == proposed_raw:
            return None, f"The {title} bill already uses {proposed_display}. Nothing to change."
    else:
        current = {
            "vendor_name": doc.effective_vendor_name, "invoice_number": doc.effective_invoice_number,
            "invoice_date": doc.effective_invoice_date, "due_date": doc.effective_due_date,
            "total": doc.effective_total, "currency": doc.effective_currency,
        }[field_name]
        current_display = None if current is None else str(current)
        if field_name in ("invoice_date", "due_date"):
            parsed = parse_date(value)
        elif field_name == "total":
            parsed = parse_decimal(value)
        elif field_name == "currency":
            parsed = parse_currency(value)
        else:
            parsed = value.strip() or None
        if parsed is None:
            return None, f"I couldn't understand “{value}” as a {label.lower()}. Nothing was changed."
        proposed_raw = parsed.isoformat() if hasattr(parsed, "isoformat") else (
            str(parsed) if isinstance(parsed, Decimal) else parsed)
        proposed_display = str(proposed_raw)
        if field_name == "vendor_name":
            # Keep the QuickBooks vendor in step when exactly one has this name.
            rows, _ = await _qb_choices(db, "vendor")
            same = [r for r in rows if normalize_vendor_name(r[1]) == normalize_vendor_name(proposed_display)]
            if len(same) == 1 and same[0][0] != doc.qb_vendor_id:
                extra["qb_vendor_id"] = same[0][0]
                extra_text = f" The QuickBooks vendor will also be set to {same[0][1]}."
    proposal = {
        "document_id": doc.id, "revision": doc.revision, "field": field_name, "field_label": label,
        "update_key": UPDATE_KEYS[field_name], "current_value": current_display, "proposed_value": proposed_display,
        "proposed_raw": proposed_raw, "updates": {UPDATE_KEYS[field_name]: proposed_raw, **extra},
    }
    noun = "bill" if doc.is_bill else "document"
    text = (f"Proposed change to the {title} {noun}: {label} from {current_display or 'empty'} to "
            f"{proposed_display}.{extra_text} Nothing changes until you confirm.")
    if doc.status in POSTED_STATUSES:
        text += " QuickBooks will not be changed; the difference will be flagged for follow-up."
    return proposal, text


def _describe_criteria(criteria: Criteria, terms: List[str], query: str) -> str:
    parts = []
    if terms:
        parts.append(", ".join(terms))
    if criteria.amounts:
        parts.append(" or ".join(f"${v}" for v, _ in criteria.amounts))
    if criteria.date_label:
        parts.append(f"dated {criteria.date_label}")
    return "; ".join(parts) or query


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
    criteria = parse_criteria(query)
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
        # A confirmed copy is answered through its original, so it never competes with it.
        candidates = [d for d in docs if d.status != DocumentStatus.LINKED_COPY]
        raw = [(_score(d, interp.search_terms, criteria), d) for d in candidates]
        if any(s[0] > 0 for s, _ in raw):
            scored = [(s[0], d) for s, d in raw if s[0] > 0]
        else:
            scored = [(s[1], d) for s, d in raw if s[1] > 0]
        scored.sort(key=lambda x: -x[0])
        if not scored:
            if interp.intent == "correction":
                return {**base, "answer": "Which bill should I change? Ask about the bill first, then give the "
                                          "correction.", "needs_clarification": True}
            return {**base, "answer": f"I couldn't find a bill matching “"
                                      f"{_describe_criteria(criteria, interp.search_terms, query)}”."}
        best = scored[0][0]
        top = [d for s, d in scored if s == best]
        if len(top) > 1:
            return {**base, "needs_clarification": True,
                    "clarification_question": f"{len(top)} bills could match. Which one do you mean?",
                    "answer": f"I found {len(top)} bills that could match. Choose one so I can answer.",
                    "documents": [result_document(d) for d in top[:5]],
                    "pending_query": query}
        target = top[0]

    original = by_id.get(target.copy_of_document_id) if target.copy_of_document_id else None
    events = await get_document_history(db, target.id)
    await db.commit()
    history = [describe(e, target) for e in events]
    result = {**base, "selected_document_id": target.id, "documents": [result_document(target)],
              "history": history}

    if interp.intent == "correction" and interp.correction_field and interp.correction_value:
        proposal, text = await propose_correction(db, target, interp.correction_field, interp.correction_value)
        await db.commit()
        return {**result, "answer": text, "proposed_edit": proposal}
    return {**result, "answer": grounded_answer(target, interp.asked_field, original)}
