# SOP: Tax Document Extraction
This procedure defines how to manually extract data from a tax document (PDF or Image).

## Step 1: Metadata Mining (Configuration Phase)
- Search the headers for **Tax Year**, **Account Number**, and **Institution ID**.
- Update the system `config/` or report parameters with these found values.

## Step 2: Read Raw Text
- Use `pdftotext -layout` to get the raw content.
- Locate the "Issuer", "Tax Year", and "Category".

## Step 3: Critical Evaluation (Agent Intelligence)
Do not merely extract numbers; evaluate the document's purpose:
1. **1099-R (Retirement)**:
    - Check **Box 7 (Distribution Code)**. If Code is **'G'**, it is a **Direct Rollover**.
    - Verify **Box 2a (Taxable Amount)**. If Box 2a is $0.00, do not count the distribution as taxable income.
2. **1099-HC (Healthcare)**:
    - This is a **Verification Form**, not an income form.
    - Categorize as **Health**. The amount is typically $0.00.
    - Purpose is to verify compliance with the individual mandate.
3. **1098 (Mortgage)**:
    - This is a **Deduction**.
    - Extract **Box 1 (Mortgage Interest)** as the primary amount.
    - Extract **Box 10 (Real Estate Taxes)** and store in raw_data.
4. **W-2 (Wages)**:
    - Always extract **Box 17 (State Income Tax)**. Do not omit state-level withholdings.
    - Check Box 12 for retirement (D, AA) and HSA (W) contributions.

## Step 4: Multi-Modal Fallback
- If `pdftotext` returns empty or only whitespace, the PDF is likely a scan/image.
- **Mandate**: Switch to vision-based processing (Base64 extraction) to "read" the document manually.

## Step 5: Identify Income Lines
- Look for "Interest", "Wages", or "Dividends".
- Calculate the **Gross Amount** (Net + Tax Withheld).

## Step 6: Identify Tax Withheld
- Look for "Federal Tax Withheld" or "State Tax Withheld".
- Record the exact amount and currency.

## Step 7: Database Verification
- Check if an entry already exists for this issuer/amount.
- Insert or update the record in `tax_documents` with `needs_review=True`.
