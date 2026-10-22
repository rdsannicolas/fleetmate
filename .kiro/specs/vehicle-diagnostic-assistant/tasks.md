# Implementation Plan: Vehicle Diagnostic Assistant (FleetMate)

## Overview

This plan implements the serverless, event-driven AWS design in Python 3.12+ with Boto3. It builds bottom-up: core data models and typed outcomes first, then the shared validation/logging/auth utilities, then the DynamoDB single-table repository, then the Conversation_Manager, AI_Engine (Bedrock adapter), escalation logic, the asynchronous Interaction_Store write path, the diagnostics service orchestration, the thin Lambda handlers + API Gateway endpoints, and finally the CloudFormation infrastructure. Each component is wired into the next so no code is left orphaned.

Property-based tests use Hypothesis (minimum 100 iterations per property), each placed next to the code it validates, tagged `Feature: vehicle-diagnostic-assistant, Property {number}: {property_text}` and annotated with the requirement it checks. Bedrock and DynamoDB are mocked/faked; a fake clock is used for the idle-timeout (Property 8) and timestamp (Property 9) tests. All test sub-tasks are optional (marked `*`); core implementation tasks are never optional.

## Tasks

- [x] 1. Scaffold backend project and define core data models and typed outcomes
  - Create the `backend/src` package layout: `handlers/`, `diagnostics/{services,repositories,models,prompts,events}/`, `shared/{auth,logging,validation,utils}/`, and `tests/`
  - Define data models in `diagnostics/models/`: `Session` (sessionId, customerId, status, createdAt, lastSubmissionAt, interactionCount, ttl), `InteractionRecord` (recordId, sessionId, symptomDescription, result, createdAt), `DiagnosticResult` (probableIssue?, repairRecommendation?, estimatedCost?, confidenceScore?, needsMoreDetail, message?), and `Page` (records, nextPageToken?)
  - Define `SessionStatus` enum (`ACTIVE`, `CLOSED`) and the string-literal error-code set as typed outcomes: `EMPTY`, `TOO_LONG`, `SESSION_CLOSED`, `LIMIT_REACHED`, `AI_TIMEOUT`, `AI_ERROR`, `PERSIST_FAILED`, `ACCESS_DENIED`, `INVALID_RANGE`, `NOT_FOUND`
  - Model errors as explicit typed outcomes (not exceptions crossing component boundaries)
  - Create `backend/requirements.txt` pinning `boto3`, `pytest`, and `hypothesis`; set up the pytest + Hypothesis test framework under `backend/src/tests/`
  - _Requirements: 2.2, 4.1, 4.2, 5.5_

- [x] 2. Implement shared cross-cutting utilities
  - [x] 2.1 Implement the shared symptom validator in `shared/validation/`
    - Implement `validate_symptom(text) -> Valid | Invalid(EMPTY) | Invalid(TOO_LONG)` as a pure, fail-fast function: `EMPTY` when null/empty/whitespace-only, `TOO_LONG` when length exceeds 2000, `Valid` for 1..2000 chars with at least one non-whitespace char
    - _Requirements: 1.2, 1.3, 2.4, 3.2_

  - [x] 2.2 Implement structured JSON logging and Cognito/RBAC auth helpers
    - In `shared/logging/`: emit structured JSON logs with `correlationId`, `service`, `level`, `message`
    - In `shared/auth/`: parse Cognito JWT claims and expose RBAC helpers distinguishing a Customer (owner `sub`) from an analyst (analyst group/claim)
    - _Requirements: 4.5, 5.4_

  - [ ]* 2.3 Write property test for invalid-symptom rejection (validator side)
    - **Feature: vehicle-diagnostic-assistant, Property 3: Invalid symptoms are rejected everywhere with no side effects**
    - Assert empty/whitespace-only/>2000-char inputs map to `EMPTY`/`TOO_LONG` with no side effects at the validator level
    - **Validates: Requirements 1.2, 1.3, 2.4, 3.2**

  - [ ]* 2.4 Write unit tests for logging and auth helpers
    - Assert structured JSON log shape (`correlationId`, `service`, `level`, `message`); assert RBAC resolves Customer-owner vs analyst correctly
    - _Requirements: 4.5, 5.4_

- [x] 3. Implement the DynamoDB single-table repository
  - [x] 3.1 Implement the `FleetMateDiagnostics` repository in `diagnostics/repositories/`
    - Session item (`PK=SESSION#{sessionId}`, `SK=META`) and InteractionRecord item (`PK=SESSION#{sessionId}`, `SK=REC#{createdAt}#{recordId}`) with ascending SK ordering by UTC-millisecond timestamp
    - GSI1 (`GSI1PK`, `GSI1SK={createdAt}#{recordId}`) for analyst date-range queries; TTL attribute on session items
    - Generate UUID v4 record ids; record `createdAt` in ISO 8601 UTC with millisecond precision
    - Expose session CRUD (`get_session`, `put_session`, `update_session`), `append_record`, ordered/paginated `get_session_history(sessionId, pageToken?) -> Page` (max 50, `ScanIndexForward=true`), and `query_by_date_range(start, end)` (ascending)
    - Isolate Boto3 behind the repository so callers never import the SDK; use a faked DynamoDB client in tests
    - _Requirements: 3.1, 3.3, 4.1, 4.2, 4.4, 5.1, 5.5_

  - [ ]* 3.2 Write property test for faithful persistence round-trip
    - **Feature: vehicle-diagnostic-assistant, Property 9: Persisted interactions round-trip faithfully with a UTC millisecond timestamp**
    - Use a fake clock; assert read-back yields identical symptom, result, sessionId, and a UTC millisecond `createdAt`
    - **Validates: Requirements 4.1**

  - [ ]* 3.3 Write property test for globally unique identifiers
    - **Feature: vehicle-diagnostic-assistant, Property 10: Persisted interaction identifiers are globally unique**
    - Assert all assigned record ids are distinct across any set of persisted records
    - **Validates: Requirements 4.2**

  - [ ]* 3.4 Write property test for inclusive ascending date-range queries
    - **Feature: vehicle-diagnostic-assistant, Property 12: Date-range queries return exactly the inclusive in-range records in ascending order**
    - Generate timestamps on and around range boundaries; assert exactly the inclusive in-range records are returned ascending
    - **Validates: Requirements 4.4**

  - [ ]* 3.5 Write property test for complete ascending paginated history
    - **Feature: vehicle-diagnostic-assistant, Property 15: Session history returns the complete ascending set, paginated at 50 per page**
    - Insert records in shuffled order; assert pages of <=50 concatenate to the complete ascending set once, with no omissions or duplicates
    - **Validates: Requirements 5.1, 5.5**

