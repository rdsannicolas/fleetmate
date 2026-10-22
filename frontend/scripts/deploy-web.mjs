#!/usr/bin/env node
/**
 * deploy-web.mjs — Repeatable web-tier deploy for the FleetMate SPA.
 *
 * Runs the full post-`sam deploy` web deploy sequence, in order:
 *
 *   1. Read the stack Outputs (read-only `aws cloudformation describe-stacks`),
 *      requiring `WebAppBucketName` and `WebAppDistributionId` (and `WebAppUrl`
 *      for the closing summary). Fails clearly if they are missing — that means
 *      the web tier has not been deployed yet.
 *   2. Generate the SPA runtime config (`scripts/generate-config.mjs`) so
 *      `public/config.json` matches the stack, then run the production build
 *      (`npm run build`). Vite copies `public/config.json` into `dist/`, so the
 *      uploaded bundle always carries config that matches the live backend.
 *   3. Upload `dist/` to the private S3 bucket in two cache passes:
 *        - `dist/assets/**` — hashed, immutable files → long-lived
 *          `cache-control: public,max-age=31536000,immutable`.
 *        - everything else (`index.html`, `config.json`) → `no-cache` so new
 *          deploys and config changes are picked up immediately.
 *      Uses `aws s3 sync … --delete` so removed files are pruned and the bucket
 *      is an exact mirror of `dist/` (idempotent — re-running is a no-op when
 *      nothing changed).
 *   4. Create a CloudFront invalidation for `/*` so the edge serves the new
 *      `index.html`/`config.json` immediately.
 *   5. Print the CloudFront URL (`WebAppUrl`).
 *
 * The bucket is private (served only through CloudFront via OAC); this script
 * uploads straight to the bucket with the caller's AWS credentials and never
 * makes objects public. No secrets are read or written.
 *
 * ---------------------------------------------------------------------------
 * Usage
 * ---------------------------------------------------------------------------
 *   node scripts/deploy-web.mjs [options]
 *
 *   --stack-name <name>   CloudFormation stack (default: fleetmate-dev)
 *   --region <region>     AWS region            (default: us-east-1)
 *   --bucket <name>       Override the S3 bucket (skips reading it from outputs)
 *   --distribution-id <id>  Override the CloudFront distribution id
 *   --from-json <path>    Read Outputs from a JSON file instead of the AWS CLI.
 *                         Accepts the describe-stacks array
 *                         ([{OutputKey,OutputValue}]) or a flat
 *                         { "WebAppBucketName": "...", ... } object. Passed
 *                         through to generate-config too.
 *   --skip-config         Do not regenerate config.json (use the existing one)
 *   --skip-build          Do not run `npm run build` (reuse the current dist/)
 *   --dry-run             Print every command that would run; execute nothing
 *   --help                Show this help
 *
 *   Env fallbacks: STACK_NAME, AWS_REGION / AWS_DEFAULT_REGION,
 *                  WEB_APP_BUCKET, WEB_APP_DISTRIBUTION_ID.
 *
 * Reading outputs uses `aws cloudformation describe-stacks`, which is
 * read-only. With `--dry-run` nothing is uploaded or invalidated.
 */

import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { existsSync, readFileSync } from 'node:fs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(__dirname, '..');
const DIST_DIR = resolve(FRONTEND_ROOT, 'dist');
const GENERATE_CONFIG = resolve(__dirname, 'generate-config.mjs');

const DEFAULT_STACK = 'fleetmate-dev';
const DEFAULT_REGION = 'us-east-1';

const ASSETS_CACHE_CONTROL = 'public,max-age=31536000,immutable';
const ENTRY_CACHE_CONTROL = 'no-cache';

const HELP = `deploy-web.mjs — build + upload the FleetMate SPA and invalidate CloudFront

Usage:
  node scripts/deploy-web.mjs [options]

Options:
  --stack-name <name>      CloudFormation stack      (default: ${DEFAULT_STACK})
  --region <region>        AWS region                (default: ${DEFAULT_REGION})
  --bucket <name>          Override the S3 bucket (skip reading from outputs)
  --distribution-id <id>   Override the CloudFront distribution id
  --from-json <path>       Read Outputs from a JSON file instead of the AWS CLI
  --skip-config            Do not regenerate config.json (reuse existing)
  --skip-build             Do not run \`npm run build\` (reuse current dist/)
  --dry-run                Print the commands that would run; execute nothing
  --help                   Show this help

Sequence: generate config -> build -> upload (assets immutable, entry no-cache)
-> CloudFront invalidation -> print WebAppUrl. Reading outputs is read-only
(describe-stacks). Never makes bucket objects public; reads/writes no secrets.`;

