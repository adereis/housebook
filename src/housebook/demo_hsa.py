"""HSA Shoebox content for the Ledger Family demo.

Writes what a real import leaves behind, so every part of the /hsa
page and `housebook-hsa` has something to show:

- a one-page PDF per document under ``hsa/YYYY/`` (receipts, insurance
  EOBs, a 5498-SA), named by the import SOP's convention;
- an envelope sidecar beside each PDF, registered in
  ``processed_files`` so a later ``housebook-hsa ingest`` sees them as
  unchanged instead of attaching every document twice;
- the ``hsa_expenses`` / ``hsa_documents`` rows the ingestor and the
  reconcile SOP would have produced, plus the card charges that paid
  for them, the audit log, and two reimbursement batches.

Rows are written directly (as the rest of the seeder does) rather than
through ``HsaIngestor``: the ingestor resolves paths against the
workspace captured when settings are first imported, which is not
necessarily the demo workspace.

Evidence levels are derived from the sources on file, following the
table in ``hsa/AGENTS.md``, so the demo cannot contradict it.
"""

import datetime
import json
import os

from housebook.core.sidecar import Sidecar, build_source_file

INSURER = "Shield Health"
CUSTODIAN = "Vault HSA"
CLASSIFIED_AT = "2026-04-03T12:00:00Z"
# Card charges after this date fall in the seeder's audit window and
# are still UNVERIFIED (see demo_seed.seed_db).
AUDIT_WINDOW_START = datetime.date(2026, 3, 4)

# Each visit is one medical service. "docs" lists the documents on
# file (EOB from the insurer, REC from the provider); "paid" is how the
# family paid: "card" (a charge on the Chase statement), "check" (no
# statement line), or None (not paid yet). Visits sharing a
# "charge_group" were paid by one combined card charge.
HSA_VISITS = [
    {
        "key": "crown", "date": "2023-03-14", "provider": "Maple Dental",
        "patient": "Sterling", "service": "Porcelain crown",
        "category": "dental", "billed": 1480.00, "plan_paid": 755.00,
        "owed": 725.00, "docs": ["EOB", "REC"], "paid": "card",
        "status": "REIMBURSED",
    },
    {
        "key": "glasses", "date": "2023-09-08",
        "provider": "Clearview Eye Care", "patient": "Ally",
        "service": "Prescription glasses", "category": "vision",
        "billed": 310.00, "plan_paid": 0.0, "owed": 310.00,
        "docs": ["REC"], "paid": "card", "status": "REIMBURSED",
    },
    {
        "key": "whitening", "date": "2023-12-11", "provider": "Maple Dental",
        "patient": "Penny", "service": "Teeth whitening",
        "category": "dental", "billed": 450.00, "plan_paid": 0.0,
        "owed": 450.00, "docs": ["REC"], "paid": "card",
        "exclusion": "Cosmetic procedure, not a qualified medical"
                     " expense (IRS Pub. 502)",
    },
    {
        "key": "sick-visit", "date": "2024-02-05",
        "provider": "Riverside Pediatrics", "patient": "Buck",
        "service": "Sick visit", "category": "medical", "billed": 220.00,
        "plan_paid": 175.00, "owed": 45.00, "docs": ["EOB", "REC"],
        "paid": "card",
    },
    {
        "key": "fillings", "date": "2024-07-30", "provider": "Maple Dental",
        "patient": "Ally", "service": "Two fillings", "category": "dental",
        "billed": 480.00, "plan_paid": 288.00, "owed": 192.00,
        "docs": ["EOB", "REC"], "paid": "card",
    },
    {
        "key": "pt-1", "date": "2024-10-08",
        "provider": "Harbor Physical Therapy", "patient": "Penny",
        "service": "Physical therapy session", "category": "medical",
        "billed": 180.00, "plan_paid": 115.00, "owed": 65.00,
        "docs": ["EOB"], "paid": "card", "charge_group": "pt-october",
    },
    {
        "key": "pt-2", "date": "2024-10-22",
        "provider": "Harbor Physical Therapy", "patient": "Penny",
        "service": "Physical therapy session", "category": "medical",
        "billed": 180.00, "plan_paid": 115.00, "owed": 65.00,
        "docs": ["EOB"], "paid": "card", "charge_group": "pt-october",
    },
    {
        "key": "counseling", "date": "2025-01-15",
        "provider": "Brightpath Counseling", "patient": "Ally",
        "service": "Counseling session", "category": "mental_health",
        "billed": 120.00, "plan_paid": 0.0, "owed": 120.00,
        "docs": ["REC"], "paid": "check",
        "note": "Paid by check; add the cleared check image to"
                " corroborate.",
    },
    {
        "key": "mri", "date": "2025-04-02",
        "provider": "Riverside Medical Center", "patient": "Penny",
        "service": "MRI, left knee", "category": "medical",
        "billed": 2800.00, "plan_paid": 2100.00, "owed": 700.00,
        "docs": ["EOB", "REC"], "paid": "card", "status": "PENDING",
    },
    {
        "key": "amoxicillin", "date": "2025-08-19",
        "provider": "CVS Pharmacy", "patient": "Buck",
        "service": "Amoxicillin prescription", "category": "pharmacy",
        "billed": 12.40, "plan_paid": 0.0, "owed": 12.40, "docs": ["REC"],
        "paid": "card",
    },
    {
        "key": "bloodwork", "date": "2025-11-06",
        "provider": "Northside Lab", "patient": "Sterling",
        "service": "Annual bloodwork", "category": "lab", "billed": 240.00,
        "plan_paid": 155.00, "owed": 85.00, "docs": ["EOB"], "paid": None,
        "note": "Lab bill not paid yet.",
    },
    {
        "key": "eye-exam", "date": "2026-01-27",
        "provider": "Clearview Eye Care", "patient": "Sterling",
        "service": "Eye exam and contact lenses", "category": "vision",
        "billed": 285.00, "plan_paid": 0.0, "owed": 285.00,
        "docs": ["REC"], "paid": "card",
    },
    {
        "key": "ear-infection", "date": "2026-03-20",
        "provider": "Riverside Pediatrics", "patient": "Buck",
        "service": "Ear infection visit", "category": "medical",
        "billed": 190.00, "plan_paid": 150.00, "owed": 40.00, "docs": [],
        "paid": "card",
    },
]