- [x] 4. Checkpoint - core models, utilities, and repository
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement the Conversation_Manager over the repository
  - [x] 5.1 Implement session lifecycle and context assembly in `diagnostics/services/`
    - `ensure_session(customerId, sessionId?) -> SessionHandle | Error(SESSION_CLOSED) | Error(LIMIT_REACHED)`: create a unique (UUID v4) session when none is active before processing
    - `get_context(sessionId) -> ordered list` ascending by `createdAt`; `append_interaction(sessionId, record)` maintaining ascending order
    - Enforce the 50-interaction limit; evaluate 30-minute idle close against `lastSubmissionAt` (authoritative) with DynamoDB TTL for eventual cleanup via `is_active(sessionId, now)`
    - Delegate follow-up validation to the shared validator, preserving context unchanged on rejection
    - _Requirements: 1.4, 3.1, 3.2, 3.3, 3.4, 3.5_

  - [ ]* 5.2 Write property test for on-demand unique session creation
    - **Feature: vehicle-diagnostic-assistant, Property 2: Session identifiers are created on demand and are unique**
    - Assert a new unique session exists before processing when none is active
    - **Validates: Requirements 1.4**

  - [ ]* 5.3 Write property test for ascending context ordering
    - **Feature: vehicle-diagnostic-assistant, Property 6: Session records are maintained and provided as context in ascending chronological order**
    - Append records in shuffled order; assert maintained sequence and AI context are exactly all records ascending by timestamp
    - **Validates: Requirements 3.1, 3.3**

  - [ ]* 5.4 Write property test for the 50-interaction limit
    - **Feature: vehicle-diagnostic-assistant, Property 7: A Session never exceeds 50 interactions**
    - Fill a session to 50; assert the 51st follow-up is rejected with `LIMIT_REACHED` and count never exceeds 50
    - **Validates: Requirements 3.4**

  - [ ]* 5.5 Write property test for the 30-minute idle close (fake clock)
    - **Feature: vehicle-diagnostic-assistant, Property 8: A Session is closed exactly when idle for 30 minutes**
    - Use a fake clock straddling the 30-minute boundary; assert closed iff idle >= 30 min and closed-session follow-ups are rejected with `SESSION_CLOSED`
    - **Validates: Requirements 3.5**

  - [ ]* 5.6 Write unit tests for session boundary examples
    - Assert the 50th interaction is accepted and the 51st rejected
    - _Requirements: 3.4_

- [x] 6. Implement the AI_Engine Bedrock adapter behind a provider-agnostic interface
  - [x] 6.1 Define reusable prompt templates in `diagnostics/prompts/`
    - Store templates separately from business logic; sanitize user input and insert via template placeholders — never raw-concatenate; validate the prompt payload before use
    - _Requirements: 2.1_

  - [x] 6.2 Implement the `AiEngine` interface and `BedrockAiEngine` adapter in `diagnostics/services/`
    - Define `generate(symptomText, context) -> DiagnosticResult | InsufficientInfo | Timeout | AiError`; business logic depends on this interface, not Boto3
    - `BedrockAiEngine` uses Boto3 `bedrock-runtime` with approved models (Nova/Llama), attaching Bedrock Guardrails on every invocation
    - Parse output into `{ probableIssue, repairRecommendation, estimatedCost, confidenceScore }`; never return raw model text; emit the insufficient-info shape when no issue can be determined
    - Enforce a 30-second generation timeout; apply exponential backoff with jitter and a circuit breaker on the Bedrock dependency
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6_

  - [ ]* 6.3 Write property test for exactly-one issue and recommendation surfaced
    - **Feature: vehicle-diagnostic-assistant, Property 4: Successful diagnoses carry exactly one issue and one recommendation, surfaced to the Customer**
    - With a faked Bedrock adapter, assert successful results carry exactly one issue and one recommendation returned unchanged
    - **Validates: Requirements 2.1, 2.2, 2.3**

  - [ ]* 6.4 Write property test for insufficient-info results
    - **Feature: vehicle-diagnostic-assistant, Property 5: Insufficient-information results omit the issue and signal that more detail is needed**
    - Assert the returned result omits a Probable_Issue and includes a Customer-observable needs-more-detail indication
    - **Validates: Requirements 2.5**

  - [ ]* 6.5 Write unit tests for prompt templating and no-raw-concat
    - Assert user input is sanitized and inserted via template (never raw-concatenated) and raw model text is never surfaced
    - _Requirements: 2.1, 2.2_

- [x] 7. Implement escalation logic in the service layer
  - [x] 7.1 Implement escalation decision in `diagnostics/services/`
    - Set `escalated: true` when `confidenceScore < 0.70`, when Guardrails flag an unsafe recommendation, or when the diagnosis is ambiguous
    - _Requirements: 2.3_

  - [ ]* 7.2 Write unit tests for escalation branches
    - Assert confidence just below 0.70, a Guardrail-flagged unsafe recommendation, and an ambiguous diagnosis each produce `escalated: true`
    - _Requirements: 2.3_

- [x] 8. Checkpoint - conversation, AI engine, and escalation
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement the asynchronous Interaction_Store write path
  - [x] 9.1 Implement event emission and the persistence Lambda in `diagnostics/events/`
    - Define the `DiagnosisCompleted` event (Interaction_Record draft) emitted to EventBridge; wire EventBridge -> SQS -> a dedicated persistence Lambda that writes via the repository
    - Retry a failed write once within 5 seconds; on repeated failure route the message to a DLQ and log the failure with `sessionId` and timestamp; never block or alter the customer response; write no partial record
    - _Requirements: 4.1, 4.2, 4.3_

  - [ ]* 9.2 Write property test for failed-persistence integrity
    - **Feature: vehicle-diagnostic-assistant, Property 11: A failed persistence leaves no partial record and reports the failure**
    - Force persistence failure; assert no partial record remains, an error is reported (DLQ routing), and the failure is logged with sessionId and timestamp
    - **Validates: Requirements 4.3**

