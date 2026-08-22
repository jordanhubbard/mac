import { describe, expect, it } from "vitest";
import { UNKNOWN, bytes, count, duration, ranked, shortId, sum } from "../src/lib/format";
import {
  FLOW_ORDER,
  STATUS_COLORS,
  agentStatusColor,
  healthColor,
  orderStates,
  taskStateColor,
  taskStateTone,
} from "../src/lib/states";

describe("formatters never invent a value", () => {
  it.each([null, undefined, NaN, Infinity])(
    "renders %s as the unknown marker, not zero",
    (value) => {
      expect(duration(value as number)).toBe(UNKNOWN);
      expect(count(value as number)).toBe(UNKNOWN);
      expect(bytes(value as number)).toBe(UNKNOWN);
    },
  );

  it("renders a real zero as zero", () => {
    expect(count(0)).toBe("0");
    expect(duration(0)).toBe("0s");
  });

  it("formats durations at every scale", () => {
    expect(duration(45)).toBe("45s");
    expect(duration(90)).toBe("1m");
    expect(duration(3600 * 3 + 60 * 12)).toBe("3h12m");
    expect(duration(86400 * 9)).toBe("9d0h");
    expect(duration(86400 * 40)).toBe("40d");
  });

  it("groups large counts", () => {
    expect(count(3522)).toBe("3,522");
  });

  it("formats bytes", () => {
    expect(bytes(0)).toBe("0B");
    expect(bytes(2048)).toBe("2.0KB");
  });

  it("truncates long ids but leaves short ones alone", () => {
    expect(shortId("task_abc")).toBe("task_abc");
    expect(shortId("task_0123456789abcdef")).toHaveLength(15);
    expect(shortId(null)).toBe(UNKNOWN);
  });

  it("sums and ranks a count map, tolerating absence", () => {
    expect(sum(undefined)).toBe(0);
    expect(ranked(undefined)).toEqual([]);
    expect(sum({ a: 2, b: 3 })).toBe(5);
    expect(ranked({ a: 2, b: 3, c: 3 })).toEqual([
      ["b", 3],
      ["c", 3],
      ["a", 2],
    ]);
  });
});

describe("state ordering and colour assignment", () => {
  it("orders known states by pipeline position", () => {
    expect(orderStates(["completed", "running", "open"])).toEqual([
      "open",
      "running",
      "completed",
    ]);
  });

  it("shows states it has never heard of rather than dropping them", () => {
    const out = orderStates(["running", "teleporting"]);
    expect(out).toContain("teleporting");
    expect(out[out.length - 1]).toBe("teleporting");
  });

  it("gives every documented task state a colour", () => {
    for (const state of FLOW_ORDER) {
      expect(taskStateColor(state)).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });

  it("keeps the ordinal pipeline ramp distinct from the status palette", () => {
    // open/claimed/running/reviewing are one blue ramp; failed is status red.
    const ramp = new Set(
      ["open", "claimed", "running", "reviewing"].map(taskStateColor),
    );
    expect(ramp.size).toBe(4);
    expect(ramp.has(taskStateColor("failed"))).toBe(false);
    expect(taskStateColor("completed")).not.toBe(taskStateColor("failed"));
  });

  it("classifies exception states so the UI can pair colour with an icon", () => {
    expect(taskStateTone("failed")).toBe("bad");
    expect(taskStateTone("blocked")).toBe("warn");
    expect(taskStateTone("needs_input")).toBe("warn");
    expect(taskStateTone("completed")).toBe("good");
    expect(taskStateTone("running")).toBe("flow");
  });

  it("distinguishes agent statuses", () => {
    const colors = ["idle", "busy", "draining", "offline"].map(agentStatusColor);
    expect(new Set(colors).size).toBe(4);
  });

  it("gives tests_failed a distinct status colour from rejected and infrastructure", () => {
    expect(healthColor("tests_failed")).toBe(STATUS_COLORS.serious);
    expect(healthColor("infrastructure")).toBe(STATUS_COLORS.warning);
    expect(healthColor("rejected")).toBe(STATUS_COLORS.critical);
    expect(healthColor("tests_failed")).not.toBe(healthColor("rejected"));
  });
});
