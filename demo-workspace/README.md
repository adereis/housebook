# 📊 Housebook: The Ledger Family Demo

Welcome to the **Housebook** demo environment! This workspace is designed to showcase the full capabilities of the system using a high-fidelity, fictitious financial history.

## 🏠 The Story: Meet "The Ledger Family"

To provide a realistic yet safe demo, we've created the **Ledger Family**—a household of four (plus a dog) based in Massachusetts. Their story is told through five years of financial data (2021–2026), featuring several "pun-intended" milestones.

### 👥 The Personas
*   **Sterling Ledger (Father)**: A "Senior" Actuary at *Blue Chip Insurance*. He handles the household's "Macro" financial view and travels frequently for work.
*   **Penny Ledger (Mother)**: A freelance "Asset" Manager and Auditor. She balances the family's "Diversified" portfolio of school schedules and local expenses.
*   **Buck Ledger (Son)**: A high-energy toddler who is currently a "Liability" to the living room furniture.
*   **Ally (Allocation) Ledger (Daughter)**: A high-schooler with a "High-Yield" interest in swimming and piano.
*   **Ticker (The Dog)**: A Golden Retriever who provides a consistent "Return on Affection" but requires significant "Liquidity" for premium kibble.

---

## 📈 What’s in the Demo?

### 1. Realistic Inflation (2021–2026)
The data isn't static. You'll notice that grocery bills at *Whole Foods* and *Market Basket*, as well as fuel costs at *Shell*, gradually increase year-over-year to reflect real-world economic trends (approx. 25% total inflation over 5 years).

### 2. Amazon Granularity
Unlike standard bank imports that show a vague "Amazon.com" charge, this demo showcases the **Amazon Provider**. Expenses are broken down by product (e.g., *Kindle Paperwhite*, *LEGO Star Wars*, *Blue Buffalo Dog Food*), allowing for precise categorization across *Pets*, *Electronics*, and *Health*.

### 3. Trips
The history holds 16 trips, each with its own spending page. Look out for the annual family vacation puns:
*   **2022**: *The Bull Market Beach Bash* (Hawaii)
*   **2023**: *The Great Recession Retreat* (White Mountains)
*   **2025**: *The Dividend Discovery* (Rome, Florence & Tuscany)
*   ...plus several professional "Fiduciary Forums" (Work Trips).

**The Dividend Discovery is the showcase.** It is a hand-written two-week
itinerary for a family of four, about $14k all in:
*   **Booked months ahead:** flights in February, the Florence apartment and
    the agriturismo deposit in spring. These count toward the trip even though
    they fall outside its dates.
*   **Paid in euros:** each Italian charge shows its USD amount, with the
    original EUR amount and exchange rate in the transaction's metadata.
*   **More than hotels and dinners:** a train to Florence, a rental car with a
    one-way fee, fuel, parking, a cooking class, a pharmacy stop, groceries
    for the apartment, and airport parking back home in Boston.

Trips are linked by **where** a charge happened, never by date alone. While
the family is in Italy, the mortgage, utilities and piano lessons still bill
at home, and they stay out of the trip. Everyday home spending (groceries,
takeout, fuel) pauses while the family is away. On work trips it continues,
because only Sterling travels. Visits to relatives have no hotel bill.

### 4. HSA Shoebox
The family pays medical bills out of pocket and saves the receipts, so they
can reimburse themselves from the HSA tax-free later. The shoebox holds 13
fictitious medical expenses from 2023 to 2026, with a receipt or
insurance-statement PDF you can open for most of them. Between them they
cover every state the page can show:
*   **Strong** (three sources agree): Sterling's crown, with the insurer's
    statement, Maple Dental's receipt and the card charge.
*   **Ready** (two sources): Ally's glasses, with a receipt and card charge.
*   **Weak** (needs a math proof): two of Penny's physical-therapy visits,
    paid with one combined $130 charge.
*   **Stub** (not enough proof yet): a counseling session paid by check,
    an unpaid lab bill, and last month's ear-infection visit, which so far
    exists only as a card charge.
*   **Excluded**: teeth whitening, which is cosmetic and not HSA-eligible.
*   **Already withdrawn** (2024) and **pending** (the MRI, in a planned
    batch).

Only ready and strong expenses count toward "available for withdrawal"
($534.40 in the demo). The files live in `hsa/YYYY/` beside their JSON
sidecars, just as a real import leaves them.

### 5. Tax Readiness
The `tax_documents` table is pre-populated with 5 years of W2s and 1098 Mortgage Interest statements, demonstrating how the system tracks multi-year tax liabilities and deductions.

### 6. The "Audit" Workflow
While most of the history is marked as `AGENT_VERIFIED`, the **last 30 days of data** are intentionally left as `UNVERIFIED`. This allows you to demo the live "Monthly Audit" workflow using the `prompts/monthly_audit.md` SOP.

---

## 🚀 Running the Demo

This environment is completely isolated in the `demo-workspace/` directory. To launch the app pointing to this data:

```bash
# Set the workspace directory and start the app
HOUSEBOOK_WORKSPACE_DIR=demo-workspace housebook-app
```

## 🛠 Technical Notes
*   **Isolation**: No data from your live workspace is accessed or modified.
*   **Deterministic**: The data is generated using a fixed seed (`42`), ensuring the "Ledger Family" history is consistent every time the seed script is run.
*   **Schema**: The demo database is automatically migrated to the latest schema version.