- [x] 10. Implement authorized read side of the Interaction_Store
  - [x] 10.1 Implement authorized retrieval in `diagnostics/services/`
    - `query_by_date_range(principal, start, end)`: analyst RBAC check first; reject inverted ranges with `INVALID_RANGE`; return ascending in-range records
    - `get_session_history(principal, sessionId, pageToken?)`: Customer owner-only check; `NOT_FOUND` for unknown sessions; empty set for known-but-empty sessions; paginate at 50
    - Fail closed on authorization: check before reading any records; return zero records on denial
    - _Requirements: 4.4, 4.5, 4.6, 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ]* 10.2 Write property test for inverted date-range rejection
    - **Feature: vehicle-diagnostic-assistant, Property 13: Inverted date ranges are rejected**
    - Assert start > end is rejected with `INVALID_RANGE` and returns no records
    - **Validates: Requirements 4.6**

  - [ ]* 10.3 Write property test for unauthorized access returning no records
    - **Feature: vehicle-diagnostic-assistant, Property 14: Unauthorized access returns access-denied with no records**
    - Assert an analyst lacking the RBAC claim and a Customer requesting a non-owned session each get access-denied and zero records, regardless of underlying data
    - **Validates: Requirements 4.5, 5.4**

  - [ ]* 10.4 Write unit tests for empty-but-known and unknown sessions
    - Assert a known session with no records returns an empty set (not an error, Req 5.2) and an unknown session returns `NOT_FOUND`/404 (Req 5.3)
    - _Requirements: 5.2, 5.3_

- [x] 11. Implement the diagnostics service orchestration
  - [x] 11.1 Implement `analyze(...)` orchestration in `diagnostics/services/`
    - Fail-fast validation via the shared validator before any Bedrock/DynamoDB call; surface `EMPTY`/`TOO_LONG`
    - Resolve the session via Conversation_Manager, surfacing `SESSION_CLOSED`/`LIMIT_REACHED`
    - Call the AI_Engine interface with ascending context; run escalation; emit `DiagnosisCompleted` for non-blocking persistence; append the interaction; return the structured diagnosis (or insufficient-info / `AI_TIMEOUT` / `AI_ERROR`)
    - _Requirements: 1.1, 1.4, 2.3, 2.5, 2.6, 4.3_

  - [ ]* 11.2 Write property test for valid-symptom acceptance and association
    - **Feature: vehicle-diagnostic-assistant, Property 1: Valid symptoms are accepted and associated with a session**
    - Generate valid 1..2000-char symptoms; assert acceptance, association with the active session, and an acknowledgment
    - **Validates: Requirements 1.1**

  - [ ]* 11.3 Write property test for invalid-symptom rejection with no AI invocation (service side)
    - **Feature: vehicle-diagnostic-assistant, Property 3: Invalid symptoms are rejected everywhere with no side effects**
    - Assert invalid first/follow-up symptoms are rejected, no result generated, Bedrock not invoked, session/context preserved, no record created
    - **Validates: Requirements 1.2, 1.3, 2.4, 3.2**

  - [ ]* 11.4 Write unit tests for AI timeout and response/log shapes
    - Inject a fake Bedrock adapter exceeding 30s (fake clock) and assert a retry-prompting 503 (Req 2.6); assert 422/503 response shapes (`code`, `suggestion`, `timestamp`)
    - _Requirements: 2.6_

- [x] 12. Checkpoint - store and service orchestration
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Implement thin Lambda handlers and API Gateway endpoints
  - [x] 13.1 Implement thin handlers in `handlers/`
    - `POST /diagnostics/analyze` (Customer JWT), `GET /diagnostics/history` (owner-only, paginated), `/repair-estimates` (analyst RBAC)
    - Validate Cognito JWT (authorizer), parse the request, attach `correlationId`, delegate to the service layer (no orchestration in the handler)
    - Map typed outcomes to HTTP codes 200/422/503/400/403/404 with `{ code, suggestion, timestamp }`; never leak internal/provider errors or stack traces
    - _Requirements: 1.1, 1.5, 2.3, 4.4, 4.5, 4.6, 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ]* 13.2 Write integration tests for API Gateway + Cognito and end-to-end flow
    - API Gateway + Cognito JWT accept/reject and Customer-vs-analyst RBAC; Bedrock `InvokeModel` + Guardrails parsing into the 4-field shape; DynamoDB write/read round-trip, ordering, 50-record pagination, GSI date-range, SSE; EventBridge->SQS->persistence Lambda retry + DLQ not affecting response; timing budgets (5s acceptance Req 1.5, 2s persistence Req 4.1, 2s history Req 5.1); end-to-end submit->generate->emit->persist->retrieve on dev
    - _Requirements: 1.1, 1.5, 2.1, 4.1, 4.4, 5.1, 5.5_

- [x] 14. Implement CloudFormation infrastructure as code
  - [x] 14.1 Author CloudFormation templates in `infrastructure/cloudformation/` and `backend/template.yaml`
    - Define API Gateway (REST, OpenAPI), Lambdas, Cognito user pool + JWT authorizer, DynamoDB `FleetMateDiagnostics` table + GSI1 with SSE and TTL, EventBridge bus/rule, SQS + DLQ, Bedrock Guardrail config references, least-privilege IAM roles per Lambda, Secrets Manager, X-Ray tracing
    - Add CloudWatch alarms for Lambda error rate, API latency, Bedrock response latency, token-usage spikes, and confidence-score degradation
    - Parameterize for isolated `dev`/`qa`/`prod` under `infrastructure/environments/`; configure blue/green or canary deployment with automatic rollback on failed health checks / elevated error rate / critical alarms
    - _Requirements: 1.5, 4.1, 5.1_

- [x] 15. Final checkpoint - full workflow verification
  - Ensure all tests pass, ask the user if questions arise.

## Web UI Tasks (Requirements 6–12)

These tasks implement the browser tier added to design.md ("Web User Interface" sections). They are additive: the existing backend, Cognito user pool, and `/v1` REST API are reused unchanged. The SPA is TypeScript React under the existing `frontend/` directory, grouped per structure.md (`frontend/src/{auth,services,pages,components,config,utils}`). Property-based tests use **fast-check** (minimum 100 iterations per property), each placed next to the code it validates, tagged `Feature: vehicle-diagnostic-assistant, Property {number}: {property_text}` and annotated with the requirement it checks; example/component tests use Vitest + React Testing Library. Network (token exchange, Diagnostic_API calls), browser redirect/location, `crypto`/storage, and timers are faked; a controllable clock is used for token-expiry logic. All test sub-tasks are optional (marked `*`); core implementation tasks are never optional.

