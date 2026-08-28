import { describe, expect, it } from "vitest";
import { computeDagLayout } from "../src/lib/dag-layout";

describe("layered DAG layout", () => {
  it("puts a prerequisite to the left of its waiter", () => {
    const layout = computeDagLayout(["a", "b"], [{ from: "a", to: "b" }]);
    const a = layout.nodes.find((n) => n.id === "a")!;
    const b = layout.nodes.find((n) => n.id === "b")!;
    expect(a.rank).toBe(0);
    expect(b.rank).toBe(1);
    expect(a.x).toBeLessThan(b.x);
  });

  it("does not throw on a cycle", () => {
    const layout = computeDagLayout(
      ["a", "b"],
      [
        { from: "a", to: "b" },
        { from: "b", to: "a" },
      ],
    );
    expect(layout.nodes).toHaveLength(2);
    expect(layout.edges.some((edge) => edge.isBackEdge)).toBe(true);
  });
});
