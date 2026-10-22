#!/usr/bin/env node
/**
 * generate-config.mjs — Deploy-time generator for the SPA runtime config.
 *
 * The FleetMate SPA is built once and deployed unchanged to any environment. It
 * loads its environment-specific, non-secret settings at runtime from
 * `/config.json` (see `src/config/runtimeConfig.ts` → `loadRuntimeConfig()`).
 * This script produces that `config.json` from the deployed CloudFormation
 * stack's Outputs, so the file always matches the live backend.
 *
 * It writes `frontend/public/config.json`. Vite copies everything in `public/`
 * verbatim into `dist/` at build time, so the generated config ships with the
 * compiled bundle that task 25.2 uploads to S3.
 *
 * ---------------------------------------------------------------------------
 * Output → RuntimeConfig mapping
 * ---------------------------------------------------------------------------
 *   apiBaseUrl   ← ApiBaseUrl        (REST API invoke URL, e.g. .../v1)
 *   cognitoDomain← CognitoDomain     (Hosted UI base, https://<prefix>.auth...)
 *   userPoolId   ← UserPoolId
 *   appClientId  ← UserPoolClientId
 *   redirectUri  ← WebCallbackUrl    (falls back to WebAppUrl)
 *   logoutUri    ← WebLogoutUrl      (falls back to WebAppUrl)
 *
 * `redirectUri`/`logoutUri` are the plain CloudFront origin
 * (https://<dist>.cloudfront.net) — NOT a /callback path. The Cognito app
 * client registers that exact origin as its CallbackURLs/LogoutURLs in the
 * template (`!Sub "https://${WebAppDistribution.DomainName}"`). The SPA reads
 * the authorization `code`/`state` straight from the query string and
 * hash-routes to its callback view, so the registered redirect is the origin
 * itself. Keeping the generated config aligned with the registered URLs avoids
 * `redirect_mismatch` errors from the Hosted UI.
 *
 * Only public client values are emitted; NO secrets are ever written.
 *
 * ---------------------------------------------------------------------------
 * Deploy ordering (why this step exists and when to run it)
 * ---------------------------------------------------------------------------
 *   1. `sam deploy` — creates/updates the stack, including the CloudFront
 *      distribution AND the Cognito app client's callback/logout URLs. Because
 *      those URLs are derived from the CloudFront domain *inside* the template
 *      (`!Sub "https://${WebAppDistribution.DomainName}"`), the former
 *      "chicken-and-egg" (app client needs the CloudFront URL, which only
 *      exists after the distribution is created) is resolved within a single
 *      deploy. There is NO separate app-client patch / second update-stack
 *      step and NO manual console edit required.
 *   2. Run THIS script — now that the distribution and app client have
 *      resolved, read the stack outputs and write `public/config.json`.
 *   3. Build + upload the SPA (task 25.2) — `npm run build` then upload
 *      `dist/` (which now contains the generated `config.json`) to the private
 *      S3 bucket and invalidate the CloudFront cache.
 *
 * The only post-deploy steps are therefore: (2) config generation and
 * (3) asset upload. If a future change ever needs the CloudFront domain fed
 * back in as a template *parameter* (rather than derived via !Sub), the
 * fallback is a two-phase deploy: deploy once to mint the distribution, read
 * `WebAppUrl`, then `update-stack` passing it as a parameter. That is NOT the
 * current design.
 *
 * ---------------------------------------------------------------------------
 * Usage
 * ---------------------------------------------------------------------------
 *   node scripts/generate-config.mjs [options]
 *
 *   --stack-name <name>   CloudFormation stack (default: fleetmate-dev)
 *   --region <region>     AWS region            (default: us-east-1)
 *   --out <path>          Output file (default: <frontend>/public/config.json)
 *   --from-json <path>    Read Outputs from a JSON file instead of calling the
 *                         AWS CLI. Accepts either the raw
 *                         `Stacks[0].Outputs` array ([{OutputKey,OutputValue}])
 *                         or a flat `{ "ApiBaseUrl": "...", ... }` object.
 *   --dry-run             Print the resulting config to stdout; do not write.
 *   --help                Show this help.
 *
 *   Env fallbacks: STACK_NAME, AWS_REGION / AWS_DEFAULT_REGION.
 *
 * Reading outputs uses `aws cloudformation describe-stacks`, which is
 * read-only. This script never deploys, uploads, or mutates any AWS resource.
 */

import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(__dirname, '..');
const DEFAULT_OUT = resolve(FRONTEND_ROOT, 'public', 'config.json');

const DEFAULT_STACK = 'fleetmate-dev';
const DEFAULT_REGION = 'us-east-1';

/**
 * RuntimeConfig fields (keep in sync with src/config/runtimeConfig.ts) and the
 * stack output key(s) each is sourced from. The first present/non-empty output
 * wins, so a primary key can fall back to an alternate.
 */
const FIELD_SOURCES = {
  apiBaseUrl: ['ApiBaseUrl'],
  cognitoDomain: ['CognitoDomain'],
  userPoolId: ['UserPoolId'],
  appClientId: ['UserPoolClientId'],
  redirectUri: ['WebCallbackUrl', 'WebAppUrl'],
  logoutUri: ['WebLogoutUrl', 'WebAppUrl'],
};

const HELP = `generate-config.mjs — write frontend/public/config.json from CloudFormation outputs

Usage:
  node scripts/generate-config.mjs [options]

Options:
  --stack-name <name>   CloudFormation stack       (default: ${DEFAULT_STACK})
  --region <region>     AWS region                 (default: ${DEFAULT_REGION})
  --out <path>          Output file                (default: public/config.json)
  --from-json <path>    Read Outputs from a JSON file instead of the AWS CLI
  --dry-run             Print the config to stdout; do not write a file
  --help                Show this help

Reads outputs via "aws cloudformation describe-stacks" (read-only). Never
deploys or uploads anything. Emits only public, non-secret client values.`;