- [x] 16. Scaffold the React/TypeScript SPA and runtime config loader
  - [x] 16.1 Scaffold the SPA project and structure under `frontend/`
    - Initialize a TypeScript React single-page application with a build tool (e.g., Vite) and `frontend/package.json`; enable TypeScript strict mode
    - Create the structure per structure.md: `frontend/src/{auth,services,pages,components,config,utils}` and `frontend/public/`
    - Set up the test framework: Vitest + React Testing Library for example/component tests and **fast-check** for property-based tests
    - _Requirements: 11.1_

  - [x] 16.2 Implement the runtime config loader in `config/runtimeConfig.ts`
    - Load the generated, non-secret `config.json` (or `window.__FLEETMATE_CONFIG__`) exposing `apiBaseUrl` (`https://<api-id>.execute-api.us-east-1.amazonaws.com/v1`), `cognitoDomain`, `userPoolId`, `appClientId`, `redirectUri`, `logoutUri`
    - Define the `RuntimeConfig` interface; embed no secrets in the client
    - _Requirements: 6.6, 12.1, 12.2_

  - [ ]* 16.3 Write unit test for runtime config loading
    - Assert config.json populates the API/Cognito identifiers used by the auth and API clients and that no secret values are present
    - _Requirements: 6.6, 12.2_

- [x] 17. Implement the auth module (Hosted UI, PKCE, token lifecycle, guard)
  - [x] 17.1 Implement PKCE helpers in `auth/pkce.ts`
    - `generateVerifier()` (high-entropy random) and `deriveChallenge(verifier)` returning `base64url(SHA256(verifier))`; fake `crypto` in tests
    - _Requirements: 6.2_

  - [x] 17.2 Implement the token store and validity predicate in `auth/tokenStore.ts`
    - Hold ID_Token/Access_Token in memory and/or `sessionStorage` (session-scoped), discarded when the browser session ends
    - `isTokenValid(expiryEpoch, nowEpoch) -> bool` (valid iff `expiryEpoch > nowEpoch`); `clearTokens()` leaving the store empty even if the underlying discard reports failure
    - _Requirements: 10.4, 10.5, 10.6_

  - [x] 17.3 Implement the auth client (login, callback, exchange, logout) in `auth/authClient.ts`
    - `buildLoginUrl(config, state, challenge)` targeting the Hosted UI authorize endpoint (code flow + PKCE)
    - `handleCallback(params, pendingVerifier) -> CodeReady | AuthError`: `?code` → CodeReady (verifier matches pending), `?error` → AuthError with retry and no token stored
    - `exchangeCode(code, verifier, config) -> Tokens | ExchangeFailed` within a 10-second budget; discard any partial token on failure/timeout and offer retry
    - `logout(config)` discarding tokens and returning the Hosted UI logout redirect URL
    - _Requirements: 6.2, 6.4, 6.7, 10.2, 10.3_

  - [x] 17.4 Implement the route guard in `auth/routeGuard.ts`
    - `requireAuth(authState, route) -> ALLOW | REDIRECT_TO_HOSTED_UI`: ALLOW iff a valid (unexpired) ID_Token is held, otherwise redirect to the Hosted UI; handle token-expiry → discard + re-auth
    - _Requirements: 6.1, 6.5, 10.5_

  - [ ]* 17.5 Write property test for the route guard
    - **Feature: vehicle-diagnostic-assistant, Property 16: Protected views require a valid ID token**
    - Generate auth states (token present/absent/expired) and protected routes; assert ALLOW iff a valid ID_Token is held, else REDIRECT_TO_HOSTED_UI
    - **Validates: Requirements 6.1, 6.5**

  - [ ]* 17.6 Write property test for PKCE challenge derivation
    - **Feature: vehicle-diagnostic-assistant, Property 17: PKCE challenge derives from the verifier used at exchange**
    - Generate random verifiers; assert the challenge equals `base64url(SHA256(verifier))` and the verifier submitted at exchange equals the stored pending verifier
    - **Validates: Requirements 6.2**

  - [ ]* 17.7 Write property test for callback outcomes never leaving a partial token
    - **Feature: vehicle-diagnostic-assistant, Property 18: Authorization callback outcomes never leave a partial token**
    - Generate callback parameter sets (code / error / malformed) and exchange outcomes; assert tokens are held only on full exchange success, otherwise no token remains and retry is exposed
    - **Validates: Requirements 6.4, 6.7**

  - [ ]* 17.8 Write property test for token validity decided solely by expiry vs now
    - **Feature: vehicle-diagnostic-assistant, Property 20: Token validity is decided solely by expiry versus current time**
    - Use a controllable clock with expiry/now pairs straddling equality; assert valid iff `expiry > now`, else both tokens discarded and re-authentication chosen
    - **Validates: Requirements 10.5**

- [x] 18. Implement the API client service
  - [x] 18.1 Implement the API client in `services/apiClient.ts`
    - `analyze(symptomText, sessionId?)` → `POST /diagnostics/analyze` with body field `symptomText`; `history(sessionId, pageToken?)` → `GET /diagnostics/history`
    - Attach `Authorization: Bearer <Access_Token>` on every request; map responses to UI outcomes (200 diagnosis / 200 needs-more-detail / 422 / 503 / 401 / 403 / 404) without surfacing internal or provider error detail
    - On 401, discard ID+Access tokens and signal re-authentication; enforce request timeouts (30s analyze, 10s history)
    - _Requirements: 6.3, 8.2, 9.1, 10.1_

  - [ ]* 18.2 Write property test for authenticated requests carrying the bearer token to the correct endpoint
    - **Feature: vehicle-diagnostic-assistant, Property 19: Authenticated requests carry the bearer token to the correct endpoint**
    - Generate tokens and operation kinds; assert `analyze`/`history` target the matching endpoint+method and include the Access_Token as `Authorization: Bearer`
    - **Validates: Requirements 6.3, 8.2, 9.1**

  - [ ]* 18.3 Write property test for 401 discarding tokens and triggering re-authentication
    - **Feature: vehicle-diagnostic-assistant, Property 21: A 401 response discards tokens and triggers re-authentication**
    - Generate prior auth states; assert handling a 401 empties the token store and chooses redirect to the Hosted UI
    - **Validates: Requirements 10.1**

  - [ ]* 18.4 Write property test for the token-clearing invariant after logout/clear
    - **Feature: vehicle-diagnostic-assistant, Property 22: After logout or any token clear, no request carries a token**
    - Generate prior auth states (including a discard that reports failure); assert the store is empty, the unauthenticated view is selected, and every subsequently built request omits the Authorization header
    - **Validates: Requirements 10.2, 10.3, 10.6**

- [x] 19. Checkpoint - config, auth, and API client
  - Ensure all tests pass, ask the user if questions arise.

