# FleetMate Infrastructure (Task 14.1)

Infrastructure-as-code for the Vehicle Diagnostic Assistant backend. CloudFormation
only — the root template is a SAM template (`backend/template.yaml`) that transforms
to CloudFormation; shared resources that benefit from separation live in nested
CloudFormation stacks under `infrastructure/cloudformation/`.

## Layout

```
backend/
  template.yaml                 # SAM root: API + Lambdas + data + events + auth + deploy
  samconfig.toml                # per-environment deploy config (dev/qa/prod stacks)
infrastructure/
  cloudformation/
    monitoring.yaml             # nested stack: CloudWatch alarms + SNS alarm topic
  environments/
    dev/parameters.json         # dev parameter overrides
    qa/parameters.json          # qa parameter overrides
    prod/parameters.json        # prod parameter overrides
```

## What the root template provisions

| Concern | Resource |
|---|---|
| REST API (OpenAPI, TLS, Cognito JWT authorizer) | `AWS::Serverless::Api` |
| Thin API Lambdas | `AnalyzeFunction`, `HistoryFunction`, `RepairEstimatesFunction` |
| Async persistence Lambda | `PersistenceFunction` (SQS event source) |
| Auth | Cognito `UserPool` + `UserPoolClient` + `analysts` group (RBAC) |
| Data | DynamoDB `FleetMateDiagnostics-{env}` single-table, SSE + TTL + `GSI1` |
| Events | EventBridge bus + `DiagnosisCompleted` rule |
| Queue + DLQ | `PersistenceQueue` + `PersistenceDeadLetterQueue` (redrive) |
| Secrets | Secrets Manager `fleetmate/{env}/ai-integration` |
| AI governance | Bedrock model + Guardrail referenced per-env; IAM scoped to the guardrail |
| Observability | X-Ray active tracing; alarms via nested `monitoring.yaml` |
| Deployment | `AutoPublishAlias` + `DeploymentPreference` canary/linear with alarm rollback |

### Lambda handler entry points

- `handlers.analyze.handler` — `POST /diagnostics/analyze`
- `handlers.history.handler` — `GET /diagnostics/history`
- `handlers.repair_estimates.handler` — `GET /repair-estimates`
- `diagnostics.events.persistence_lambda.PersistenceLambda.handle` — SQS consumer

### CloudWatch alarms (monitoring.yaml)

Lambda error rate (per function), API latency (p99 vs the 5s budget), Bedrock
response latency, token-usage spikes, and confidence-score degradation — plus a
DLQ-arrival alarm. The analyze/history/repair-estimates error and latency alarm
names are exported and wired into each function's `DeploymentPreference`, so a
canary deployment rolls back automatically when an alarm fires.

The Bedrock-latency, token-usage, and confidence-score alarms evaluate custom
metrics (`FleetMate/Diagnostics` namespace) the AI engine publishes per
invocation.

## Environment isolation

Each environment is a separate CloudFormation stack (`fleetmate-dev`,
`fleetmate-qa`, `fleetmate-prod`) with its own DynamoDB table, user pool, queues,
secret, and alarms. The `Environment` parameter drives all resource names, so no
resource is shared across environments.

Per-environment values (guardrail id, model id, deployment strategy, alarm email,
TTL) are supplied by the files under `environments/{env}/parameters.json`.
Replace the `REPLACE_WITH_*_GUARDRAIL_ID` placeholders with the real Bedrock
Guardrail ids before deploying.

## Deploy

```bash
# from backend/
sam build
sam deploy --config-env dev      # or qa / prod
```

To deploy with the raw AWS CLI instead of SAM config, pass the environment file:

```bash
aws cloudformation deploy \
  --template-file packaged.yaml \
  --stack-name fleetmate-dev \
  --parameter-overrides file://../infrastructure/environments/dev/parameters.json \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
```

> `dev` uses `AllAtOnce` for fast iteration; `qa` uses linear and `prod` uses a
> canary rollout, both with automatic alarm-based rollback.

## SPA runtime config + asset deploy (tasks 25.1 / 25.2)

The SPA is built once and loads its non-secret settings at runtime from
`/config.json` (`frontend/src/config/runtimeConfig.ts`). That file is generated
from the stack's CloudFormation Outputs so it always matches the live backend.

### Deploy ordering

1. **`sam deploy`** — creates/updates the stack, including the CloudFront
   distribution **and** the Cognito app client's callback/logout URLs. Those
   URLs are derived from the CloudFront domain *inside* the template
   (`!Sub "https://${WebAppDistribution.DomainName}"`), so the former
   "chicken-and-egg" (the app client needs the CloudFront URL, which only
   exists once the distribution is created) is resolved within a single deploy.
   **There is no separate app-client patch step and no manual console edit.**
