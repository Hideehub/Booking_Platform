# Design decisions

The main design choices behind the booking platform, and their tradeoffs.

## 1. Double-booking is prevented by the database

- **Context:** Two customers can request the same staff member's time at the same moment. Checking "is it free?" and then inserting is a race.
- **Decision:** A Postgres exclusion constraint: `EXCLUDE USING gist (staff_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&) WHERE status IN ('pending_payment', 'confirmed')`, with `btree_gist` enabled in its own migration. Half-open ranges allow back-to-back bookings.
- **Alternatives rejected:** Python checks with `select_for_update` (there is no row to lock when the slot is free); a unique `(staff, start_at)` (misses partial overlaps).
- **Consequences:** Exactly one concurrent insert or update wins. The loser's `IntegrityError` is identified by constraint name and shown as "that time was just taken". Rules that span tables (staff offers the service, same business) live in `Booking.clean()`.

## 2. One server-side path creates bookings

- **Context:** The client submits a service, staff member and start time, none of which can be trusted.
- **Decision:** `create_booking_hold()` is the only creation path. It requires the start time to still be in `get_available_slots()` for that day. That single check covers past, off-grid, outside-hours, time-off and taken slots. Overlap is left to the constraint.
- **Alternatives rejected:** validation in views or forms (duplicated and easy to bypass).
- **Consequences:** The Django admin is a second path, but it still runs `full_clean()`, including the constraint check.

## 3. Pending holds and expiry

- **Context:** A slot must be reserved while the customer pays, but abandoned checkouts must not block it.
- **Decision:** Bookings start as `pending_payment` with `hold_expires_at = now + 15 min`, and block slots only while the hold is live. Lapsed holds are marked `expired` just before a new hold is created for that staff member. Services with no deposit are confirmed immediately.
- **Alternatives rejected:** extending the hold when the customer clicks Pay (it could be extended indefinitely).
- **Consequences:** The constraint treats a lapsed hold as active until its status changes. A periodic Celery sweep is planned; until then, an unswept lapsed hold can block an admin booking or a late payment for that slot. Holds are not rate-limited yet.

## 4. UTC storage, local working hours

- **Context:** Businesses work in local time, possibly in zones with daylight saving.
- **Decision:** Bookings, time off and payments are stored in UTC and displayed in the business's IANA timezone. Working hours are stored as local wall-clock times (weekday, start, end), so "opens at 09:00" survives DST changes.
- **Alternatives rejected:** working hours in UTC (they shift by an hour twice a year); the timezone as a `choices` field (migrations change whenever the timezone database changes).
- **Consequences:** Slot generation converts each shift to UTC for a concrete date. Shifts can't cross midnight. The Django admin currently shows UTC.

## 5. Slot generation is a pure function

- **Context:** Availability (shifts, breaks, time off, bookings, DST) is the most error-prone logic in the system.
- **Decision:** `generate_slots()` takes plain values, including `not_before` instead of reading the clock. It steps every 15 minutes from each shift start (touching shifts are merged) and keeps candidates that fit the shift and overlap nothing busy. `get_available_slots()` loads the inputs in a fixed 5 queries.
- **Alternatives rejected:** interval subtraction (more edge cases, no gain at about 40 × 20 checks per day); model methods that mix queries and logic.
- **Consequences:** The rules are unit-tested without a database, including DST transitions and zones far from UTC.

## 6. Booking flow state lives in the URL

- **Context:** Booking takes five steps (service, staff, date, time, details), rendered on the server with HTMX.
- **Decision:** The query string (`?service=&staff=&date=&start=`) is the state. One view renders all completed steps. HTMX requests get the `#flow` fragment, others the full page, and responses send `Vary: HX-Request`.
- **Alternatives rejected:** wizard state in the session (breaks the back button and multiple tabs); one endpoint per step.
- **Consequences:** Works without JavaScript, and links are shareable. Invalid or stale parameters are dropped rather than treated as errors.

## 7. Money in integer minor units

- **Context:** Deposits are charged through Paystack, whose API takes amounts in minor units.
- **Decision:** Amounts are integers in minor units (`*_minor`; kobo for NGN), with a currency on the business. A booking copies the service's deposit when it's created.
- **Alternatives rejected:** floats (rounding errors); `DecimalField` (correct, but needs converting at every Paystack call).
- **Consequences:** Later price changes don't affect existing bookings. Currency is free text, not yet validated against Paystack's supported currencies.

## 8. Unguessable booking URLs

- **Context:** Customers book without accounts, so the booking page URL is their only access.
- **Decision:** Customer-facing URLs use `Booking.public_id` (UUID4); integer IDs are never exposed. The column was added in three migration steps (nullable → backfill → unique) so existing rows each get their own value.
- **Alternatives rejected:** sequential IDs (anyone can step through them); signed tokens (more machinery for the same result).
- **Consequences:** Anyone holding the link can view the booking, so links should be treated as secrets.

## 9. Payment references are ours and saved first

- **Context:** Every Paystack transaction must be matchable to our records, and one booking must never have two checkouts open.
- **Decision:** We generate the reference and commit the `Payment` row before calling Paystack's initialize API, outside the transaction. Under a row lock on the booking, an open payment's checkout URL is reused rather than creating a second transaction.
- **Alternatives rejected:** references generated by Paystack (only known after the call); calling Paystack inside the transaction (holds a lock across a network call, and a rollback could orphan a live transaction).
- **Consequences:** Every webhook finds its row. Initialization failures mark the payment `failed`, and the customer can retry.

## 10. Verified, idempotent webhook

- **Context:** Paystack confirms payments by webhook to a public endpoint and may deliver the same event more than once.
- **Decision:** Verify the HMAC-SHA512 of the raw body with `hmac.compare_digest`. Then, in one transaction: lock the payment by its unique reference, return early if already processed, check the amount and currency, and confirm the booking. The customer's redirect back from checkout is never trusted.
- **Alternatives rejected:** IP allowlisting as the main check; processing in a queue (the work is a few database writes).
- **Consequences:** Repeated or concurrent deliveries are no-ops. Only bad signatures or bodies get a 400; unknown references and mismatches get a 200 (retrying can't fix them) and are logged. Prod refuses to start with an empty secret key.

## 11. Payments that can't be honoured become `refund_due`

- **Context:** A payment can arrive after its hold lapsed, after the slot went to someone else, or for a booking that is already paid.
- **Decision:** A late payment confirms the booking if the exclusion constraint allows it. Otherwise, and for already-confirmed or cancelled bookings, the payment is marked `refund_due`.
- **Alternatives rejected:** automatic refunds through Paystack's API (deferred).
- **Consequences:** Refunds are made manually in the Paystack dashboard. There is not yet a status to record that a refund was made.

## 12. Configuration from the environment, failing fast in prod

- **Context:** The same code runs locally, in CI and on Render.
- **Decision:** `config/settings/{base,dev,prod}.py`, chosen by `DJANGO_SETTINGS_MODULE`; values come from the environment via `django-environ`. `SECRET_KEY`, `DATABASE_URL` and `PAYSTACK_SECRET_KEY` have no defaults outside dev.
- **Alternatives rejected:** a single settings file with `if DEBUG:` branches; committed `.env` files.
- **Consequences:** A misconfigured deploy fails at startup instead of running insecurely. A test runs `check --deploy` against prod settings.