- [x] 20. Implement the login/landing and callback pages
  - [x] 20.1 Implement `pages/LoginLandingPage.tsx` and `pages/CallbackPage.tsx`
    - Landing page: unauthenticated view with a "Log in / Sign up" control that redirects to the Hosted UI via `buildLoginUrl(...)` (self-service sign-up + email verification occur in the Hosted UI)
    - Callback page: on `?code` validate the PKCE verifier and exchange for tokens; on `?error` show a login-did-not-complete message with retry and store no token; on exchange failure/timeout discard any partial token and offer retry
    - _Requirements: 6.1, 6.4, 6.5, 6.7, 7.1, 7.7_

  - [ ]* 20.2 Write unit test for logout clearing tokens
    - Assert the logout action clears the token store and selects the Hosted UI logout redirect URL
    - _Requirements: 10.2, 10.3_

- [x] 21. Implement the submit-symptom and diagnosis-result views
  - [x] 21.1 Implement `pages/SubmitSymptomPage.tsx` with input gating and in-flight guard
    - Present the symptom input and submit control while a valid Access_Token is held
    - `isSubmitEnabled(text)` pure gate: enabled iff 1–2000 characters with at least one non-whitespace char
    - On submit, call `analyze(...)`, show an in-progress indicator, and block duplicate submission of the same text while a request is in flight
    - If no response within 30 seconds, stop the indicator, show a did-not-complete message with retry, and retain the submitted text
    - _Requirements: 8.1, 8.2, 8.7, 8.8_

  - [x] 21.2 Implement `pages/DiagnosisResultView.tsx`
    - On `Diagnosis`, render Probable_Issue, Repair_Recommendation, estimated cost, and confidence score
    - On `NeedsMoreDetail`, show the needs-more-detail message and retain the symptom for editing
    - On `Rejected` (422), show the returned suggestion and retain the symptom; on `Unavailable` (503), show the temporarily-unavailable message with a retry control
    - _Requirements: 8.3, 8.4, 8.5, 8.6_

  - [ ]* 21.3 Write property test for submit enabled exactly for valid input
    - **Feature: vehicle-diagnostic-assistant, Property 23: Submit is enabled exactly for valid symptom input**
    - Generate input strings including whitespace-only and boundary lengths (1, 2000, 2001); assert enabled iff 1–2000 chars with a non-whitespace char
    - **Validates: Requirements 8.1**

  - [ ]* 21.4 Write property test for the in-flight duplicate-submission guard
    - **Feature: vehicle-diagnostic-assistant, Property 24: An in-flight submission blocks duplicate submission of the same text**
    - Generate in-flight/same-text scenarios; assert re-submitting the same text is prevented and the in-progress indication is shown while pending
    - **Validates: Requirements 8.7**

  - [ ]* 21.5 Write property test for response→UI-state mapping with retention
    - **Feature: vehicle-diagnostic-assistant, Property 25: API responses map to the correct UI state with input and record retention**
    - Generate the full set of response codes/shapes (200 diagnosis, 200 needs-more-detail, 422, 503, 403-history); assert each maps to the correct UI state with input/record retention and no internal/provider detail surfaced
    - **Validates: Requirements 8.3, 8.4, 8.5, 8.6, 9.4**

  - [ ]* 21.6 Write component test for the 503 and success renders
    - Assert a 503 renders the temporarily-unavailable message with a retry control; assert the result view renders all four fields for a representative success result
    - _Requirements: 8.3, 8.6_

- [x] 22. Implement the session-history view
  - [x] 22.1 Implement `pages/SessionHistoryPage.tsx`
    - On open, request `GET /diagnostics/history` with the Customer Session identifier
    - Render records ascending by `createdAt`, tie-broken by `recordId`; show a no-past-interactions message for an empty set
    - On 403, show a not-authorized message and retain previously displayed records
    - On service/network error or no response within 10 seconds, show a could-not-retrieve message with a retry control capped at 3 attempts
    - When the response carries a `nextPageToken`, present a next-page control requesting up to 50 further records
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [ ]* 22.2 Write property test for history ascending order with tie-break
    - **Feature: vehicle-diagnostic-assistant, Property 26: History renders ascending by timestamp, tie-broken by record identifier**
    - Generate record sets with duplicated `createdAt` values; assert rendering ascending by `createdAt`, ties ordered by `recordId`
    - **Validates: Requirements 9.2**

  - [ ]* 22.3 Write property test for next-page control and retry cap
    - **Feature: vehicle-diagnostic-assistant, Property 27: Next-page control appears exactly when more records remain, and history retries cap at three**
    - Generate responses with/without `nextPageToken` and failure sequences of length 0..5; assert the next-page control appears iff a token is present and retries are capped at 3 attempts
    - **Validates: Requirements 9.5, 9.6**

  - [ ]* 22.4 Write component test for the empty-history message
    - Assert an empty history set renders the no-past-interactions message
    - _Requirements: 9.3_

- [x] 23. Checkpoint - web pages and client logic
  - Ensure all tests pass, ask the user if questions arise.

- [x] 24. Add web-tier infrastructure as code (S3 + CloudFront + Cognito Hosted UI)
  - [x] 24.1 Add private S3 + CloudFront (OAC, SPA fallback, HTTPS redirect) to the stack
    - In `backend/template.yaml` and/or `infrastructure/cloudformation/`, add a private S3 bucket (BlockPublicAccess all true, SSE enabled, no website hosting) for the SPA assets
    - Add a CloudFront distribution using the default `*.cloudfront.net` domain (no aliases/ACM), `DefaultRootObject: index.html`, Origin Access Control so the bucket is reachable only via CloudFront, `ViewerProtocolPolicy: redirect-to-https`, and custom error responses mapping 403→`/index.html` (200) and 404→`/index.html` (200) for SPA routing
    - Add an `AWS::CloudFront::OriginAccessControl` and a bucket policy granting read only to the distribution
    - Preserve `dev`/`qa`/`prod` isolation under `infrastructure/environments/`; add no Route 53 record, no custom DNS, and no custom TLS certificate
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 12.1, 12.3_

  - [x] 24.2 Add the Cognito UserPoolDomain and re-enable OAuth on the existing App_Client
    - Add `AWS::Cognito::UserPoolDomain` on the existing user pool → `*.auth.us-east-1.amazoncognito.com` Hosted UI
    - Update the existing `AWS::Cognito::UserPoolClient`: `AllowedOAuthFlows: [code]`, `AllowedOAuthFlowsUserPoolClient: true`, `AllowedOAuthScopes: [openid, email, profile]`, `SupportedIdentityProviders: [COGNITO]`, no client secret; set `CallbackURLs`/`LogoutURLs` to the CloudFront HTTPS origin (depends on the distribution so CloudFormation creates it first)
    - _Requirements: 6.6, 12.2, 12.4_

  - [x] 24.3 Add CORS for the CloudFront origin on the browser-called API methods
    - On `POST /diagnostics/analyze` and `GET /diagnostics/history`, add CORS: `Access-Control-Allow-Origin` = the CloudFront HTTPS origin, `Access-Control-Allow-Headers: Authorization,Content-Type`, `Access-Control-Allow-Methods: POST,GET,OPTIONS`, and handle preflight `OPTIONS`
    - _Requirements: 8.2, 9.1_