# (date, method, status, visit keys)
REIMBURSEMENTS = [
    ("2024-01-20", "Vault HSA online transfer", "COMPLETED",
     ["crown", "glasses"]),
    ("2026-04-15", "Vault HSA online transfer", "PLANNED", ["mri"]),
]

PATIENTS = [
    ("Sterling", "self", "1982-05-14"),
    ("Penny", "spouse", "1984-09-02"),
    ("Ally", "child", "2010-03-21"),
    ("Buck", "child", "2023-01-09"),
]


def write_hsa_config(workspace):
    """Write the demo's config/hsa/{patients,providers}.json."""
    config_dir = os.path.join(workspace, "config", "hsa")
    os.makedirs(config_dir, exist_ok=True)
    patients = {"patients": [
        {
            "id": name.lower(),
            "name": f"{name} Ledger",
            "relationship": relationship,
            "dob": dob,
            "hsa_eligible_since": "2021-01-01",
        }
        for name, relationship, dob in PATIENTS
    ]}
    providers = {"providers": [
        {
            "canonical_name": name,
            "category": category,
            "aliases": [name.upper(), name.replace(" ", "-")],
        }
        for name, category in sorted({
            (v["provider"], v["category"]) for v in HSA_VISITS
        })
    ]}
    for filename, content in (
        ("patients.json", patients), ("providers.json", providers),
    ):
        with open(os.path.join(config_dir, filename), "w") as f:
            json.dump(content, f, indent=4)


def evidence_level(visit, shared_charge):
    """The level the reconcile SOP would assign, from sources on file."""
    sources = len(visit["docs"]) + (1 if visit["paid"] == "card" else 0)
    if shared_charge:
        return "weak"
    if sources >= 3:
        return "strong"
    if sources == 2:
        return "ready"
    return "stub"


def _pdf_text(text):
    return (
        text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    )


def render_pdf(title, lines):
    """A minimal one-page PDF: a bold title over lines of text.

    Hand-built so the demo needs no PDF library; the output is
    byte-for-byte deterministic, so recorded hashes stay stable.
    """
    ops = [f"BT /F2 18 Tf 72 720 Td ({_pdf_text(title)}) Tj ET"]
    y = 684
    for line in lines:
        ops.append(f"BT /F1 11 Tf 72 {y} Td ({_pdf_text(line)}) Tj ET")
        y -= 18
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >>"
        b" /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _money(amount):
    return f"${amount:,.2f}"


