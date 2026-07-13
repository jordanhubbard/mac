#!/usr/bin/env node
// Idempotently translate a host-generated Hermes cron plan into OpenClaw jobs.

import {readFileSync, writeFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {spawnSync} from "node:child_process";

const planPath = process.argv[2];
if (!planPath) process.exit(0);

let plan;
try {
  plan = JSON.parse(readFileSync(planPath, "utf8"));
} catch (error) {
  if (error?.code === "ENOENT") process.exit(0);
  throw error;
}
if (plan?.schema !== "mac.openclaw_cron_migration.v1" || !Array.isArray(plan.jobs)) {
  throw new Error("invalid MAC OpenClaw cron migration plan");
}

function openclaw(args, {json = false} = {}) {
  const result = spawnSync("/usr/local/bin/openclaw", args, {
    encoding: "utf8",
    env: process.env,
    maxBuffer: 16 * 1024 * 1024,
  });
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "").trim();
    // A fresh headless OpenShell instance has no interactive device-pairing
    // channel. Cron installation must not terminate an otherwise healthy
    // gateway when the CLI asks for an operator scope upgrade; the plan stays
    // on disk and can be applied after approval is provisioned.
    if (/scope upgrade pending approval|pairing required/i.test(detail)) {
      console.warn(`openclaw cron deferred until device approval: ${detail}`);
      return null;
    }
    throw new Error(`openclaw ${args.join(" ")} failed: ${detail}`);
  }
  return json ? JSON.parse(result.stdout) : result.stdout;
}

const listed = openclaw(["cron", "list", "--json"], {json: true}) || {jobs: []};
const byName = new Map((listed.jobs || []).map((job) => [String(job.name || ""), job]));
const primarySlackAccount = process.env.MAC_OPENCLAW_SLACK_ACCOUNT_ID || "default";

function deliveryArgs(job) {
  const delivery = String(job.delivery || "");
  const origin = job.origin && typeof job.origin === "object" ? job.origin : {};
  if (delivery === "local" || (!delivery && !origin.platform)) return ["--no-deliver"];
  if (delivery.startsWith("slack:") || origin.platform === "slack") {
    const raw = delivery.startsWith("slack:") ? delivery.slice(6) : "";
    const target = String(origin.chat_id || raw || "").trim();
    if (/^[CGD][A-Z0-9]+$/.test(target)) {
      return ["--announce", "--channel", "slack", "--account", primarySlackAccount, "--to", `channel:${target}`];
    }
  }
  // Preserve execution even when a legacy delivery target was a human name
  // OpenClaw cannot resolve durably. The job remains inspectable and its final
  // response is retained locally instead of being sent to the wrong channel.
  return ["--no-deliver"];
}

let deferredScriptJobs = 0;
// Script-backed jobs are installed DISABLED here (they cannot run their host
// pre-run script inside the sandbox). We ALSO emit a host-runner spec for each
// so the two-stage flow can be restored on the HOST, where the scripts and the
// Hermes session DB live. install-openclaw-gateway.sh installs the runner
// (mac-cron-script-runner = run-script-cron-job.py) and schedules these jobs
// via the host supervisor (launchd/systemd). See task_c8bb46ec.
const hostScriptJobs = [];
for (const job of plan.jobs) {
  const name = String(job.name || job.legacy_id || "hermes-job").trim();
  // A Hermes two-stage job ran a pre-run script on the host and injected its
  // stdout into the agent prompt under "## Script Output". OpenClaw cron is
  // message-only and runs in a sandbox without the host script or its data
  // sources (e.g. the Hermes session DB), so installing such a job here would
  // fire it hourly against a prompt that references a script output that was
  // never produced — the exact "cron fires but script output never delivered"
  // failure. Until the script stage is ported (host-side runner), keep these
  // jobs disabled and describe them honestly rather than claiming a lossless
  // migration that silently dropped their first stage.
  const legacyScript = String(job.legacy_script || "").trim();
  const hasScript = legacyScript.length > 0;
  const description = hasScript
    ? `Hermes cron ${job.legacy_id || name}: pre-run script (${legacyScript}) NOT yet ported to OpenClaw; disabled so it does not fire without its data`
    : `Migrated losslessly from Hermes cron ${job.legacy_id || name}`;
  const enable = hasScript ? false : Boolean(job.enabled);
  if (hasScript) {
    deferredScriptJobs += 1;
    // Restore this two-stage job host-side: carry the fields the host runner
    // needs (name, cron, script, message, delivery/origin) so it can run the
    // pre-run script, inject its stdout under "## Script Output", and deliver.
    hostScriptJobs.push({
      name,
      cron: String(job.cron),
      legacy_script: legacyScript,
      message: String(job.message || ""),
      delivery: job.delivery ?? null,
      origin: job.origin ?? null,
      legacy_id: job.legacy_id ?? null,
      enabled: Boolean(job.enabled),
    });
  }
  const common = [
    "--name", name,
    "--cron", String(job.cron),
    "--message", String(job.message || ""),
    "--description", description,
    "--agent", "main",
    "--session", "isolated",
    "--exact",
    ...deliveryArgs(job),
  ];
  const existing = byName.get(name);
  if (existing?.id) {
    openclaw(["cron", "edit", String(existing.id), ...common, enable ? "--enable" : "--disable"]);
  } else {
    openclaw(["cron", "add", ...common, ...(enable ? [] : ["--disabled"]), "--json"]);
  }
}

// Emit the host-runner spec next to the plan so the installer can restore the
// disabled script-backed jobs on the host. Writing must never crash cron
// installation, so failures are logged and swallowed.
let hostScriptJobsPath = null;
try {
  hostScriptJobsPath = join(dirname(planPath), "host-script-jobs.json");
  writeFileSync(
    hostScriptJobsPath,
    JSON.stringify(
      {
        schema: "mac.openclaw_host_script_jobs.v1",
        generated_from: planPath,
        jobs: hostScriptJobs,
      },
      null,
      2,
    ) + "\n",
  );
} catch (error) {
  console.warn(`apply-cron-plan: could not write host-script-jobs.json: ${error?.message || error}`);
  hostScriptJobsPath = null;
}

if (deferredScriptJobs > 0) {
  console.warn(
    `apply-cron-plan: ${deferredScriptJobs} script-backed Hermes job(s) installed DISABLED ` +
    `(pre-run script runs host-side via mac-cron-script-runner; spec at ${hostScriptJobsPath}). ` +
    `See task_c8bb46ec.`,
  );
}

process.stdout.write(
  JSON.stringify({
    schema: plan.schema,
    applied: plan.jobs.length,
    deferred_script_jobs: deferredScriptJobs,
    host_script_jobs: hostScriptJobs.length,
  }) + "\n",
);