- [x] 25. Implement deploy-time wiring and SPA config generation
  - [x] 25.1 Generate the SPA runtime config and wire deferred CloudFront URLs
    - Add a deploy-time step that, after the distribution and app client resolve, writes `frontend/public/config.json` (or equivalent) from CloudFormation stack outputs: API base URL (`/v1`), Cognito domain, user pool id, app client id, and CloudFront redirect/logout URIs (no secrets)
    - Register the CloudFront callback + logout URLs on the App_Client (the deferred chicken-and-egg step), using the default CloudFront domain over HTTPS; document the two-phase `update-stack` fallback if the domain must be fed back as a parameter
    - _Requirements: 6.6, 12.4_

  - [x] 25.2 Build and upload SPA assets to S3 and invalidate the CloudFront cache
    - Add a deploy-time step that builds the SPA, uploads the compiled assets plus `config.json` to the private S3 bucket, and creates a CloudFront cache invalidation so the latest assets are served
    - _Requirements: 11.1, 11.2_

- [x] 26. Integrate SPA routing and wire the pages together
  - [x] 26.1 Wire the SPA entry, router, and route guard in `frontend/src`
    - Implement the application entry and client-side router connecting the landing/callback, submit-symptom, diagnosis-result, and session-history pages
    - Apply `requireAuth` on the protected routes (submit and history), initialize the runtime config loader at startup, and wire the API client and auth module into the pages so no component is orphaned
    - _Requirements: 6.1, 6.5, 8.1, 9.1, 11.3_

  - [ ]* 26.2 Write property test for SPA route fallback
    - **Feature: vehicle-diagnostic-assistant, Property 28: Unmatched SPA routes fall back to the entry document**
    - Generate arbitrary request paths (asset vs navigational); assert navigational/unknown paths resolve to `index.html` and stored-asset paths resolve to the asset
    - **Validates: Requirements 11.3**

  - [ ]* 26.3 Write web integration/manual verification for the end-to-end flow
    - Hosted UI authorization-code + PKCE login and self-service sign-up with email verification (largely manual) within the 10s exchange budget (Req 6.1, 6.2, 6.7, 7.1–7.7); CloudFormation assertions that the UserPoolClient has `AllowedOAuthFlows=code`, scopes `openid/email/profile`, `SupportedIdentityProviders=COGNITO`, and CloudFront callback/logout URLs, and that a UserPoolDomain exists (Req 6.6, 12.2, 12.4)
    - CloudFront/S3/OAC delivery over HTTPS from the private origin, direct-to-origin denial, HTTP→HTTPS redirect, and SPA deep-route fallback (Req 11.1, 11.2, 11.4, 11.5, 12.1, 11.3); CORS preflight+actual browser calls to the existing API (Req 8.2, 9.1); no-custom-DNS/no-cert deployment (Req 12.3); end-to-end login → submit symptom → see diagnosis → view history
    - _Requirements: 6.2, 7.1, 11.2, 11.4, 12.3_

- [x] 27. Final checkpoint - web UI feature verification
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional (all property, example/unit, and integration tests) and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each task references specific requirements for traceability; each property test references its design property number and the requirement it validates.
- Property-based tests use Hypothesis with a minimum of 100 iterations each; Bedrock and DynamoDB are mocked/faked, and a fake clock is used for Properties 8 and 9.
- Handlers stay thin; orchestration lives in the service layer. Persistence is event-driven and never blocks the customer response.
- Checkpoints ensure incremental validation at natural boundaries.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "2.2", "3.1"] },
    { "id": 2, "tasks": ["2.3", "2.4", "3.2", "3.3", "3.4", "3.5", "5.1", "6.1"] },
    { "id": 3, "tasks": ["5.2", "5.3", "5.4", "5.5", "5.6", "6.2", "9.1", "10.1"] },
    { "id": 4, "tasks": ["6.3", "6.4", "6.5", "7.1", "9.2", "10.2", "10.3", "10.4"] },
    { "id": 5, "tasks": ["7.2", "11.1"] },
    { "id": 6, "tasks": ["11.2", "11.3", "11.4", "13.1"] },
    { "id": 7, "tasks": ["13.2", "14.1"] }
  ]
}
```

## Task Dependency Graph (Web UI Tasks 16–27)

The web UI tasks form their own dependency graph; wave ids continue after the backend graph (starting at 8). Setup (scaffold, runtime config) comes first; auth and API client build on config; pages build on auth + API client; IaC and deploy-time wiring come after the client exists; SPA routing wires everything together; tests follow the code they validate.

```json
{
  "waves": [
    { "id": 8, "tasks": ["16.1"] },
    { "id": 9, "tasks": ["16.2", "16.3"] },
    { "id": 10, "tasks": ["17.1", "17.2"] },
    { "id": 11, "tasks": ["17.3", "17.4"] },
    { "id": 12, "tasks": ["17.5", "17.6", "17.7", "17.8", "18.1"] },
    { "id": 13, "tasks": ["18.2", "18.3", "18.4", "20.1", "21.1", "22.1", "24.1"] },
    { "id": 14, "tasks": ["20.2", "21.2", "21.3", "21.4", "22.2", "22.3", "24.2", "24.3"] },
    { "id": 15, "tasks": ["21.5", "21.6", "22.4", "25.1"] },
    { "id": 16, "tasks": ["25.2", "26.1"] },
    { "id": 17, "tasks": ["26.2", "26.3"] }
  ]
}
```

## Most-Recent Prior Interaction Tasks (Requirement 13)

These tasks are additive and extend the existing backend (Python 3.12 under `backend/src`), the React/TypeScript SPA (`frontend/src`), and the SAM stack (`backend/template.yaml`) to implement Requirement 13 (criteria 13.1–13.10) from design.md ("Most-Recent Prior Interaction (Requirement 13)", Data Models → Prior-Interaction Access Pattern / GSI2, the Web_Client prior-interaction panel, and the prior-interaction IaC notes). They reuse the existing Cognito user pool, `/v1` REST API, repository Boto3 seam, shared `_http` handler helpers, typed outcomes, and the SPA auth/API-client modules unchanged except where noted. Backend property-based tests use **Hypothesis** (minimum 100 iterations each) against a faked DynamoDB client, tagged `Feature: vehicle-diagnostic-assistant, Property {number}: {property_text}` and annotated with the requirement they validate (design Correctness Properties 29–32). All test sub-tasks are optional (marked `*`); core implementation tasks are never optional.

**FORWARD-ONLY decision (user-confirmed): NO backfill/migration.** New Interaction_Records begin carrying `customerId` and the GSI2 key attributes (`GSI2PK=CUSTOMER#{customerId}`, `GSI2SK={createdAt}#{recordId}`) going forward only. There is deliberately **no task to backfill or migrate existing DynamoDB records** — interactions created before this change simply will not appear as prior interactions (the panel shows "no previous interaction" for them), and that is the accepted behavior.

