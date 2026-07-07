import type { DashboardAgent } from "../api/mac";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function agentHardware(item: DashboardAgent): Record<string, unknown> {
  const agentResources = record(item.agent.resources);
  const machine = record(item.machine);
  const machineResources = record(machine.resources);
  return record(agentResources.hardware || machineResources.hardware || machine.hardware);
}

export function gpuName(item: DashboardAgent): string {
  return String(record(agentHardware(item).gpu).name || "").trim();
}

export function memoryLabel(item: DashboardAgent): string {
  const memoryMb = Number(agentHardware(item).memory_mb || 0);
  if (!Number.isFinite(memoryMb) || memoryMb <= 0) return "—";
  const memoryGb = memoryMb / 1024;
  return `${memoryGb >= 100 ? Math.round(memoryGb) : memoryGb.toFixed(1)} GB`;
}

export function cpuLabel(item: DashboardAgent): string {
  const hardware = agentHardware(item);
  const count = Number(hardware.cpu_count || 0);
  const arch = String(hardware.arch || "").trim();
  if (!count) return arch || "—";
  return `${count} × ${arch || "CPU"}`;
}

export function platformLabel(item: DashboardAgent): string {
  const hardware = agentHardware(item);
  return [hardware.os, hardware.arch].filter(Boolean).map(String).join(" · ") || "No hardware report";
}

export function availableCodingClis(item: DashboardAgent): string[] {
  const codingClis = record(record(item.agent.resources).coding_clis);
  const clis = record(codingClis.clis);
  return Object.entries(clis)
    .filter(([, value]) => record(value).available === true)
    .map(([name]) => name);
}

export function chatGatewayLabel(item: DashboardAgent): string {
  const gateway = record(record(item.agent.resources).chat_gateway);
  const implementation = String(gateway.implementation || "").trim();
  if (!implementation) return "not advertised";
  const confinement = String(record(gateway.confinement).provider || "").trim();
  const channels = record(gateway.channels);
  const activeChannels = Object.entries(channels)
    .filter(([, value]) => record(value).enabled === true)
    .map(([name]) => name);
  const verified = gateway.verified === true ? "verified" : "unverified";
  return [implementation, confinement, activeChannels.join(" + "), verified]
    .filter(Boolean)
    .join(" · ");
}

export function isAgentOnline(item: DashboardAgent): boolean {
  return item.agent.health_status === "healthy" && item.agent.status !== "offline";
}

export function availabilityLabel(item: DashboardAgent): string {
  if (!item.availability) return "eligibility unknown";
  if (item.availability.eligible === true) return "dispatch eligible";
  const reasons = (item.availability.reasons || []).map(String).filter(Boolean);
  return reasons[0] || "not dispatch eligible";
}
