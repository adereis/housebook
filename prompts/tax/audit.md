# SOP: Tax Audit

## Objective
Reconcile tax data sources to produce accurate figures for tax reporting.

## Prerequisites
- Read `config/user_profile.json` for the user's institutions list and any expected data gaps that affect tax document matching.

## Step 1: Document Discovery & Aggregation
- Scan `input-sources/` for tax documents.
- Query all `UNVERIFIED` records from the `tax_documents` table for the target year.
- Group entries by Issuer, Asset, and Category.

## Step 2: Cross-Reference & Deduplication
- **Source Hierarchy**: Ensure that multiple documents representing the same income or deduction event are deduplicated.
- **Deduplication Rule**: Always rely on the most authoritative source for year-end totals.

## Step 3: Specific Asset Validation
- Review documents for correct categorization.
- Identify any discrepancies between reported income and corresponding tax forms.

## Step 4: Finalize
- Present an "Audit Findings" summary to the User.
- Identify any gaps (e.g., Income found but no Tax entry).
- Upon approval, update database records to `VERIFIED` and `needs_review=False`.