- [x] 28. Extend the repository write path and record model to carry `customerId` and GSI2 keys (forward-only)
  - [x] 28.1 Add `customerId` to `InteractionRecord` and write the GSI2 key attributes on new records
    - Add a `customerId` field to the `InteractionRecord` model in `diagnostics/models/` (the owning Customer's Cognito `sub`)
    - In the `FleetMateDiagnostics` repository (`diagnostics/repositories/`), update `append_record` so every newly persisted InteractionRecord item also writes `customerId`, `GSI2PK=CUSTOMER#{customerId}`, and `GSI2SK={createdAt}#{recordId}` — forward-only; do NOT touch or rewrite existing records
    - Keep Boto3 isolated behind the repository; cover the new attributes with the faked DynamoDB client used by existing repository tests
    - _Requirements: 13.1, 13.3, 13.4_

  - [x] 28.2 Thread `customerId` through the write path from the Session to the persisted record
    - Ensure the `DiagnosisCompleted` event (`diagnostics/events/`) carries `customerId` (the Session already holds it from the analyze orchestration), and the persistence Lambda passes it to `append_record` so new records are owner-attributed
    - Update the analyze orchestration / event emission in `diagnostics/services/` to populate `customerId` on the Interaction_Record draft from the authenticated principal already resolved for the session
    - _Requirements: 13.1, 13.4_

  - [x] 28.3 Add the GSI2 newest-first query method to the repository
    - Implement `query_most_recent_prior(customerId, currentSessionId, limit=2) -> InteractionRecord | None` querying `GSI2PK=CUSTOMER#{customerId}` with `ScanIndexForward=false` and a small `Limit` (e.g. 2), skipping any returned record whose `sessionId == currentSessionId`, returning the first remaining record or `None`
    - _Requirements: 13.1, 13.3, 13.5_

  - [x]* 28.4 Write property test for prior-interaction selection (repository/service selection logic)
    - **Feature: vehicle-diagnostic-assistant, Property 29: Prior-interaction selection excludes the current session and picks the latest**
    - Generate owner record sets (including duplicate `createdAt` for the `recordId` tie-break) with records in and out of a chosen `currentSessionId`; assert the resolved record is the latest by `(createdAt, recordId)` among records whose `sessionId` differs from `currentSessionId`, and that a current-session record is never returned
    - **Validates: Requirements 13.1, 13.3**

- [x] 29. Implement the owner-scoped service read method on `InteractionStore`
  - [x] 29.1 Implement `get_most_recent_prior_interaction` in `diagnostics/services/interaction_store.py`
    - Add `get_most_recent_prior_interaction(customerId, currentSessionId) -> InteractionRecord | None`: enforce owner-scoping from the authenticated `customerId` (fail closed) **before any read**, require a non-blank `currentSessionId`, delegate to `query_most_recent_prior`, and return `None` when the Customer owns no record outside `currentSessionId`
    - Return typed outcomes consistent with the existing read side; keep Boto3 out of the service layer
    - _Requirements: 13.1, 13.3, 13.4, 13.5, 13.6_

  - [x]* 29.2 Write property test for owner-scoped resolution
    - **Feature: vehicle-diagnostic-assistant, Property 30: Prior-interaction resolution is owner-scoped and never leaks another Customer's record**
    - Generate mixed-owner data sets (including cases where another Customer has the newest record overall); assert resolution returns only a record owned by the authenticated Customer_Identity or an empty result, never another Customer's record
    - **Validates: Requirements 13.4**

  - [x]* 29.3 Write property test for the empty-result case
    - **Feature: vehicle-diagnostic-assistant, Property 31: An empty result is returned when no record exists outside the current session**
    - Generate owner record sets where every record's `sessionId` equals `currentSessionId` (including the zero-record case); assert the service returns an empty result containing no Interaction_Record
    - **Validates: Requirements 13.5**

- [x] 30. Implement the thin `GET /diagnostics/previous` handler
  - [x] 30.1 Implement `handlers/previous.py`
    - Thin handler for `GET /diagnostics/previous?currentSessionId=...`: resolve `customerId` exclusively from the validated token `sub` claim (never from query/header/body), attach a `correlationId`, and reuse the shared `_http` helpers (CORS echo of `WEB_APP_ORIGIN`, `safe_handler`, structured logging)
    - Require `currentSessionId`; when omitted, empty, or whitespace-only, return `400 { code: "MISSING_CURRENT_SESSION", suggestion, timestamp }` before any read
    - Delegate to `get_most_recent_prior_interaction` and return `{ record: InteractionRecord | null, timestamp }` using the same `InteractionRecord` shape as history (`{ record: null }` when no prior interaction resolves); never leak internal/provider detail
    - _Requirements: 13.1, 13.2, 13.5, 13.6_

  - [ ]* 30.2 Write property test for current-session-identifier rejection
    - **Feature: vehicle-diagnostic-assistant, Property 32: A request without a current-session identifier is rejected before any read**
    - Generate requests whose `currentSessionId` is omitted, empty, or whitespace-only; assert the request is rejected with a current-session-required error and no read of the Interaction_Store occurs
    - **Validates: Requirements 13.6**

  - [x]* 30.3 Write unit tests for the handler response shape and owner resolution
    - Assert `customerId` is taken only from the token `sub` (a client-supplied id is ignored), a resolved record returns `{ record, timestamp }` in the history shape, no prior interaction returns `{ record: null, timestamp }`, and a missing `currentSessionId` returns `400` with `{ code, suggestion, timestamp }`
    - _Requirements: 13.2, 13.4, 13.5, 13.6_

- [x] 31. Checkpoint - prior-interaction backend read path
  - Ensure all tests pass, ask the user if questions arise.

- [x] 32. Add the prior-interaction infrastructure to the SAM stack (forward-only)
  - [x] 32.1 Add GSI2 to the `FleetMateDiagnostics` table in `backend/template.yaml`
    - Add `GSI2PK` and `GSI2SK` to `AttributeDefinitions` and a new `GlobalSecondaryIndexes` entry (`GSI2PK` HASH, `GSI2SK` RANGE) projecting the attributes the panel renders
    - Note in the template/change description that this is an online `UpdateTable` on the live table and that, under the forward-only decision, existing records are not in GSI2 (only records written after this change are queryable)
    - Preserve `dev`/`qa`/`prod` isolation under `infrastructure/environments/` and the no-Route53/no-custom-cert constraint
    - _Requirements: 13.1, 13.3, 13.4_

  - [x] 32.2 Add the least-privilege `PreviousFunction` Lambda
    - Add a new `AWS::Serverless::Function` for `handlers/previous.py` with a least-privilege IAM policy granting `dynamodb:Query` on the GSI2 index ARN (`.../table/FleetMateDiagnostics/index/GSI2`) and `dynamodb:GetItem` on the base table only if a session lookup is needed — nothing broader
    - Set the `WEB_APP_ORIGIN` environment variable for the CORS echo and enable structured logging + X-Ray tracing, matching the existing API Lambdas
    - _Requirements: 13.1, 13.2, 13.4, 13.5, 13.6_

  - [x] 32.3 Add the `GET /diagnostics/previous` API method and its OPTIONS preflight
    - In the inline OpenAPI, add `GET /diagnostics/previous` wired to `PreviousFunction` under the **same Cognito ID-token authorizer** as `GET /diagnostics/history`, plus an unauthenticated `OPTIONS` MOCK preflight mirroring the existing per-resource preflights
    - Confirm the existing `AWS::ApiGateway::GatewayResponse` CORS configuration covers the new method/preflight (401/403 responses), and note that the API **stage must be force-redeployed** after the method change for the route to go live
    - _Requirements: 13.7, 13.9_

- [x] 33. Implement the SPA API-client `previous(...)` method and typed outcome
  - [x] 33.1 Add `previous(currentSessionId)` to `services/apiClient.ts`
    - Implement `previous(currentSessionId) -> PreviousOutcome` → `GET /diagnostics/previous?currentSessionId=...`, attaching `Authorization: Bearer <Access_Token>` and the CloudFront CORS origin like the other calls, with a 10-second request timeout
    - Define the typed `PreviousOutcome` union: `PriorRecord { record }` (200 record present), `NoPrior` (200 `{ record: null }`), `Unauthorized` (401 → discard tokens + re-auth via the shared 401 handling), and `PreviousError { message, retry: true }` (service/network/timeout)
    - Normalize the returned `InteractionRecord` the **same way history records are normalized** (flatten `symptomDescription` + nested `result` into the flat view-model with `probableIssue`, `repairRecommendation`, estimated cost, confidence score), reusing the shared normalization helper so both views render identical shapes
    - _Requirements: 13.2, 13.7, 13.8, 13.9, 13.10_

- [x] 34. Implement the `PriorInteractionPanel` page and wire it into the authenticated view
  - [x] 34.1 Implement `pages/PriorInteractionPanel.tsx`
    - On open (authenticated view), call `previous(currentSessionId)` using the persisted Current_Session identifier from `utils/sessionStore.ts`, showing a loading indicator while in flight; the panel is distinct from `SessionHistoryPage`
    - For `PriorRecord`, display the prior Symptom_Description and Diagnostic_Result using the shared normalized view-model; for `NoPrior`, show an empty-state message that no previous interaction exists
    - On `Unauthorized` (401), discard tokens and redirect to the Hosted_UI via the shared auth handling; on `PreviousError`, show a could-not-retrieve-previous-interaction message with a retry control that re-issues the request
    - _Requirements: 13.2, 13.7, 13.8, 13.9, 13.10_

  - [x] 34.2 Wire `PriorInteractionPanel` into `AuthenticatedApp.tsx`
    - Mount the panel in the authenticated composition alongside (but separate from) the history view, passing the persisted current `sessionId`, so the component is not orphaned
    - _Requirements: 13.7_

  - [x]* 34.3 Write component tests for the panel states
    - Assert the empty-state render for `NoPrior`, a populated render for `PriorRecord` (prior symptom + diagnosis fields), and that a 401 triggers token discard + re-auth; cover the `PreviousError` retry control
    - _Requirements: 13.8, 13.9, 13.10_

- [x] 35. Deploy the prior-interaction feature
  - [x] 35.1 Build and deploy the backend and force an API Gateway stage redeploy
    - Run `sam build` and `sam deploy` for the target environment to apply the GSI2 `UpdateTable`, the new `PreviousFunction`, and the new API method/preflight, then force the API Gateway stage redeploy so `GET /diagnostics/previous` goes live (describe as a task; the orchestrator executes it with the user)
    - _Requirements: 13.1, 13.7_

  - [x] 35.2 Build and publish the frontend
    - Build the SPA, upload the compiled assets to the private S3 bucket, and create a CloudFront cache invalidation so the new `PriorInteractionPanel` and `apiClient.previous` are served (describe as a task; the orchestrator executes it with the user)
    - _Requirements: 13.7_

- [x] 36. Final checkpoint - most-recent prior interaction feature verification
  - Ensure all tests pass, ask the user if questions arise.

## Task Dependency Graph (Most-Recent Prior Interaction Tasks 28–36)

The prior-interaction tasks form their own dependency graph; wave ids continue after the Web UI graph (which ends at wave 17), starting at **wave 18**. The repository/model write-path change comes first; the service read method builds on the repository query; the handler builds on the service; IaC can proceed in parallel once the handler exists; the SPA API client builds on the endpoint contract; the panel builds on the API client and wires into the authenticated view; deploy comes after code and IaC; tests follow the code they validate.

```json
{
  "waves": [
    { "id": 18, "tasks": ["28.1"] },
    { "id": 19, "tasks": ["28.2", "28.3"] },
    { "id": 20, "tasks": ["28.4", "29.1"] },
    { "id": 21, "tasks": ["29.2", "29.3", "30.1"] },
    { "id": 22, "tasks": ["30.2", "30.3", "32.1", "32.2"] },
    { "id": 23, "tasks": ["32.3", "33.1"] },
    { "id": 24, "tasks": ["34.1"] },
    { "id": 25, "tasks": ["34.2", "34.3"] },
    { "id": 26, "tasks": ["35.1"] },
    { "id": 27, "tasks": ["35.2"] }
  ]
}
```
