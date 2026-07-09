#!/usr/bin/env node
// Idempotently translate a host-generated Hermes cron plan into OpenClaw jobs.

import {readFileSync} from "node:fs";
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
    throw new Error(`openclaw ${args.join(" ")} failed: ${(result.stderr || result.stdout || "").trim()}`);
  }
  return json ? JSON.parse(result.stdout) : result.stdout;
}

const listed = openclaw(["cron", "list", "--json"], {json: true});
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

for (const job of plan.jobs) {
  const name = String(job.name || job.legacy_id || "hermes-job").trim();
  const common = [
    "--name", name,
    "--cron", String(job.cron),
    "--message", String(job.message || ""),
    "--description", `Migrated losslessly from Hermes cron ${job.legacy_id || name}`,
    "--agent", "main",
    "--session", "isolated",
    "--exact",
    ...deliveryArgs(job),
  ];
  const existing = byName.get(name);
  if (existing?.id) {
    openclaw(["cron", "edit", String(existing.id), ...common, job.enabled ? "--enable" : "--disable"]);
  } else {
    openclaw(["cron", "add", ...common, ...(job.enabled ? [] : ["--disabled"]), "--json"]);
  }
}

process.stdout.write(JSON.stringify({schema: plan.schema, applied: plan.jobs.length}) + "\n");
