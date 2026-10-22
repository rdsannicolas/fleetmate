---
inclusion: always
---

# FleetMate — Product & Engineering Guidelines

These conventions apply to all code, APIs, data contracts, and tests in this project. When a request conflicts with these rules, prefer the rule and flag the conflict.

## Product Overview

FleetMate is an AI-powered vehicle diagnostic assistant. Customers describe vehicle symptoms via a REST API, optionally provide vehicle context (make, model, year), and receive a structured diagnosis containing a probable issue and a repair recommendation. Every interaction is persisted for analytics, but persistence must never block or degrade the customer response.

## Tech Stack

- **Language**: TypeScript (strict mode)
- **Runtime**: Node.js
- **Database**: PostgreSQL
- **AI Integration**: LLM provider accessed only through a provider-agnostic interface
- **Testing**: fast-check for property-based tests; example-based unit and integration tests

## Architecture Principles

- **Stateless API layer** — no server-side session state between requests.
- **AI provider abstraction** — all LLM calls go through a common interface so providers can be swapped without touching business logic. Never call a provider SDK directly from business logic.
- **Non-blocking storage** — persistence runs out of the response path; storage failures never block or alter the customer response.
- **Fail-fast validation** — reject invalid input immediately, before any AI or storage call, with a clear error message.
- **Structured diagnosis output** — always parse AI responses into `probableIssue` and `repairRecommendation` fields; never return raw model text to the customer.
- **Service-layer orchestration** — keep handlers thin; orchestration logic lives in dedicated service layers.

## Code Style

- Use strict TypeScript with explicit interface definitions for all data contracts.
- Prefer `interface` over `type` for object shapes.
- Model error codes as string literal unions (e.g., `'TOO_SHORT' | 'TOO_LONG'`), not free-form strings.
- Represent all timestamps as ISO 8601 UTC strings.
- Use UUID v4 for interaction identifiers.
- Keep functions small and single-purpose.
- Use `async`/`await` for all asynchronous operations — no raw Promise chains.

## API Conventions

- Expose REST endpoints under the `/api/` prefix.
- Include a `timestamp` field (ISO 8601 UTC) in every response.
- Validation errors → `422` with `code` and `suggestion` fields.
- AI or service failures → `503` with a user-friendly `suggestion`.
- Retrieval errors → `400` (bad request) or `404` (not found) as appropriate.
- Never expose internal error details, stack traces, or provider errors to customers.

## Error Handling

- Log all errors with a correlation ID.
- Retry a failed storage write once within 5 seconds; if the retry also fails, log and continue without affecting the response.
- Treat AI calls exceeding 10 seconds as failures — never hang indefinitely.

## Input Validation Rules

Partial vehicle details are accepted; validate only the fields that are provided.

- **Symptom description**: 5–2000 characters.
- **Vehicle year**: 4-digit integer, 1900 to the current year inclusive.
- **Vehicle make / model**: 1–50 characters each.

## Database

- PostgreSQL with an index on `created_at` for efficient date-range queries.
- Retain interaction records for at least 12 months.
- Store vehicle year as an integer with a CHECK constraint of 1900–2100 (schema bound; API validation is stricter — see Input Validation Rules).
- Store make and model as `VARCHAR(50)`.

## Testing

- Property-based tests use fast-check with at least 100 iterations per property.
- Tag each property test with its feature and property name for traceability.
- Cover edge cases and timing behavior (retries, timeouts) in unit tests.
- Verify end-to-end flows, including database interactions, in integration tests.
- Every validation boundary must have a corresponding property test.
