#!/usr/bin/env python3
"""Concurrent-chat load/stress test - exercises the REAL asynchronous chat queue path (POST /chat/jobs +
GET /chat/jobs/{id}) against a live, running Smart Wardrobe stack, the way
real concurrent users would: several separate authenticated accounts,
submitting jobs at the same time, polling for completion over HTTP.

This is NOT part of the regular pytest suite (see
services/web/tests/test_chat_concurrency.py for the fast, deterministic,
no-Docker-required concurrency-correctness tests) - it requires the actual
Docker Compose stack (web + MongoDB + llm + Ollama) up and reachable, and it
calls the real local Ollama model, so a run takes real wall-clock time and
should be run deliberately, not on every commit.

Requirements
------------
    pip install httpx            # required
    pip install pymongo          # optional - only for --mongo-uri extras

Usage
-----
    # 1. Start the real stack (from the repo root):
    docker compose up -d

    # 2. Confirm it's healthy, then run a conservative default load:
    python scripts/chat_load_test.py

    # A bigger run (raise the chat/default rate limits first - see "Rate
    # limiting" below):
    python scripts/chat_load_test.py --users 10 --jobs-per-user 3

    # With direct-Mongo extras (duplicate-processing evidence + cleanup of
    # the test users/jobs this run created):
    python scripts/chat_load_test.py --mongo-uri "mongodb://user:pass@localhost:27017/?authSource=admin" --cleanup

Rate limiting - read before raising --users/--jobs-per-user
-------------------------------------------------------------
The app's rate limiter (services/web/app/core/limiter.py) keys buckets by
*source IP*, not by authenticated user (see RATE_LIMIT_* in .env.example).
Running this script from one machine against a local stack means every
simulated user shares the SAME bucket for /auth/register (default
5/minute), /auth/login (default 5/minute) and /chat/jobs (default
20/minute) - creating several accounts does NOT get you separate rate-limit
allowances the way it would for genuinely distinct client IPs. This script:
  - registers users with a small stagger to stay under the default
    RATE_LIMIT_AUTH window at the default --users count;
  - defaults to a total job count comfortably under the default
    RATE_LIMIT_CHAT (20/minute).
For a larger stress run, raise RATE_LIMIT_CHAT (and RATE_LIMIT_AUTH if
--users is large) in your .env before `docker compose up`, then increase
--users/--jobs-per-user here to match. This is a genuine, pre-existing
characteristic of the app's IP-based rate limiting (part of its
security hardening) - out of scope for this script to change, but worth knowing
when interpreting a run's numbers (a burst of 429s means you hit the
limiter, not the chat queue).

What this measures
-------------------
For every submitted job, always (no extra flags needed):
  - **client-observed latency**: submit -> first-seen-terminal, from this
    script's own monotonic clock. Includes this script's poll-interval
    slack (a job that finishes 0.1s before a poll is observed ~poll-interval
    late) - it measures what a real polling client would experience, not
    the queue's internal timing.
  - **server end-to-end latency**: `completed_at - created_at`, both taken
    from the API's JSON responses (POST /chat/jobs's immediate response +
    GET /chat/jobs/{id}'s terminal response). This is server-side wall time
    from job creation to resolution, independent of this script's poll
    interval, but it does NOT separate "time spent queued" from "time spent
    actually calling the LLM".

**Queue wait time (`started_at - created_at`) is only measured when
`--mongo-uri` is given.** `started_at` is intentionally not part of the
public `GET /chat/jobs/{id}` response (see services/web/app/models.py::
ChatJobPublic - exposing it there solely for this script was rejected as
scope creep on the public API), so isolating "time spent pending" from
"time spent processing" requires reading the job documents directly from
MongoDB. Without `--mongo-uri`, this script does NOT claim to measure queue
wait - only the two latencies above.

Aggregated: submitted/completed/failed/timed-out counts, elapsed wall time,
throughput (completed jobs/minute), and mean/median/p95/min/max for each
latency actually collected. See --help for every knob.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field

try:
    import httpx
except ImportError:
    print("This script requires httpx: pip install httpx", file=sys.stderr)
    raise SystemExit(1)


@dataclass
class JobRecord:
    user_index: int
    job_index: int
    job_id: str | None = None
    submit_monotonic: float | None = None
    terminal_monotonic: float | None = None
    status: str | None = None  # "completed" | "failed" | "timeout" | "submit_error" | "poll_error"
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error_reason: str | None = None
    http_error: str | None = None


@dataclass
class RunResult:
    jobs: list[JobRecord] = field(default_factory=list)
    users_registered: int = 0
    users_failed: int = 0
    started_wall: float = 0.0
    ended_wall: float = 0.0


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime, timezone

    # The API is inconsistent about timezone suffixes depending on whether a
    # datetime went through a MongoDB round-trip: POST /chat/jobs's
    # immediate created_at is an in-memory tz-aware datetime -> serializes
    # with "Z"/"+00:00"; anything read back via GET /chat/jobs/{id} comes
    # from motor, which returns naive datetimes (tzinfo stripped, value
    # still UTC) -> serializes with NO suffix at all. A naive
    # datetime.fromisoformat(...).timestamp() call assumes the *local*
    # timezone, silently corrupting every duration computed against a
    # tz-aware value from the other source - so naive strings here are
    # always explicitly treated as UTC, never left for Python to guess.
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def _register_or_login(
    client: httpx.AsyncClient, username: str, password: str, email: str
) -> str | None:
    """Register a fresh test account (or log in if it already exists from a
    prior run) and return its bearer token, or None on failure."""
    resp = await client.post(
        "/auth/register", json={"username": username, "email": email, "password": password}
    )
    if resp.status_code not in (201, 409):
        print(f"  [{username}] register failed: {resp.status_code} {resp.text[:200]}")
        return None

    resp = await client.post("/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        print(f"  [{username}] login failed: {resp.status_code} {resp.text[:200]}")
        return None
    return resp.json()["access_token"]


async def _submit_and_poll_one_job(
    client: httpx.AsyncClient,
    token: str,
    record: JobRecord,
    message: str,
    poll_interval: float,
    job_timeout: float,
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    record.submit_monotonic = time.monotonic()
    try:
        resp = await client.post("/chat/jobs", json={"message": message}, headers=headers)
    except httpx.HTTPError as exc:
        record.status = "submit_error"
        record.http_error = str(exc)
        return

    if resp.status_code != 202:
        record.status = "submit_error"
        record.http_error = f"{resp.status_code}: {resp.text[:200]}"
        return

    body = resp.json()
    record.job_id = body["job_id"]
    record.created_at = body["created_at"]

    deadline = time.monotonic() + job_timeout
    while time.monotonic() < deadline:
        try:
            resp = await client.get(f"/chat/jobs/{record.job_id}", headers=headers)
        except httpx.HTTPError as exc:
            record.status = "poll_error"
            record.http_error = str(exc)
            return

        if resp.status_code != 200:
            record.status = "poll_error"
            record.http_error = f"{resp.status_code}: {resp.text[:200]}"
            return

        body = resp.json()
        if body["status"] in ("completed", "failed"):
            record.terminal_monotonic = time.monotonic()
            record.status = body["status"]
            record.completed_at = body.get("completed_at")
            record.error_reason = body.get("error_reason")
            return

        await asyncio.sleep(poll_interval)

    record.status = "timeout"


async def _run_one_user(
    client: httpx.AsyncClient,
    user_index: int,
    username: str,
    password: str,
    jobs_per_user: int,
    poll_interval: float,
    job_timeout: float,
    results: list[JobRecord],
) -> bool:
    """Returns True iff this simulated user authenticated successfully."""
    # NOT "@example.com"/"@...invalid" - the app's EmailStr validator
    # (email-validator) rejects RFC 2606 reserved domains (example.*,
    # *.invalid, *.test, *.localhost) outright, syntax-only, no DNS lookup
    # involved - a non-reserved-looking domain is required even though
    # nothing here ever actually sends mail.
    token = await _register_or_login(client, username, password, f"{username}@m15loadtest-smartwardrobe.com")
    if token is None:
        for j in range(jobs_per_user):
            results.append(
                JobRecord(user_index=user_index, job_index=j, status="submit_error",
                          http_error="auth failed")
            )
        return False

    records = [JobRecord(user_index=user_index, job_index=j) for j in range(jobs_per_user)]
    results.extend(records)
    await asyncio.gather(
        *(
            _submit_and_poll_one_job(
                client,
                token,
                rec,
                message=f"Load test user {user_index} job {rec.job_index}: what should I wear today?",
                poll_interval=poll_interval,
                job_timeout=job_timeout,
            )
            for rec in records
        )
    )
    return True


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _latency_stats(values: list[float]) -> dict:
    """Shared count/mean/median/p95/min/max summary for any list of
    second-denominated durations - used for every latency this script
    actually collects (client-observed, server end-to-end, and, when
    --mongo-uri is given, queue wait)."""
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 3) if values else None,
        "median": round(statistics.median(values), 3) if values else None,
        "p95": round(_percentile(values, 0.95), 3) if len(values) >= 2 else None,
        "min": round(min(values), 3) if values else None,
        "max": round(max(values), 3) if values else None,
    }


def _summarize(result: RunResult, worker_concurrency_hint: str) -> dict:
    jobs = result.jobs
    completed = [j for j in jobs if j.status == "completed"]
    failed = [j for j in jobs if j.status == "failed"]
    timed_out = [j for j in jobs if j.status == "timeout"]
    errored = [j for j in jobs if j.status in ("submit_error", "poll_error")]
    in_flight = [j for j in jobs if j.status is None]

    # Server end-to-end latency: completed_at - created_at, both from the
    # API's own JSON responses. See the module docstring's "What this
    # measures" section for exactly what this does/doesn't capture.
    server_latencies = []
    for j in completed + failed:
        c_epoch = _iso_to_epoch(j.created_at)
        done_epoch = _iso_to_epoch(j.completed_at)
        if c_epoch is not None and done_epoch is not None:
            server_latencies.append(done_epoch - c_epoch)

    # Client-observed latency: this script's own monotonic clock, submit ->
    # first-seen-terminal. Always collected (no --mongo-uri needed) since
    # submit_monotonic/terminal_monotonic are recorded by every poll loop.
    client_latencies = [
        j.terminal_monotonic - j.submit_monotonic
        for j in completed + failed
        if j.submit_monotonic is not None and j.terminal_monotonic is not None
    ]

    elapsed = result.ended_wall - result.started_wall
    throughput_per_min = (len(completed) / elapsed * 60) if elapsed > 0 else 0.0

    return {
        "configured_worker_concurrency_hint": worker_concurrency_hint,
        "users_registered": result.users_registered,
        "users_failed": result.users_failed,
        "jobs_submitted": len(jobs),
        "jobs_completed": len(completed),
        "jobs_failed": len(failed),
        "jobs_timed_out": len(timed_out),
        "jobs_errored_client_side": len(errored),
        "jobs_still_in_flight_at_overall_timeout": len(in_flight),
        "elapsed_seconds": round(elapsed, 2),
        "throughput_completed_per_minute": round(throughput_per_min, 2),
        "client_observed_latency_seconds": _latency_stats(client_latencies),
        "server_latency_seconds": _latency_stats(server_latencies),
        "queue_wait_seconds": (
            "not measured - rerun with --mongo-uri to compute this from "
            "started_at/created_at (see the module docstring)"
        ),
        "errors_sample": [
            {"user": j.user_index, "job": j.job_index, "status": j.status, "detail": j.http_error}
            for j in errored[:10]
        ],
        "failed_reason_sample": [j.error_reason for j in failed[:10]],
    }


async def _fetch_worker_concurrency_hint(base_url: str) -> str:
    """Best-effort only - the app has no admin endpoint exposing
    CHAT_WORKER_CONCURRENCY, so this is just a health-reachability check,
    not a real value lookup. Report the configured value yourself via
    `docker compose exec web env | grep CHAT_WORKER_CONCURRENCY` if needed."""
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            resp = await client.get("/health")
            return "reachable" if resp.status_code == 200 else f"unexpected status {resp.status_code}"
    except httpx.HTTPError as exc:
        return f"unreachable ({exc})"


def _mongo_extras(mongo_uri: str, db_name: str, run_id: str, job_ids: list[str], cleanup: bool) -> dict:
    """Optional: connect directly to MongoDB (bypassing the API) to (a)
    report each job's `attempts` counter as duplicate-processing evidence -
    an attempts value > 1 means a job was claimed more than once (recovered
    by the stale-job reaper and reprocessed; see chat_queue.py::
    reclaim_stale_jobs), which should not normally happen for jobs that
    complete quickly - (b) compute real queue-wait time
    (`started_at - created_at`) directly from the job documents, since
    `started_at` is not part of the public API response (see the module
    docstring), and (c) optionally delete this run's test users/jobs.

    Datetimes read back here are plain pymongo/motor documents, not the
    API's JSON serialization - both `created_at` and `started_at` come from
    the same driver the same way, so (unlike `_iso_to_epoch` above) there is
    no naive-vs-aware mismatch to work around: subtracting one from the
    other is always safe.
    """
    try:
        from bson import ObjectId
        from pymongo import MongoClient
    except ImportError:
        return {"error": "pymongo not installed - skipping --mongo-uri extras (pip install pymongo)"}

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    db = client[db_name]
    try:
        db.command("ping")
    except Exception as exc:
        return {"error": f"could not reach MongoDB at --mongo-uri: {exc}"}

    oids = [ObjectId(jid) for jid in job_ids if jid]
    docs = list(
        db.chat_jobs.find(
            {"_id": {"$in": oids}},
            {"attempts": 1, "status": 1, "created_at": 1, "started_at": 1, "completed_at": 1},
        )
    )
    reprocessed = [str(d["_id"]) for d in docs if d.get("attempts", 1) > 1]

    queue_waits = [
        (d["started_at"] - d["created_at"]).total_seconds()
        for d in docs
        if d.get("started_at") is not None and d.get("created_at") is not None
    ]

    cleanup_result = None
    if cleanup:
        job_delete = db.chat_jobs.delete_many({"_id": {"$in": oids}})
        user_delete = db.users.delete_many({"username": {"$regex": f"^{run_id}_"}})
        cleanup_result = {
            "chat_jobs_deleted": job_delete.deleted_count,
            "users_deleted": user_delete.deleted_count,
        }

    client.close()
    return {
        "jobs_inspected": len(docs),
        "jobs_with_attempts_gt_1": len(reprocessed),
        "reprocessed_job_ids": reprocessed,
        "queue_wait_seconds": _latency_stats(queue_waits),
        "cleanup": cleanup_result,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=5, help="simulated concurrent users (default: 5)")
    parser.add_argument("--jobs-per-user", type=int, default=2, help="jobs each user submits (default: 2)")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="seconds between job status polls")
    parser.add_argument("--job-timeout", type=float, default=150.0, help="per-job max wait before marking 'timeout'")
    parser.add_argument("--overall-timeout", type=float, default=600.0, help="hard cap for the whole run, seconds")
    parser.add_argument("--username-prefix", default="m15loadtest")
    parser.add_argument("--password", default="LoadTest123!")
    parser.add_argument(
        "--mongo-uri", default=None,
        help="optional: direct MongoDB URI for duplicate-processing evidence, real "
             "queue-wait-time measurement (started_at - created_at; NOT available "
             "without this flag, since started_at isn't in the public API response), "
             "and --cleanup",
    )
    parser.add_argument("--database-name", default="smartwardrobe")
    parser.add_argument("--cleanup", action="store_true", help="delete this run's test users/jobs (requires --mongo-uri)")
    parser.add_argument("--output", default=None, help="optional path to write the JSON summary")
    args = parser.parse_args()

    run_id = f"{args.username_prefix}_{uuid.uuid4().hex[:8]}"
    print(f"Chat load test - run_id={run_id}")
    print(f"  base_url={args.base_url} users={args.users} jobs_per_user={args.jobs_per_user} "
          f"total_jobs={args.users * args.jobs_per_user}")
    print("  (see this script's module docstring re: IP-keyed rate limiting before scaling this up)")

    concurrency_hint = await _fetch_worker_concurrency_hint(args.base_url)
    print(f"  web reachability: {concurrency_hint}")
    if concurrency_hint != "reachable":
        print("Aborting: the web service is not reachable at --base-url. "
              "Start the stack first (`docker compose up -d`).", file=sys.stderr)
        return 2

    result = RunResult()
    result.started_wall = time.monotonic()

    async def _run_all():
        async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
            tasks = []
            for i in range(args.users):
                username = f"{run_id}_{i}"
                tasks.append(
                    _run_one_user(
                        client, i, username, args.password, args.jobs_per_user,
                        args.poll_interval, args.job_timeout, result.jobs,
                    )
                )
                # Stagger registrations slightly so a large --users doesn't
                # blast RATE_LIMIT_AUTH (per-IP, see module docstring) all
                # in the same instant.
                await asyncio.sleep(0.05)
            auth_ok = await asyncio.gather(*tasks)
            result.users_registered = sum(1 for ok in auth_ok if ok)
            result.users_failed = sum(1 for ok in auth_ok if not ok)

    try:
        await asyncio.wait_for(_run_all(), timeout=args.overall_timeout)
    except asyncio.TimeoutError:
        # result.jobs was appended to in place by _run_one_user, so whatever
        # progress happened before the hard cap is still reported below -
        # any job still in flight simply has no terminal status set, which
        # _summarize below reports honestly rather than dropping.
        print(f"Overall timeout ({args.overall_timeout}s) reached - stopping and reporting partial results.")

    result.ended_wall = time.monotonic()

    summary = _summarize(result, concurrency_hint)

    if args.mongo_uri:
        job_ids = [j.job_id for j in result.jobs if j.job_id]
        mongo_extras = _mongo_extras(
            args.mongo_uri, args.database_name, run_id, job_ids, args.cleanup
        )
        summary["mongo_extras"] = mongo_extras
        # Promote the real queue-wait measurement (only obtainable via
        # direct Mongo access - see the module docstring) over the
        # "not measured" placeholder _summarize sets by default.
        if "queue_wait_seconds" in mongo_extras:
            summary["queue_wait_seconds"] = mongo_extras["queue_wait_seconds"]
    elif args.cleanup:
        print("--cleanup requested but --mongo-uri not provided - skipping cleanup. "
              f"Test users are identifiable by the username prefix '{run_id}_'.")

    print("\n=== Chat Load Test Summary ===")
    print(json.dumps(summary, indent=2))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\nWrote summary to {args.output}")

    if not args.mongo_uri:
        print(
            f"\nNote: test users were created with the prefix '{run_id}_' and were NOT deleted "
            "(no --mongo-uri given, and there is no account-deletion API). "
            "They are clearly identifiable local test data."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
