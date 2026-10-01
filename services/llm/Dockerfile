FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Hardening: run as a dedicated unprivileged user rather than the
# image's default root - this service never writes to the filesystem at
# runtime (stateless HTTP service, no volumes), so a plain USER switch is
# sufficient - no writable bind-mounted path to reconcile ownership for
# (contrast with services/web/Dockerfile's uploads mount).
RUN groupadd -r appgroup --gid=1000 \
    && useradd -r -g appgroup --uid=1000 --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appgroup /app
USER appuser

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
