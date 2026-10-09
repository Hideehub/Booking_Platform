# Booking platform

Appointment booking for clinics, salons and consultants: customers pick a service, staff member and time, hold the slot, and pay a deposit through Paystack.

## Running locally

**Requires:** Docker Desktop (or another Docker engine with Compose v2).

```bash
docker compose up
```

The first run builds the image. This starts six services:

| Service  | What it does                                                          |
|----------|-----------------------------------------------------------------------|
| `web`    | Django dev server on http://localhost:8000 (runs migrations on start) |
| `worker` | Celery worker: sends emails, runs scheduled jobs                      |
| `beat`   | Celery scheduler: hold expiry every minute, reminders every 5 minutes |
| `css`    | Tailwind, rebuilding `static/css/app.css` when templates change       |
| `db`     | Postgres 16                                                           |
| `redis`  | Redis 7 (Celery broker)                                               |

Check it's up: http://localhost:8000/healthz/ returns `{"status": "ok"}`.

In a second terminal, create an admin user:

```bash
docker compose exec web python manage.py createsuperuser
```

| URL                                          | Page                                                  |
|----------------------------------------------|-------------------------------------------------------|
| `http://localhost:8000/admin/`               | Admin: businesses, staff, bookings, payments          |
| `http://localhost:8000/b/<business-slug>/`   | Customer booking page                                 |
| `http://localhost:8000/bookings/<id>/`       | A customer's booking page (link shown after booking)  |

**Configuration:** no `.env` file is needed for local development. Dev settings supply safe defaults, including a fake Paystack key and a console email backend (emails print in the `worker` logs). To override something, copy `.env.example` to `.env` and edit it.

**Checks:**

```bash
docker compose run --rm web pytest
docker compose run --rm web ruff check .
docker compose run --rm web mypy .
```

**Stop:** `Ctrl+C`, then `docker compose down`. Add `-v` to also delete the database.

For a step-by-step walkthrough of booking, payment, emails and hold expiry, see [docs/manual-testing.md](docs/manual-testing.md).
