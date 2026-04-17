#!/usr/bin/env node
/**
 * scan.js — Thin Node.js wrapper around the former2 CLI.
 *
 * Usage:
 *   node scripts/scan.js \
 *     --region ap-northeast-1 \
 *     --services Lambda,IAM \
 *     --profile default \
 *     --out-raw raw.json \
 *     --out-cfn cfn-full.yml
 *
 * All flags map directly to former2 generate arguments.
 * stderr from former2 is forwarded to stderr.
 * Exit code mirrors former2's exit code.
 */

'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------

/**
 * Parse CLI arguments into a plain object.
 * @param {string[]} argv - process.argv.slice(2)
 * @returns {Record<string, string | boolean>}
 */
function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg.startsWith('--')) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith('--')) {
        args[key] = next;
        i++;
      } else {
        args[key] = true;
      }
    }
  }
  return args;
}

// ---------------------------------------------------------------------------
// former2 resolution
// ---------------------------------------------------------------------------

/**
 * Resolve path to the former2 binary.
 * Prefers local node_modules/.bin, falls back to global PATH.
 * @returns {string}
 */
function resolveFormer2() {
  const local = path.join(__dirname, '..', 'node_modules', '.bin', 'former2');
  try {
    require('fs').accessSync(local, require('fs').constants.X_OK);
    return local;
  } catch {
    return 'former2'; // rely on PATH (global install)
  }
}

// ---------------------------------------------------------------------------
// Build former2 args
// ---------------------------------------------------------------------------

/**
 * Build the argument list for `former2 generate`.
 * @param {Record<string, string | boolean>} args
 * @returns {string[]}
 */
function buildFormer2Args(args) {
  const former2Args = ['generate'];

  if (args['region']) {
    former2Args.push('--region', String(args['region']));
  }
  if (args['services']) {
    former2Args.push('--services', String(args['services']));
  }
  if (args['exclude-services']) {
    former2Args.push('--exclude-services', String(args['exclude-services']));
  }
  if (args['profile']) {
    former2Args.push('--profile', String(args['profile']));
  }
  if (args['out-raw']) {
    former2Args.push('--output-raw-data', String(args['out-raw']));
  }
  if (args['out-cfn']) {
    former2Args.push('--output-cloudformation', String(args['out-cfn']));
  }

  return former2Args;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

/**
 * Main entry point.
 */
function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args['help'] || args['h']) {
    console.log(`
Usage: node scripts/scan.js [options]

Options:
  --region <region>              AWS region to scan (e.g. ap-northeast-1)
  --services <list>              Comma-separated service list (e.g. Lambda,IAM)
  --exclude-services <list>      Comma-separated services to skip
  --profile <profile>            AWS CLI profile name
  --out-raw <path>               Output path for raw JSON data (default: raw.json)
  --out-cfn <path>               Output path for full CFN YAML (default: cfn-full.yml)
  --help                         Show this help message

Examples:
  node scripts/scan.js --region ap-northeast-1 --services Lambda,IAM
  node scripts/scan.js --region us-east-1 --profile prod --out-raw raw.json --out-cfn cfn-full.yml
    `.trim());
    process.exit(0);
  }

  const former2Bin = resolveFormer2();
  const former2Args = buildFormer2Args(args);

  process.stderr.write(`[scan.js] Running: ${former2Bin} ${former2Args.join(' ')}\n`);

  const result = spawnSync(former2Bin, former2Args, {
    stdio: 'inherit',
    shell: false,
  });

  if (result.error) {
    process.stderr.write(`[scan.js] Failed to spawn former2: ${result.error.message}\n`);
    process.stderr.write('  Is former2 installed? Run: npm install\n');
    process.exit(1);
  }

  process.exit(result.status ?? 1);
}

main();