def _document(visit, doc_type, claim_id):
    """(entity, tags, data block, PDF title, PDF lines) for one doc."""
    provider = visit["provider"]
    patient = f"{visit['patient']} Ledger"
    common = [
        f"Patient: {patient}",
        f"Date of service: {visit['date']}",
        f"Service: {visit['service']}",
    ]
    if doc_type == "EOB":
        data = {
            "date": visit["date"],
            "entity": INSURER.replace(" ", "-"),
            "doc_type": "EOB",
            "patient": visit["patient"],
            "amount": visit["owed"],
            "tags": [provider.replace(" ", "-")],
            "claim_id": claim_id,
            "financials": {
                "billed": visit["billed"],
                "plan_paid": visit["plan_paid"],
                "patient_responsibility": visit["owed"],
            },
        }
        lines = [
            "THIS IS NOT A BILL", "", f"Provider: {provider}", *common,
            f"Claim #: {claim_id}", "",
            f"Amount billed: {_money(visit['billed'])}",
            f"What your plan paid: {_money(visit['plan_paid'])}",
            f"What I owe: {_money(visit['owed'])}",
        ]
        return (INSURER, data["tags"], data,
                f"{INSURER} - Explanation of Benefits", lines)
    data = {
        "date": visit["date"],
        "entity": provider.replace(" ", "-"),
        "doc_type": "REC",
        "patient": visit["patient"],
        "amount": visit["owed"],
        "tags": [],
    }
    method = {"card": "Visa ending 0000", "check": "Check"}[visit["paid"]]
    lines = [
        *common, "",
        f"Amount paid: {_money(visit['owed'])}",
        f"Payment method: {method}",
        "", "Thank you for your payment.",
    ]
    return provider, [], data, f"{provider} - Payment Receipt", lines


def _write_document(workspace, conn, date, entity, doc_type, patient,
                    amount, tags, data, title, lines):
    """Write PDF + sidecar; register the sidecar; return provenance."""
    parts = [date, entity.replace(" ", "-"), doc_type, patient,
             f"{amount:.2f}", *tags]
    basename = "__".join(parts)
    year_dir = os.path.join(workspace, "hsa", date[:4])
    os.makedirs(year_dir, exist_ok=True)
    pdf_path = os.path.join(year_dir, basename + ".pdf")
    json_path = os.path.join(year_dir, basename + ".json")
    body = [*lines, "",
            "Fictitious document - Housebook Ledger Family demo"]
    with open(pdf_path, "wb") as f:
        f.write(render_pdf(title, body))
    source_file = build_source_file(pdf_path, workspace_root=workspace)
    sidecar = Sidecar(
        source="hsa", source_file=source_file, data=data,
        classified_at=CLASSIFIED_AT,
    )
    with open(json_path, "w") as f:
        json.dump(sidecar.to_dict(), f, indent=2)
        f.write("\n")
    sidecar_rel = os.path.relpath(json_path, workspace).replace(os.sep, "/")
    conn.execute(
        "INSERT INTO processed_files (file_path, file_hash) VALUES (?, ?)",
        (sidecar_rel, build_source_file(json_path, workspace).sha256),
    )
    return source_file, sidecar_rel, basename + ".pdf"


def _card_charge(conn, date, provider, amount):
    """The statement line that paid for a visit; returns its id."""
    recent = datetime.date.fromisoformat(date) > AUDIT_WINDOW_START
    cur = conn.execute(
        "INSERT INTO transactions"
        " (date, description, amount, category, source, status,"
        " original_file, needs_review)"
        " VALUES (?, ?, ?, 'Health', 'Chase Sapphire', ?,"
        " 'chase_statement.pdf', ?)",
        (date, provider.upper(), round(amount, 2),
         "UNVERIFIED" if recent else "AGENT_VERIFIED", 1 if recent else 0),
    )
    return cur.lastrowid


def _log(conn, expense_id, field, old, new, changed_by, reason):
    conn.execute(
        "INSERT INTO hsa_audit_log (table_name, record_id, field_name,"
        " old_value, new_value, changed_by, reason)"
        " VALUES ('hsa_expenses', ?, ?, ?, ?, ?, ?)",
        (expense_id, field, old, new, changed_by, reason),
    )