function parseArgs(argv) {
  const opts = {
    stackName: process.env.STACK_NAME ?? DEFAULT_STACK,
    region: process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION ?? DEFAULT_REGION,
    bucket: process.env.WEB_APP_BUCKET,
    distributionId: process.env.WEB_APP_DISTRIBUTION_ID,
    fromJson: undefined,
    skipConfig: false,
    skipBuild: false,
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
      case '--bucket':
        opts.bucket = next();
        break;
      case '--distribution-id':
        opts.distributionId = next();
        break;
      case '--from-json':
        opts.fromJson = resolve(process.cwd(), next());
        break;
      case '--skip-config':
        opts.skipConfig = true;
        break;
      case '--skip-build':
        opts.skipBuild = true;
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

function nonEmpty(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

/**
 * Resolve the deploy targets (bucket, distribution id, web URL). Explicit
 * --bucket / --distribution-id (or env) win; anything still unknown is read
 * from the stack outputs. Fails clearly when the web-tier outputs are absent.
 */
function resolveTargets(opts) {
  let bucket = opts.bucket;
  let distributionId = opts.distributionId;
  let webAppUrl;

  const needOutputs = !nonEmpty(bucket) || !nonEmpty(distributionId);

  if (needOutputs || opts.fromJson !== undefined) {
    const outputs =
      opts.fromJson !== undefined
        ? readOutputsFromFile(opts.fromJson)
        : readOutputsFromStack(opts.stackName, opts.region);

    if (!nonEmpty(bucket)) bucket = outputs.WebAppBucketName;
    if (!nonEmpty(distributionId)) distributionId = outputs.WebAppDistributionId;
    webAppUrl = nonEmpty(outputs.WebAppUrl) ? outputs.WebAppUrl.trim() : undefined;

    const missing = [];
    if (!nonEmpty(bucket)) missing.push('WebAppBucketName');
    if (!nonEmpty(distributionId)) missing.push('WebAppDistributionId');
    if (missing.length > 0) {
      const where =
        opts.fromJson !== undefined
          ? `outputs file ${opts.fromJson}`
          : `stack "${opts.stackName}" (${opts.region})`;
      throw new Error(
        `The ${where} is missing required web-tier output(s):\n` +
          missing.map((m) => `  - ${m}`).join('\n') +
          '\n\nThis usually means the web tier (private S3 bucket + CloudFront ' +
          'distribution) has not been deployed yet. Run `sam deploy` to update ' +
          'the stack with the web tier, then re-run this script — or pass the ' +
          'targets explicitly with --bucket / --distribution-id.',
      );
    }
  }

  return {
    bucket: bucket.trim(),
    distributionId: distributionId.trim(),
    webAppUrl,
  };
}

/** Pretty-print a command (program + args) for logs / dry-run plans. */
function formatCommand(program, args) {
  const quoted = args.map((a) => (/[\s"]/.test(a) ? JSON.stringify(a) : a));
  return [program, ...quoted].join(' ');
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

  const prefix = opts.dryRun ? '[dry-run] ' : '';

  // Step 1: resolve deploy targets from outputs (read-only).
  let targets;
  try {
    targets = resolveTargets(opts);
  } catch (error) {
    process.stderr.write(`Error: ${error.message}\n`);
    process.exit(1);
    return;
  }

  const { bucket, distributionId, webAppUrl } = targets;
  const s3Uri = `s3://${bucket}/`;

  // Assemble the ordered plan of steps. Each has a label and an executor.
  const nodeBin = process.execPath;
  const npmBin = process.platform === 'win32' ? 'npm.cmd' : 'npm';

  const steps = [];

  // Step 2a: generate runtime config (shares generate-config.mjs logic).
  if (!opts.skipConfig) {
    const args = [GENERATE_CONFIG];
    if (opts.fromJson !== undefined) {
      args.push('--from-json', opts.fromJson);
    } else {
      args.push('--stack-name', opts.stackName, '--region', opts.region);
    }
    steps.push({
      label: 'Generate runtime config (public/config.json)',
      program: nodeBin,
      args,
      cwd: FRONTEND_ROOT,
    });
  }

  // Step 2b: production build (populates dist/, including config.json).
  if (!opts.skipBuild) {
    steps.push({
      label: 'Build SPA (npm run build)',
      program: npmBin,
      args: ['run', 'build'],
      cwd: FRONTEND_ROOT,
    });
  }

  // Step 3a: upload hashed immutable assets with a long cache lifetime.
  steps.push({
    label: 'Upload immutable hashed assets (long cache)',
    program: 'aws',
    args: [
      's3',
      'sync',
      'dist/assets/',
      `${s3Uri}assets/`,
      '--region',
      opts.region,
      '--delete',
      '--cache-control',
      ASSETS_CACHE_CONTROL,
    ],
    cwd: FRONTEND_ROOT,
  });

  // Step 3b: upload the remainder (index.html, config.json) with no-cache.
  // Done as a second sync that excludes assets/ so the two cache policies do
  // not collide. --delete prunes removed entry files; assets/ is left intact
  // because it is excluded from this pass (its own pass above owns it).
  steps.push({
    label: 'Upload entry documents index.html + config.json (no-cache)',
    program: 'aws',
    args: [
      's3',
      'sync',
      'dist/',
      s3Uri,
      '--region',
      opts.region,
      '--delete',
      '--exclude',
      'assets/*',
      '--cache-control',
      ENTRY_CACHE_CONTROL,
    ],
    cwd: FRONTEND_ROOT,
  });

  // Step 4: invalidate the CloudFront cache so the edge serves fresh entry docs.
  steps.push({
    label: 'Create CloudFront invalidation (/*)',
    program: 'aws',
    args: [
      'cloudfront',
      'create-invalidation',
      '--distribution-id',
      distributionId,
      '--paths',
      '/*',
      '--region',
      opts.region,
    ],
    cwd: FRONTEND_ROOT,
  });

  // Report the resolved plan header.
  process.stdout.write(`${prefix}Web deploy plan\n`);
  process.stdout.write(`${prefix}  stack:        ${opts.stackName} (${opts.region})\n`);
  process.stdout.write(`${prefix}  bucket:       ${bucket}\n`);
  process.stdout.write(`${prefix}  distribution: ${distributionId}\n`);
  if (webAppUrl) {
    process.stdout.write(`${prefix}  web app url:  ${webAppUrl}\n`);
  }
  process.stdout.write('\n');

  // A build/upload needs a dist/ to exist. Guard the real run (dry-run skips
  // the check since the build step would normally create it).
  if (!opts.dryRun && opts.skipBuild && !existsSync(DIST_DIR)) {
    process.stderr.write(
      `Error: ${DIST_DIR} does not exist but --skip-build was passed. ` +
        'Build the SPA first (npm run build) or drop --skip-build.\n',
    );
    process.exit(1);
    return;
  }

  // Execute (or print) each step in order.
  for (let i = 0; i < steps.length; i += 1) {
    const step = steps[i];
    const commandLine = formatCommand(step.program, step.args);
    process.stdout.write(`${prefix}[${i + 1}/${steps.length}] ${step.label}\n`);
    process.stdout.write(`${prefix}  $ ${commandLine}\n`);

    if (opts.dryRun) {
      continue;
    }

    try {
      execFileSync(step.program, step.args, {
        cwd: step.cwd,
        stdio: 'inherit',
        encoding: 'utf8',
      });
    } catch (error) {
      process.stderr.write(
        `\nError: step "${step.label}" failed.\n  command: ${commandLine}\n` +
          `  ${error.message}\n`,
      );
      process.exit(1);
      return;
    }
  }

  process.stdout.write('\n');
  if (opts.dryRun) {
    process.stdout.write('Dry run complete — no AWS resources were changed.\n');
  } else {
    process.stdout.write('Web deploy complete.\n');
  }
  if (webAppUrl) {
    process.stdout.write(`\nWebAppUrl: ${webAppUrl}\n`);
  } else {
    process.stdout.write(
      '\nWebAppUrl: (not found in outputs — check the stack for the CloudFront URL)\n',
    );
  }
}

main();
