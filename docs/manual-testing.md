# Manual testing

A walkthrough of the main flows on a local stack. Start with [Running locally](../README.md#running-locally): `docker compose up` is running and you have a superuser.

Times in the admin are shown and entered in **UTC**. Customer pages and emails use the business's timezone (Africa/Lagos is UTC+1).

## 1. Create demo data

In the admin (http://localhost:8000/admin/):

1. **Businesses → Add business**
   - Owner: your superuser · Name: `Glow Salon` · Slug: `glow-salon` · Timezone: `Africa/Lagos` · Currency: `NGN`
   - Under **Services**, add one: Name `Haircut` · Duration `00:30:00` · Price minor `1000000` · Deposit minor `200000` (amounts are in kobo: ₦10,000 and ₦2,000)
   - Save.
2. **Staff → Add staff**
   - Business: `Glow Salon` · Name: `Ada` · Services: move `Haircut` to the chosen list
   - Under **Working hours**, add a row for each day Ada works, e.g. Monday–Saturday, start `09:00`, end `17:00` (local business time, not UTC)
   - Save.

## 2. Make a booking

1. Open http://localhost:8000/b/glow-salon/
2. Choose **Haircut** → **Ada** → a date → a time.
3. Enter a name and email, then click **Hold this time**.
4. You land on the booking page (`/bookings/<id>/`), which says "We're holding this time until …". The hold lasts 15 minutes.

## 3. Confirm it with a simulated Paystack webhook

Paystack can't reach `localhost`, so a management command sends a correctly signed `charge.success` webhook instead.

1. On the booking page, click **Pay deposit**. With the default fake key, Paystack refuses the request and you'll see "We couldn't reach the payment provider. Please try again." A payment record is still created.
2. In the admin, open **Payments** and copy the newest **Reference** (starts with `bk_`).
3. Run:
   ```bash
   docker compose exec web python manage.py simulate_paystack_webhook <reference>
   ```
   Expected output: `Webhook for bk_… → HTTP 200`
4. Reload the booking page: "Your booking is confirmed. See you then!"
5. Run the command again: still `HTTP 200`, and nothing changes. Webhooks are idempotent.

With a real Paystack test key in `.env` (`PAYSTACK_SECRET_KEY=sk_test_…`, then `docker compose up -d --force-recreate web worker beat`), **Pay deposit** opens Paystack's test checkout instead. After paying, the booking page shows "Payment received — confirming your booking…" until you run the command above with the `reference` from the page URL.

## 4. See the confirmation email

Emails print to the worker's logs in development:

```bash
docker compose logs -f worker
```

Within a few seconds of step 3 you'll see the email, starting with a line like:

```
Subject: Booking confirmed: Haircut on Tue 20 Oct at 11:00
```

The times are in the business's timezone, and the email links back to the booking page.

## 5. Test double-booking in two tabs

1. In tab A, go through the booking page until the details form appears for a time slot.
2. Copy the URL from tab A's address bar and open it in tab B. Both tabs now show the same slot.
3. Fill in the details and click **Hold this time** in tab A: you get a booking page.
4. Click **Hold this time** in tab B: the page says "That time is no longer available." and that time is no longer listed.

The database rejects overlapping active bookings for the same staff member. If both submissions arrive at exactly the same moment, the loser sees "Sorry, that time was just taken." instead.

## 6. Watch a hold expire

**Real time:** make a booking (step 2) and don't pay. After 15 minutes the booking page says "This hold has expired." and the time is offered again on the booking page.

**Faster:**

1. Make a booking and don't pay.
2. In the admin, open **Bookings**, open the new booking (status *Pending Payment*), set **Hold expires at** to a time a few minutes in the past (UTC), and save.
3. Reload the customer's booking page: "This hold has expired." The time is offered again on the booking page straight away.
4. Within a minute, the scheduled sweep marks it expired:
   ```bash
   docker compose logs worker | grep "lapsed hold"
   ```
   shows `Expired 1 lapsed hold(s)`, and the booking's status in the admin is *Expired*.

## Reset

`docker compose down -v` deletes the database. The next `docker compose up` starts empty.