def seed_hsa(conn, workspace):
    """Populate the HSA Shoebox tables and hsa/ files for the demo."""
    groups = {}
    for visit in HSA_VISITS:
        if "charge_group" in visit:
            groups.setdefault(visit["charge_group"], []).append(visit)

    charge_ids = {}
    expense_ids = {}
    for number, visit in enumerate(HSA_VISITS, start=1):
        group = groups.get(visit.get("charge_group"), [visit])
        shared = len(group) > 1
        tx_id = None
        if visit["paid"] == "card":
            group_key = visit.get("charge_group", visit["key"])
            if group_key not in charge_ids:
                charge_ids[group_key] = _card_charge(
                    conn, group[-1]["date"], visit["provider"],
                    sum(v["owed"] for v in group),
                )
            tx_id = charge_ids[group_key]

        level = evidence_level(visit, shared)
        excluded = visit.get("exclusion")
        docs = visit["docs"]
        source = ("eob" if docs[0] == "EOB" else "receipt") if docs else (
            "cc_stub")
        cur = conn.execute(
            "INSERT INTO hsa_expenses"
            " (service_date, provider, patient, description, amount_billed,"
            " insurance_paid, patient_responsibility, category,"
            " payment_method, payment_date, transaction_id, source, status,"
            " needs_review, evidence_level, notes, exclusion_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                visit["date"], visit["provider"], visit["patient"].lower(),
                f"{visit['provider']} ({visit['service']})",
                visit["billed"], visit["plan_paid"], visit["owed"],
                visit["category"], visit["paid"],
                visit["date"] if visit["paid"] else None, tx_id, source,
                visit.get("status", "UNREIMBURSED"),
                0 if excluded or level in ("ready", "strong") else 1,
                level, visit.get("note"), excluded,
            ),
        )
        expense_id = cur.lastrowid
        expense_ids[visit["key"]] = expense_id

        claim_id = f"DEMO-{visit['date'][:4]}-{number:04d}"
        for doc_type in docs:
            entity, tags, data, title, lines = _document(
                visit, doc_type, claim_id)
            source_file, sidecar_rel, filename = _write_document(
                workspace, conn, visit["date"], entity, doc_type,
                visit["patient"], visit["owed"], tags, data, title, lines,
            )
            conn.execute(
                "INSERT INTO hsa_documents (expense_id, document_type,"
                " file_path, file_hash, original_filename, raw_data,"
                " sidecar_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (expense_id, "eob" if doc_type == "EOB" else "receipt",
                 source_file.path, source_file.sha256, filename,
                 json.dumps(data), sidecar_rel),
            )

        if level != "stub":
            sources = [*docs] + (["card charge"] if tx_id else [])
            _log(conn, expense_id, "evidence_level", "stub", level, "agent",
                 "Corroborated by " + " + ".join(sources)
                 + ("; one card charge covers several visits"
                    if shared else ""))
        if excluded:
            _log(conn, expense_id, "exclusion_reason", None, excluded,
                 "agent", "Not eligible for HSA reimbursement")

    for date, method, status, keys in REIMBURSEMENTS:
        total = sum(v["owed"] for v in HSA_VISITS if v["key"] in keys)
        cur = conn.execute(
            "INSERT INTO hsa_reimbursements"
            " (reimbursement_date, total_amount, method, status)"
            " VALUES (?, ?, ?, ?)",
            (date, total, method, status),
        )
        for key in keys:
            conn.execute(
                "INSERT INTO hsa_reimbursement_items"
                " (reimbursement_id, expense_id) VALUES (?, ?)",
                (cur.lastrowid, expense_ids[key]),
            )
            new = "REIMBURSED" if status == "COMPLETED" else "PENDING"
            _log(conn, expense_ids[key], "status", "UNREIMBURSED", new,
                 "user", f"{method} on {date}")

    # Account-level document: the custodian's annual contribution form.
    data = {"date": "2024-12-31", "entity": CUSTODIAN.replace(" ", "-"),
            "doc_type": "TAX", "patient": "Sterling", "amount": 0.0,
            "tags": ["5498-SA"]}
    source_file, sidecar_rel, filename = _write_document(
        workspace, conn, "2024-12-31", CUSTODIAN, "TAX", "Sterling", 0.0,
        ["5498-SA"], data, f"{CUSTODIAN} - Form 5498-SA (2024)",
        ["Account holder: Sterling Ledger",
         "Total contributions in 2024: $8,300.00",
         "Fair market value on Dec 31: $21,480.00"],
    )
    conn.execute(
        "INSERT INTO hsa_documents (expense_id, document_type, file_path,"
        " file_hash, original_filename, raw_data, sidecar_path)"
        " VALUES (NULL, 'tax', ?, ?, ?, ?, ?)",
        (source_file.path, source_file.sha256, filename, json.dumps(data),
         sidecar_rel),
    )
