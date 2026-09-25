# Tax Document Import SOP

## Purpose

This SOP guides an AI agent through importing raw tax documents
(W2, 1099, 1098, etc.) from any location. This is **Phase 1** —
you do NOT run `housebook-tax ingest` from this SOP.

## Workflow

```
Agent receives file path(s) from user
        |
        v
[Phase 1: import]     Agent follows THIS SOP
                      -> determines form type, year, issuer, recipient
                      -> extracts key amounts via pdftotext/OCR
                      -> renames file to canonical convention
                      -> writes envelope-wrapped JSON sidecar
                      -> moves to $WORKSPACE/tax/YYYY/
                      -> STOPS — presents summary, waits for user
        |
        v
[Phase 2: ingest]     housebook-tax validate
                      housebook-tax ingest --dry-run
                      housebook-tax ingest
```

## Step 1: Determine document type

Use `pdftotext -layout <file> -` to read the document. Match
against these form types:

| Code | Form | Key identifier in text |
|------|------|----------------------|
| `W2` | W-2 Wage and Tax Statement | "Wage and Tax Statement" or "W-2" |
| `1099` | 1099-DIV/INT/B consolidated | "1099" + dividends/interest/gains |
| `1099-R` | 1099-R Retirement | "Distributions From Pensions" |
| `1099-HC` | MA Health Coverage | "1099-HC" |
| `1095-C` | Employer Health Coverage | "1095-C" or "Affordable Care" |
| `1098` | Mortgage Interest | "1098" + "Mortgage Interest" |
| `BR-TAX-REPORT` | Brazilian Tax Report (XLSX) | "Summary" sheet |
| `BR-FTC` | Brazilian Foreign Tax Credit (XLSX) | computed from Summary sheet |

For **XLSX files**: use the `core/xlsx.py` helper to read sheets.
The Brazilian Tax Report XLSX produces **two** DB entries from one
file — use the `documents[]` array in the sidecar.

## Step 2: Extract key fields

| Field | Source |
|-------|--------|
| `tax_year` | Filename or document header (4-digit year) |
| `issuer` | Employer name (W2), payer name (1099), bank (1098) |
| `recipient` | Employee/taxpayer first name |
| `amount` | Primary amount: wages (W2), total income (1099), mortgage interest (1098) |
| `form_data` | Per-form extracted fields (see below) |

### Per-form `form_data` fields

**W2**: `federal_tax_withheld`, `state_tax_withheld`, `social_security_wages`, `medicare_wages`
**1099**: `dividends`, `qualified_dividends`, `interest`, `short_term_gain_loss`, `long_term_gain_loss`, `total_gain_loss`
**1099-R**: `taxable_amount`, `distribution_code`, `is_rollover`
**1098**: `mortgage_interest`, `outstanding_principal`, `real_estate_taxes`
**1099-HC / 1095-C**: no numeric fields (compliance docs)
**BR-TAX-REPORT**: `total_income`, `interest_income`, `pfic_*`, `fbar_*`
**BR-FTC**: `total_ftc`, `IRRF_*`

## Step 3: Canonical filename

```
YYYY__<Form>__<Issuer>__<Recipient>.<ext>
```

Examples:
```
2025__W2__Acme-Corp__Sterling.pdf
2025__1099__Vault-X93__Sterling.pdf
2025__1098__National-Bank__Sterling.pdf
2025__1095-C__Employer__Sterling.pdf
2025__BR-TAX-REPORT__Brazilian-Investments.xlsx
```

Destination: `$WORKSPACE/tax/<YYYY>/`

## Step 4: Sidecar format

```json
{
  "schema_version": "1",
  "source": "tax",
  "source_file": { "path": "tax/2025/...", "sha256": "...", ... },
  "classified_at": "...",
  "classified_by": "agent",
  "data": {
    "tax_year": 2025,
    "document_type": "W2",
    "issuer": "Acme-Corp",
    "recipient": "Sterling",
    "category": "Income",
    "amount": 125000.00,
    "currency": "USD",
    "form_data": {
      "federal_tax_withheld": 28500.00,
      "state_tax_withheld": 6200.00
    }
  }
}
```

For multi-document files (Brazilian XLSX):
```json
{
  "data": {
    "tax_year": 2025,
    "documents": [
      { "document_type": "BR-TAX-REPORT", "issuer": "...", ... },
      { "document_type": "BR-FTC", "issuer": "...", ... }
    ]
  }
}
```

## Step 5: Escalation rules

| Condition | Action |
|---|---|
| Form type can't be determined | STOP — ask user |
| OCR returns no text (image-only PDF/JPG) | Note it; extract what you can; flag as low-confidence |
| Amount is $0 on a form that should have an amount | Flag in sidecar as `"amount_note": "zero — verify"` |
| Tax year unclear | STOP — ask user |

## Step 6: Summary + STOP

Present a summary in conversation (form type, year, issuer, key
amounts for each imported file) and **stop** — do not run ingest.