function parseArgs(argv) {
  const opts = {
    stackName: process.env.STACK_NAME ?? DEFAULT_STACK,
    region: process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION ?? DEFAULT_REGION,
    out: DEFAULT_OUT,
    fromJson: undefined,
    dryRun: false,
    help: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const value = argv[i + 1];
      if (value === undefined) {
        throw new Error(`Option ${arg} requires a value`);
      }
      i += 1;
      return value;
    };
    switch (arg) {
      case '--stack-name':
        opts.stackName = next();
        break;
      case '--region':
        opts.region = next();
        break;
      case '--out':
        opts.out = resolve(process.cwd(), next());
        break;
      case '--from-json':
        opts.fromJson = resolve(process.cwd(), next());
        break;
      case '--dry-run':
        opts.dryRun = true;
        break;
      case '-h':
      case '--help':
        opts.help = true;
        break;
      default:
        throw new Error(`Unknown option: ${arg}`);
    }
  }
  return opts;
}

/**
 * Normalize CloudFormation Outputs into a flat `{ OutputKey: OutputValue }`
 * map. Accepts either the describe-stacks array form or an already-flat object.
 */
function toOutputMap(raw) {
  if (Array.isArray(raw)) {
    const map = {};
    for (const entry of raw) {
      if (entry && typeof entry.OutputKey === 'string') {
        map[entry.OutputKey] = entry.OutputValue;
      }
    }
    return map;
  }
  if (raw && typeof raw === 'object') {
    return raw;
  }
  throw new Error('Outputs JSON must be an array of {OutputKey,OutputValue} or a flat object');
}

/** Read stack outputs from a local JSON file (for dry runs / offline tests). */
function readOutputsFromFile(path) {
  let text;
  try {
    text = readFileSync(path, 'utf8');
  } catch (cause) {
    throw new Error(`Could not read outputs file ${path}: ${cause.message}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (cause) {
    throw new Error(`Outputs file ${path} is not valid JSON: ${cause.message}`);
  }
  return toOutputMap(parsed);
}

/** Read stack outputs from the live stack via the AWS CLI (read-only). */
function readOutputsFromStack(stackName, region) {
  let stdout;
  try {
    stdout = execFileSync(
      'aws',
      [
        'cloudformation',
        'describe-stacks',
        '--stack-name',
        stackName,
        '--region',
        region,
        '--query',
        'Stacks[0].Outputs',
        '--output',
        'json',
      ],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
    );
  } catch (cause) {
    const detail = cause.stderr?.toString().trim() || cause.message;
    throw new Error(
      `Failed to read outputs for stack "${stackName}" in ${region} via AWS CLI.\n` +
        `  ${detail}\n` +
        '  Check the stack name/region and that AWS credentials are configured, ' +
        'or pass --from-json to work from a saved outputs file.',
    );
  }

  const trimmed = stdout.trim();
  if (trimmed === '' || trimmed === 'null') {
    throw new Error(`Stack "${stackName}" returned no outputs.`);
  }
  return toOutputMap(JSON.parse(trimmed));
}

function firstNonEmpty(outputs, keys) {
  for (const key of keys) {
    const value = outputs[key];
    if (typeof value === 'string' && value.trim().length > 0) {
      return { key, value: value.trim() };
    }
  }
  return undefined;
}

/** Map outputs → RuntimeConfig, validating every field is present/non-empty. */
function buildConfig(outputs) {
  const config = {};
  const missing = [];

  for (const [field, keys] of Object.entries(FIELD_SOURCES)) {
    const hit = firstNonEmpty(outputs, keys);
    if (hit === undefined) {
      missing.push(`${field} (expected output ${keys.join(' or ')})`);
    } else {
      config[field] = hit.value;
    }
  }

  if (missing.length > 0) {
    throw new Error(
      'Cannot generate config.json — the stack is missing required output(s):\n' +
        missing.map((m) => `  - ${m}`).join('\n') +
        '\n\nThis usually means the stack was deployed before the CloudFront / ' +
        'Cognito-domain outputs were added. Run `sam deploy` to update the ' +
        'stack, then re-run this script.',
    );
  }
  return config;
}

function main() {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n\n${HELP}\n`);
    process.exit(2);
    return;
  }

  if (opts.help) {
    process.stdout.write(`${HELP}\n`);
    return;
  }

  let outputs;
  let source;
  try {
    if (opts.fromJson !== undefined) {
      outputs = readOutputsFromFile(opts.fromJson);
      source = `file ${opts.fromJson}`;
    } else {
      outputs = readOutputsFromStack(opts.stackName, opts.region);
      source = `stack ${opts.stackName} (${opts.region})`;
    }
  } catch (error) {
    process.stderr.write(`Error: ${error.message}\n`);
    process.exit(1);
    return;
  }

  let config;
  try {
    config = buildConfig(outputs);
  } catch (error) {
    process.stderr.write(`Error: ${error.message}\n`);
    process.exit(1);
    return;
  }

  const json = `${JSON.stringify(config, null, 2)}\n`;

  if (opts.dryRun) {
    process.stdout.write(`# Config from ${source} (dry run — not written):\n`);
    process.stdout.write(json);
    return;
  }

  try {
    mkdirSync(dirname(opts.out), { recursive: true });
    writeFileSync(opts.out, json, 'utf8');
  } catch (error) {
    process.stderr.write(`Error: could not write ${opts.out}: ${error.message}\n`);
    process.exit(1);
    return;
  }

  process.stdout.write(`Wrote ${opts.out} from ${source}:\n${json}`);
}

main();
