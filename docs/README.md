# Smart Wardrobe Documentation

Technical documentation for Smart Wardrobe. It describes the system as
implemented — the code is the source of truth — covering the browser
frontend, the backend services, the local AI components, and the
end-to-end workflows that connect them.

For a project overview and quick start, see the root
[`README.md`](../README.md).

## Contents

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | Services and containers, why the system is split the way it is, cross-cutting design decisions (ownership scoping, grounding, local-only AI, shared Redis state), and step-by-step data flows for recommendations, catalog suggestions, and chat. |
| [features.md](features.md) | Every feature, organized by subsystem, with its status and API surface. |
| [data-model.md](data-model.md) | MongoDB collections, indexes, and the model-layer guarantees behind them. |
| [testing.md](testing.md) | Test strategy and categories, how to run each tier (including the end-to-end recipes), coverage by service, CI coverage, and known limitations. |
| [security-testing-report.md](security-testing-report.md) | The adversarial security test suite: what is covered, results, and residual risks. |
| [deployment.md](deployment.md) | Self-hosted production deployment with the Compose overlay and Caddy, horizontal scaling, persistence, backups, and recovery. |

## Suggested reading order

1. **architecture.md** — the mental model of the system.
2. **features.md** — what each part does for the user.
3. **testing.md** and **security-testing-report.md** — how behavior is
   verified.
4. **deployment.md** — how to run it in production.
5. **data-model.md** — reference when working with the database.
