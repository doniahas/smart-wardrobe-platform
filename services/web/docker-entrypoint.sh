#!/bin/sh
# Hardening: this container must run the app as a non-root user, but
# /app/uploads is a host bind mount (docker-compose.yml's
# ./services/web/uploads:/app/uploads) whose real ownership is decided by
# the host, not this image - it can be root:root (a fresh Linux host
# auto-creating the mount point on first `docker compose up`), some other
# host UID, or effectively unrestricted (observed on Docker Desktop's
# Windows/WSL2 file sharing). A static `USER appuser` in the Dockerfile
# cannot account for that at build time.
#
# This entrypoint starts as root (the container's normal starting
# privilege - nothing here is a broad/world-writable workaround), fixes
# ownership of exactly the one path that needs to be writable, then drops
# to the unprivileged appuser for the rest of the container's life via
# `exec setpriv`, which replaces this shell process outright (correct PID 1
# / signal handling - "$@" becomes the actual uvicorn process, not a
# lingering wrapper). setpriv is already part of util-linux in the base
# python:3.11-slim image - no extra package installed for this.
set -e

mkdir -p /app/uploads
chown -R appuser:appgroup /app/uploads

exec setpriv --reuid=1000 --regid=1000 --init-groups --no-new-privs -- "$@"
