# SOP: Smart Agent-Led Financial Audit

## Objective
Use Agent intelligence to verify transaction categorizations from the last 1 year. This moves items from "Best Effort" (Rule-based) to "Verified."

## Prerequisites
- Read `config/user_profile.json` (`special_notes`) before starting. It contains user-specific financial behaviors, dedicated accounts, and expected data gaps that affect categorization decisions.
- Do NOT read `config/rules.json` for audit decisions — rules.json is consumed by the ingestion pipeline ("dumb pipes"). Your role is intelligent review. However, you MUST add new rules to `config/rules.json` when you discover recurring patterns or fix miscategorizations (Step 4).
- **Always check the `metadata` column** when investigating transactions. Credit card statements (especially Amex) embed rich detail: flight legs, passenger names, ticket numbers, departure dates, merchant addresses, etc. This context is essential for trip assignment, refund matching, and disambiguation. Use `housebook-audit pending --json` to get metadata inline.

## CLI Tool: `housebook-audit`

All audit steps below use the `housebook-audit` CLI. Every subcommand supports `--json` for machine-readable output.

```
housebook-audit pending               # List unreviewed transactions
housebook-audit pending --source Amex  # Filter by card
housebook-audit pending --json         # JSON with metadata inline
housebook-audit calibrate              # Show verified category distribution
housebook-audit trips                  # Show trips for assignment context
housebook-audit trips --limit 5        # Limit output

housebook-audit create-trip "Trip Name" --start YYYY-MM-DD --end YYYY-MM-DD \
    --type personal --location "City, Country"

housebook-audit verify 7087,7094-7108 --category "Flights"
housebook-audit verify 7092-7093 --category "Work (Reimbursable)" --trip 15
housebook-audit verify 7088-7090      # Confirm existing category as-is

housebook-audit summary               # Post-audit report
```

## Step 0: Calibration
Before making any category changes, sample existing verified transactions to understand the user's categorization preferences:
```
housebook-audit calibrate
```
Also sample specific categories where you expect to make changes to check for edge cases (e.g., "are deodorants Groceries or Wellness in this dataset?"). The user's existing patterns are the ground truth — stay consistent with them.

## Step 1: Data Gathering
- Run `housebook-audit pending` (or `--json` for programmatic processing).
- Focus ONLY on transactions where `needs_review = 1`.
- Check `housebook-audit trips` for existing trips that may overlap with pending transactions.
- Process in batches of ~200 transactions at a time.

## Step 2: Critical Evaluation
For every item, do not just trust the current category. Look at:
1. **Description Essence**: (e.g., "Fresh Step" is clearly Pets, not "Alcohol").
2. **Context**: (e.g., "Delta" is always Travel, but "Amazon: Delta Faucet" is Home & Garden).
3. **Amount**: Large amounts might suggest unusual events (e.g., a $2000 transaction at "Home Depot" is likely a renovation, not just "Shopping").
4. **User-Specific Nuances**:
    - **Work Trips**: **Mandatory Rule**: All transactions linked to a Work Trip (Marriott stays, airport transfers, trip dining) MUST be categorized as **Work (Reimbursable)** to keep them separate from personal cash flow stats.
    - **Coffee**: Consumables (Pods, Beans, Starbucks Bags) -> **Groceries**. Appliances (Espresso Machines) -> **Shopping & Retail**.
    - **Subscriptions**: Professional/News magazines (Wired, NYT, Guardian) -> **Education**.
    - **Outdoor/Garden**: Yard maintenance items (Hose splitters, fertilizer, sprinklers) -> **Home & Garden**. Grilling supplies (charcoal, propane) -> **Groceries** unless clearly for yard maintenance.
    - **Children/Schools**: Payment portals (Schoolcafe) -> **Education**.
    - **Multi-Purpose Stores (Walmart/Target/BJs)**:
        - Default -> **Groceries**.
        - Expensive items (~$300+) at Walmart/Target -> Check for potential medication or health items. If unsure, prioritize **Health** for round amounts like $300.00.
    - **Amazon Hardware**:
        - Spark Plugs, Car accessories -> **Auto & Fuel**.
        - Yard/Fence/Sprinklers -> **Home & Garden**.
    - **IKEA**: Furniture/home purchases -> **Home & Garden**. Restaurant inside IKEA -> **Dining & Takeout**.
    - **Parking Apps**: PAYBYPHONE -> **Auto & Fuel**.
    - **Tours & Excursions**: Tour operators, excursion booking sites, activity platforms (e.g., Vivaticket, Vivaraviaggi) -> **Entertainment**, not Flights/Lodging/Local Transit. Entertainment = experiences (tickets, tours, museums, concerts). The travel categories (Flights, Lodging, Local Transit) cover transport and accommodation only.
5. **Amazon Context**:
    - Amazon is **NEVER** "Dining & Takeout". If an Amazon item looks like food or coffee, it is **Groceries**.
    - Eligible OTC medicines and first-aid items (Sudafed, bandages) -> **Health**.
    - Supplements, vitamins, personal care, and fitness (NMN, Collagen, Glucosamine) -> **Wellness**.
    - Video Games, Consoles, Electronics, Collectibles (Funko Pops) -> **Shopping & Retail**.
    - **Entertainment** is strictly experiences: tickets, parks, movies, concerts — not physical products.
    - Pet supplies (litter, water fountains, food) -> **Pets**.
    - Otherwise, default to **Shopping & Retail**.
6. **Trip Assignment Guardrails (Personal & Work Trips)**:
    - Date overlap with a trip window is a *necessary* condition for assignment, but NOT a *sufficient* one. For any transaction during a trip window:
        - **Verify Location Relevancy**: Check the merchant location (e.g. `ROMA IT` or `NICE FR`). If the merchant has a local/home state indicator (e.g. `MA`, or the home town in `config/user_profile.json`) or matches a home-location zip code, it must NOT be assigned to the trip unless it is a transition charge (e.g. airport parking or Logan airport dining).
        - **Filter Online/Recurring Transactions**: Subscriptions (e.g. a gym membership, `Google One`, `Netflix`) and online purchases (e.g. `Amazon.com`, `Kindle`) that post during the trip window are home expenses and must NOT be assigned a `trip_id`.
        - **Handle Exceptions Intellectually**: Non-travel categories (like `Shopping & Retail` or `Health`) are valid for trip assignment *only* if the description or metadata confirms they were incurred physically at the trip location (e.g. a souvenir shop in Rome or a pharmacy in Nice).

## Step 3: Action & Verification

Use `housebook-audit verify` to batch-process decisions. The command enforces the status lifecycle automatically (`AGENT_VERIFIED`, `needs_review = 0`).

- If you are **100% Certain**:
    ```
    # Re-categorize and verify
    housebook-audit verify 7094-7108 --category "Local Transit" --trip 18

    # Confirm existing category (no --category flag)
    housebook-audit verify 7087
    ```
- If a rule in `config/rules.json` is **obviously broken** (e.g., substring match error):
    - Fix the rule immediately.
- If you are **Uncertain**:
    - Leave as is for the user to decide in the UI.

## Step 4: Rule Hardening
- Every time you verify a merchant, check if a general rule exists. If not, add it to `config/rules.json`.

## Step 5: Post-Audit Summary
Run `housebook-audit summary` to generate a report of all changes made during the session. Present this to the user for final review before pushing to Drive.
