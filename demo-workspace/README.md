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

### 3. Trip Detection & Correlation
The system has automatically identified and correlated 16 major trips. Look out for the annual family vacation puns:
*   **2022**: *The Bull Market Beach Bash* (Hawaii)
*   **2023**: *The Great Recession Retreat* (White Mountains)
*   **2025**: *The Dividend Discovery* (Italy)
*   ...plus several professional "Fiduciary Forums" (Work Trips).

### 4. Tax Readiness
The `tax_documents` table is pre-populated with 5 years of W2s and 1098 Mortgage Interest statements, demonstrating how the system tracks multi-year tax liabilities and deductions.

### 5. The "Audit" Workflow
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
