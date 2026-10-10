# syntax=docker/dockerfile:1

# Tailwind standalone CLI: a single binary, no Node. Pinned version, and the
# download is rejected unless it matches the checksum from the release's
# sha256sums.txt. One stage per CPU architecture; only the needed one runs.
FROM scratch AS tailwind-amd64
ADD --checksum=sha256:dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-linux-x64 /tailwindcss

FROM scratch AS tailwind-arm64
ADD --checksum=sha256:55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195 \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-linux-arm64 /tailwindcss

FROM tailwind-${TARGETARCH} AS tailwind


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY --from=tailwind --chmod=755 /tailwindcss /usr/local/bin/tailwindcss

# Install dependencies before copying the code so this layer is cached
# until a requirements file changes.
# Production by default; docker-compose builds with requirements-dev.txt.
COPY requirements.txt requirements-dev.txt ./
ARG REQUIREMENTS=requirements.txt
RUN pip install -r ${REQUIREMENTS}

COPY . .

# Bake the compiled CSS into the image for production. In dev the `css`
# compose service rebuilds it on every template change instead.
RUN tailwindcss -i assets/app.css -o static/css/app.css --minify

# Fingerprint and compress static files into staticfiles/ for WhiteNoise.
# Prod settings insist on real secrets, so give it throwaway build-time values
# (nothing here connects to a database or sends anything).
RUN DJANGO_SETTINGS_MODULE=config.settings.prod \
    SECRET_KEY=build-only DATABASE_URL=sqlite:////tmp/build.sqlite3 \
    PAYSTACK_SECRET_KEY=build-only SITE_URL=https://build.invalid EMAIL_URL=consolemail:// \
    python manage.py collectstatic --noinput

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Web server + Celery worker (beat embedded) in one container; see bin/start.sh.
CMD ["bin/start.sh"]
