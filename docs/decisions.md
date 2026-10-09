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
- **Decision:** Bookings start as `pending_payment` with `hold_expires_at = now + 15 min`, and block slots only while the hold is live. The constraint still sees a lapsed hold as active until its status changes, so every path that writes bookings first marks that staff member's lapsed holds `expired`: new holds, webhook confirmations, and admin saves (in form validation, before the constraint is checked). Services with no deposit are confirmed immediately.
- **Alternatives rejected:** extending the hold when the customer clicks Pay (it could be extended indefinitely); relying on a periodic sweep alone (a hold can lapse between sweeps).
- **Consequences:** Correctness doesn't depend on a background job. A Celery beat task also expires all lapsed holds every minute, so they don't linger as `pending_payment` in the database. Holds are not rate-limited yet.

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
- **Alternatives rejected:** automatic refunds through Paystack's API (deferred); letting staff edit payment status directly (no audit trail, and any status could be set).
- **Consequences:** Refunds are made manually in the Paystack dashboard, then recorded with an admin action that moves only `refund_due` payments to `refunded`. It needs the `mark_payment_refunded` permission and is written to the admin history. Webhooks for refunded payments are ignored like any other already-processed payment.

## 12. Configuration from the environment, failing fast in prod

- **Context:** The same code runs locally, in CI and on Render.
- **Decision:** `config/settings/{base,dev,prod}.py`, chosen by `DJANGO_SETTINGS_MODULE`; values come from the environment via `django-environ`. `SECRET_KEY`, `DATABASE_URL` and `PAYSTACK_SECRET_KEY` have no defaults outside dev.
- **Alternatives rejected:** a single settings file with `if DEBUG:` branches; committed `.env` files.
- **Consequences:** A misconfigured deploy fails at startup instead of running insecurely. A test runs `check --deploy` against prod settings.

## 13. Booking emails: queued on commit, swept as a backstop, sent at least once

- **Context:** Customers get a confirmation email and a reminder 24 hours before. Celery tasks can run more than once, and the broker can be down when a booking is confirmed.
- **Decision:** On confirmation, the email task is queued with `transaction.on_commit`; a failure to queue is logged, not raised. A beat task every minute sends any confirmation still owed for an upcoming booking, and another every 5 minutes sends due reminders. Each send locks the booking row, skips if `confirmation_sent_at`/`reminder_sent_at` is set, sends, then sets it in the same transaction.
- **Alternatives rejected:** marking before sending (a crash after marking loses the email for good); queueing without `on_commit` (the task could run before the confirmation is visible, or for a rolled-back one); `worker -B` (a second worker would schedule every task twice; beat runs as its own single service).
- **Consequences:** Delivery is at least once: a crash between sending and committing re-sends. Bookings made less than 24 hours ahead get no reminder. On first deploy, confirmed upcoming bookings that predate this feature receive a confirmation email.

## 14. Visual design: self-hosted fonts, CC0 photography, AA contrast

- **Context:** The customer pages needed a premium look, with real photography and custom fonts, without new runtime dependencies or third-party requests.
- **Decision:** A "soft luxe" palette (ivory, espresso, champagne gold) and fonts (Playfair Display, Inter) defined once as Tailwind theme variables. Font files are self-hosted under the SIL Open Font License. Photos are CC0 only (nappy.co, StockSnap, rawpixel), resized with `sips`, kept in `static/img/{hero,cover,gallery}/` and listed in `static/img/CREDITS.md`. Every colour pair used for text was checked against WCAG AA; plain gold fails on ivory, so text and buttons use a deeper gold.
- **Alternatives rejected:** Google Fonts' CDN (a third-party request on every page); images found through general web search (unclear licences); stock faces as staff photos (would misrepresent real people, so staff are shown as initials).
- **Consequences:** Tests fail if a template references a missing image, or an image file isn't credited or isn't in `imagery.py`. The photos are site-wide and salon-themed, so every business, a clinic included, shows them until per-business uploads exist.

## 15. Owner dashboard: every query goes through the owner's business

- **Context:** Business owners manage their own services, staff, hours and bookings. One owner must never see or change another business's data.
- **Decision:** Every dashboard view is wrapped in `owner_view`, which requires login, resolves the URL's slug with `Business.objects.get(slug=…, owner=request.user)`, and runs the view inside `timezone.override(business.timezone)`. Objects inside are always fetched through that business (`get_object_or_404(Staff, pk=pk, business=business)`), and form choices such as a staff member's services are limited to it. Another business's URL returns 404, not 403.
- **Alternatives rejected:** permission checks after loading by primary key (one forgotten check leaks data); reusing the Django admin for owners (row-level scoping in the admin is easy to get wrong, and it doesn't share the site's design).
- **Consequences:** The isolation tests try every dashboard URL against another business, with its slug and with its object IDs under the owner's own slug, and assert a 404 and unchanged data. A test also fails if a new URL is added without an isolation case. Owner accounts are created by a superuser for now; there's no self sign-up.
