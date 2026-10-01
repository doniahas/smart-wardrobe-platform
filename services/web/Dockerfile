FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Hardening: run as a dedicated unprivileged user rather than the
# image's default root. Unlike recommendation/llm/catalog, this service
# has a writable, host-bind-mounted path (/app/uploads - see
# docker-compose.yml) whose real ownership isn't known at image build
# time, so the actual user switch happens in docker-entrypoint.sh (started
# as root, drops to appuser via setpriv) rather than a static USER line
# here - see that script for the full reasoning. This RUN still creates
# the user/group and chowns the baked-in app code so it's owned by (and
# readable under) appuser once the entrypoint drops privileges.
RUN groupadd -r appgroup --gid=1000 \
    && useradd -r -g appgroup --uid=1000 --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appgroup /app

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
