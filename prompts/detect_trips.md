# SOP: Proactive Trip Detection

## Objective
Detect clusters of travel-related spending and create confirmed trips in the database with correctly linked transactions.

## Prerequisites
- Database initialized (`housebook-init-db`)
- Transactions ingested (`housebook-cc ingest / housebook-amazon ingest`)
- Consult `config/user_profile.json` (`special_notes`) for dedicated travel cards or expected patterns

## Step 1: Run the Detector
```
housebook-audit detect-trips --json --months 12
```
Parse the JSON output. It contains two sections:
- `candidates`: date-clustered trip suggestions with transaction IDs
- `advance_payments`: flights/hotels paid 1-6 months before a cluster

Tunable parameters:
- `--months N`: look-back window (default 12)
- `--min-transactions N`: minimum travel transactions per cluster (default 3)
- `--gap-days N`: max days between transactions in a cluster (default 3, increase for international trips)

## Step 2: Review Each Candidate
For each candidate, evaluate:
1. **Location**: The `location` field is a hint (often a US state abbreviation). Use transaction descriptions and your knowledge to determine the actual destination (e.g., "MA" + "MARRIOTT BOSTON" → "Boston, MA").
2. **Type**: `work` is set when Work (Reimbursable) transactions are present. Verify this is correct. Set `unknown` candidates to `personal` or `work` based on context.
3. **Boundaries**: Check if start/end dates are correct. The detector uses gap-based clustering; some trips may need manual adjustment.
4. **Naming**: Generate a descriptive name (e.g., "Boston Work Trip Nov 2025", "Italy Family Vacation Jun 2025").

### Heuristics for Review

**Corporate travel platforms (Egencia, Concur, etc.):**
- These platforms bill from their HQ address (e.g., Egencia → "Scottsdale, AZ", Egencia fees → "Bellevue, WA"). The billing address is NOT the trip destination.
- Large Egencia charges ($1000+) are typically bundled hotel+flight bookings. Treat them as advance payments and look for the actual trip cluster weeks/months later.

**Small clusters that are actually advance payments:**
- If a cluster has ≤2 anchor transactions and they are all booking-type charges (Egencia, airlines, hotels), it is likely an advance payment cluster, not a trip. Look for the actual trip later in the timeline and link the bookings to it.

**Uber/Lyft as trip boundary signals:**
- Check `config/user_profile.json` for how the household uses rideshare. When it says local rides are rare, Uber/Lyft charges are strong indicators of active travel.
- Expensive rides (~$100+) are very likely airport transfers and mark trip start/end boundaries.
- However, absence of rideshare does not mean no trip — the user may get rides from others, share Ubers, or pay with a corporate card not tracked here.

**Local vs. trip transactions during a travel window:**
- If part of the household stayed home, local dining and entertainment (in the user's home state) during the trip window are NOT part of the trip. Exclude them.
- **CRITICAL**: Do NOT perform bulk `UPDATE trip_id = X WHERE date BETWEEN ...` without manually excluding local merchants (e.g., Panera, Trader Joe's in the user's home area — check `config/user_profile.json` for home location).
- Cross-check the state/city in descriptions: a local restaurant in the user's home town during a Denver trip is local, not travel.

**Work trips without hotel charges:**
- Some work travel is paid with a corporate card not tracked in this system. A cluster of Uber rides + dining in a trip city (e.g., Denver, CO) without hotel charges may still be a valid work trip.

**Transaction metadata (zip codes, flight routes):**
- Amex statements include structured metadata on continuation lines. After ingestion, query `json_extract(metadata, '$.zip_code')` on Uber transactions to determine if a ride was local (home zip, airport zip) or at a trip destination.
- Consult `config/user_profile.json` for `home_location.zip` and `home_airport.zip` to identify local vs travel rides. Rides matching these zip codes during a trip window are boundary signals (airport transfers), not destination activity.
- Egencia flight metadata (`json_extract(metadata, '$.flight_legs')`) contains the actual route (e.g., BOS→DEN, BOS→AMS→VIE). Use this to unambiguously link advance bookings to trips — don't guess based on amount or timing alone.
- `json_extract(metadata, '$.departure_date')` gives the departure date, which should match the trip start_date.

**Home-area false positives:**
- Consult `config/user_profile.json` for the user's home state and local merchants. Clusters in the home state consisting only of transit (MBTA), local dining, and entertainment are likely routine activity, not trips. Only flag home-state clusters if they contain Egencia, flights, or hotel charges.

## Step 3: Review Advance Payments
For each advance payment:
- Cross-reference with candidates to confirm the correct trip linkage
- Multiple trips in the same window require judgment (e.g., $600 flight likely domestic work trip, $2000 flight likely international vacation)
- Some advance payments may belong to future trips not yet detected — flag these for the user

## Step 4: Database Actions (Manual/CLI)
> **Prerequisite**: Schema must be at version 3+ (`housebook-init-db` handles this automatically).

If using the CLI/SQL directly instead of the API:

1. **Create the Trip**:
   ```sql
   INSERT INTO trips (name, start_date, end_date, status, type, location, created_by)
   VALUES ('Trip Name', 'YYYY-MM-DD', 'YYYY-MM-DD', 'confirmed', 'personal', 'City, State', 'agent');
   ```

2. **Link Transactions**:
   For every transaction ID identified in Step 1 or Step 3:
   ```sql
   UPDATE transactions SET trip_id = <new_trip_id> WHERE id IN (tx_id1, tx_id2, ...);
   ```

3. **Recategorize Work Trip Transactions**:
   All transactions linked to a `work` trip must be set to `Work (Reimbursable)`:
   ```sql
   UPDATE transactions SET category = 'Work (Reimbursable)'
   WHERE trip_id = <new_trip_id>
     AND category != 'Work (Reimbursable)';
   ```
   This includes Uber/Lyft rides, dining, and any other charges during the trip — they were likely ingested as `Local Transit`, `Flights`, `Lodging`, or `Dining & Takeout` but are reimbursable once assigned to a work trip.

4. **Verify Counts**:
   Always run a count check after linkage to ensure the trip summary matches the parsed spend.

## Constraints
- **Exclude Routine**: The detector already excludes Amazon and Transfers. Do not re-add them.
- **No Hallucination**: If location is uncertain, leave it as null. Do not guess.
- **Transfers**: Consider local transport (Uber/Lyft, Taxi) on trip dates as part of the trip.
