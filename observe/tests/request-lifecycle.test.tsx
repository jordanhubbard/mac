import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ConsoleClient, TaskDrilldown } from "../src/lib/api";
import { useTask } from "../src/lib/useTask";

function deferred() {
  let resolve!: (value: TaskDrilldown) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<TaskDrilldown>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

describe("task selection owns its response", () => {
  it.each(["success", "error"])(
    "ignores an obsolete %s and clears previous detail",
    async (outcome) => {
      const a = deferred();
      const b = deferred();
      const client = {
        task: (id: string) => (id === "a" ? a.promise : b.promise),
      } as ConsoleClient;
      const hook = renderHook(({ id }) => useTask(client, id), {
        initialProps: { id: "a" },
      });
      hook.rerender({ id: "b" });
      expect(hook.result.current.detail).toBeNull();
      await act(async () => b.resolve({ task_id: "b" } as TaskDrilldown));
      await act(async () =>
        outcome === "success"
          ? a.resolve({ task_id: "a" } as TaskDrilldown)
          : a.reject(new Error("old failure")),
      );
      expect(hook.result.current.detail?.task_id).toBe("b");
      expect(hook.result.current.error).toBeNull();
      hook.rerender({ id: "a" });
      expect(hook.result.current.detail).toBeNull();
      hook.unmount();
    },
  );

  it("keeps same-task detail during reload, but ignores a response after clearing selection", async () => {
    const first = deferred();
    const second = deferred();
    let calls = 0;
    const client = {
      task: () => (++calls === 1 ? first.promise : second.promise),
    } as unknown as ConsoleClient;
    const hook = renderHook(({ id }) => useTask(client, id), {
      initialProps: { id: "a" as string | null },
    });
    await act(async () => first.resolve({ task_id: "a" } as TaskDrilldown));
    act(() => hook.result.current.reload());
    expect(hook.result.current.detail?.task_id).toBe("a");
    expect(hook.result.current.loading).toBe(true);
    hook.rerender({ id: null });
    await act(async () => second.resolve({ task_id: "a" } as TaskDrilldown));
    expect(hook.result.current.detail).toBeNull();
    expect(hook.result.current.loading).toBe(false);
    hook.unmount();
  });
});
