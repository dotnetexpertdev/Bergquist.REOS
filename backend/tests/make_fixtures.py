"""Generate the demo fixture bills A, B and C as small, valid PDFs.

The visible text is what a real OpenAI extraction reads during the live
rehearsal. The ``%MA-FIXTURE`` comment line carries the expected values for the
deterministic test adapter only (PDF viewers ignore comments).

    python backend\\tests\\make_fixtures.py C:\\path\\to\\folder
"""

import json
import sys
from pathlib import Path
from typing import Dict, List

FIXTURES: Dict[str, Dict] = {
    # A: clearly addressed electricity bill
    "A": {
        "filename": "metro-power-statement.pdf",
        "lines": ["Metro Power & Light", "Electricity Service Statement", "Invoice number: MPL-2026-0415",
                  "Invoice date: 2026-10-15   Due date: 2026-11-05",
                  "Service address: 123 Main St, Springfield", "Amount due: USD 142.50"],
        "values": {"document_type": "utility bill", "vendor_name": "Metro Power & Light",
                   "invoice_number": "MPL-2026-0415", "invoice_date": "2026-10-15", "due_date": "2026-11-05",
                   "total_amount": "142.50", "currency": "USD", "service_address": "123 Main St, Springfield",
                   "confidence": 0.97, "source_text": "Amount due: USD 142.50", "source_page": 1,
                   "uncertain_fields": []},
    },
    # B: property unclear (no service address)
    "B": {
        "filename": "city-water-notice.pdf",
        "lines": ["City Water Dept", "Water and sewer charges", "Invoice number: CW-88812",
                  "Invoice date: 2026-10-20", "Account holder: Owner", "Amount due: USD 61.20"],
        "values": {"document_type": "utility bill", "vendor_name": "City Water Dept", "invoice_number": "CW-88812",
                   "invoice_date": "2026-10-20", "due_date": None, "total_amount": "61.20", "currency": "USD",
                   "service_address": None, "confidence": 0.9, "source_text": "Amount due: USD 61.20",
                   "source_page": 1, "uncertain_fields": ["service_address"]},
    },
    # C: entered manually in QuickBooks before the run
    "C": {
        "filename": "northside-electric-supply-inv.pdf",
        "lines": ["Northside Electric Supply", "Invoice", "Invoice number: NES-5531",
                  "Invoice date: 2026-09-30", "Ship to: 123 Main St, Springfield", "Total: USD 389.00"],
        "values": {"document_type": "invoice", "vendor_name": "Northside Electric Supply",
                   "invoice_number": "NES-5531", "invoice_date": "2026-09-30", "due_date": None,
                   "total_amount": "389.00", "currency": "USD", "service_address": "123 Main St, Springfield",
                   "confidence": 0.95, "source_text": "Total: USD 389.00", "source_page": 1,
                   "uncertain_fields": []},
    },
}


def make_pdf(lines: List[str], values: Dict) -> bytes:
    text_ops = ["BT", "/F1 14 Tf", "72 720 Td", "18 TL"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        text_ops.append(f"({safe}) Tj T*")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    out += b"%MA-FIXTURE " + json.dumps(values, separators=(",", ":")).encode() + b"\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def fixture_bytes(key: str) -> bytes:
    f = FIXTURES[key]
    return make_pdf(f["lines"], f["values"])


def write_all(folder: Path) -> Dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key, f in FIXTURES.items():
        path = folder / f"bill_{key}_{f['filename']}"
        path.write_bytes(fixture_bytes(key))
        paths[key] = path
    return paths


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "demo-fixtures"
    for key, path in write_all(target).items():
        print(f"{key}: {path}")