2. **Generate `config.json`** — now that the distribution and app client have
   resolved, read the outputs and write `frontend/public/config.json`:

   ```bash
   # from frontend/
   npm run generate-config                 # defaults: fleetmate-dev / us-east-1
   # or: node scripts/generate-config.mjs --stack-name fleetmate-qa --region us-east-1
   ```

   The script maps outputs → the `RuntimeConfig` shape and fails with a clear
   message if any field is missing (e.g. if the stack predates the CloudFront /
   Cognito-domain outputs). It is read-only (`describe-stacks`) and writes only
   public client values — no secrets. Use `--dry-run` to preview, or
   `--from-json <file>` to generate from a saved outputs file offline.
3. **Build + upload the SPA** (task 25.2) — generate config, `npm run build`,
   then upload `frontend/dist/` (which now contains the generated `config.json`,
   copied verbatim from `public/`) to the private S3 bucket (`WebAppBucketName`)
   and create a CloudFront invalidation (`WebAppDistributionId`). The
   `deploy-web` script below runs all of this as one repeatable step:

   ```bash
   # from frontend/
   npm run deploy-web                       # defaults: fleetmate-dev / us-east-1
   # or: node scripts/deploy-web.mjs --stack-name fleetmate-qa --region us-east-1
   ```

So the only post-deploy steps are (2) config generation and (3) asset upload —
and `deploy-web` folds both into a single command.

### Full web deploy sequence

```bash
# from backend/
sam deploy --config-env dev              # (1) stack incl. CloudFront + app client

# from frontend/
node scripts/deploy-web.mjs              # (2)+(3) generate config -> build -> upload -> invalidate
```

`scripts/deploy-web.mjs` is cross-platform (Node, no shell-isms) and performs,
in order:

1. Read `WebAppBucketName` + `WebAppDistributionId` (and `WebAppUrl`) from the
   stack via read-only `aws cloudformation describe-stacks`. If those web-tier
   outputs are missing (the web tier has not been deployed yet) it fails with a
   clear message. You can also pass the targets explicitly with
   `--bucket` / `--distribution-id`, or read outputs from a saved file with
   `--from-json <file>`.
2. Generate `public/config.json` (reusing `generate-config.mjs`) so it matches
   the stack, then run `npm run build`. Vite copies `config.json` into `dist/`,
   so the uploaded bundle always carries config that matches the live backend.
   Skip either half with `--skip-config` / `--skip-build`.
3. Upload `dist/` to the private bucket in two cache passes:
   - `dist/assets/**` (hashed, immutable) → `cache-control: public,max-age=31536000,immutable`.
   - `index.html` + `config.json` → `cache-control: no-cache`, so new deploys
     and config changes are picked up immediately.
   Both passes use `aws s3 sync … --delete`, so the bucket mirrors `dist/`
   exactly and re-running is idempotent.
4. Create a CloudFront invalidation for `/*` so the edge serves the fresh
   `index.html`/`config.json` right away.
5. Print the `WebAppUrl` (the CloudFront URL).

Use `--dry-run` to print the exact command plan without uploading or
invalidating anything (reading the stack outputs stays read-only). The script
uploads with the caller's AWS credentials, never makes bucket objects public
(the bucket is reachable only through CloudFront via OAC), and reads/writes no
secrets.

### Output → RuntimeConfig mapping

| RuntimeConfig field | Stack output |
|---|---|
| `apiBaseUrl` | `ApiBaseUrl` |
| `cognitoDomain` | `CognitoDomain` |
| `userPoolId` | `UserPoolId` |
| `appClientId` | `UserPoolClientId` |
| `redirectUri` | `WebCallbackUrl` (falls back to `WebAppUrl`) |
| `logoutUri` | `WebLogoutUrl` (falls back to `WebAppUrl`) |

`redirectUri`/`logoutUri` are the plain CloudFront origin
(`https://<dist>.cloudfront.net`), not a `/callback` path — that exact origin
is what the app client registers as its CallbackURLs/LogoutURLs, and the SPA
reads the `code`/`state` from the query string and hash-routes to its callback
view. Keeping the generated config aligned with the registered URLs avoids
`redirect_mismatch` errors from the Hosted UI.

### Two-phase fallback (not currently needed)

If a future change ever requires the CloudFront domain to be fed back as a
template **parameter** (instead of derived via `!Sub`), the fallback is a
two-phase deploy: deploy once to mint the distribution, read `WebAppUrl`, then
`update-stack` passing it as a parameter. The current template derives the
callback/logout URLs internally, so this step is not required.
