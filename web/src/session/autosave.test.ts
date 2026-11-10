import { describe, expect, it } from "vitest";
import type { Scene } from "../types";
import { Autosave, SAVE_DEBOUNCE_MS, SAVE_RETRY_MS, STATE_AFTER, type SaveJob, type SaveState } from "./autosave";

interface Call {
  job: SaveJob;
  keepalive: boolean;
  succeed: () => void;
  fail: (err?: unknown) => void;
}

/** 假的定时器与保存函数：时间由测试推进，每次保存什么时候成功 / 失败由测试决定。 */
function harness() {
  let now = 0;
  let nextId = 0;
  let timers: Array<{ id: number; due: number; fn: () => void }> = [];
  const calls: Call[] = [];
  const states: SaveState[] = [];
  const errors: unknown[] = [];
  const autosave = new Autosave({
    save: (job, keepalive) => new Promise<void>((resolve, reject) => calls.push({ job, keepalive, succeed: resolve, fail: reject })),
    onState: (state) => states.push(state),
    onError: (err) => errors.push(err),
    setTimer: (fn, ms) => {
      timers.push({ id: ++nextId, due: now + ms, fn });
      return nextId;
    },
    clearTimer: (id) => {
      timers = timers.filter((t) => t.id !== id);
    },
  });
  /** 让已经完成的 Promise 的后续代码跑完。 */
  const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
  const advance = async (ms: number) => {
    now += ms;
    for (const timer of timers.filter((t) => t.due <= now).sort((a, b) => a.due - b.due)) {
      timers = timers.filter((t) => t.id !== timer.id);
      timer.fn();
    }
    await settle();
  };
  return { autosave, calls, states, errors, advance, settle, pendingTimers: () => timers.length };
}

const job = (label: string, projectId = "song-a"): SaveJob => ({ projectId, scene: { marker: label } as unknown as Scene });
const labels = (calls: Call[]) => calls.map((c) => (c.job.scene as unknown as { marker: string }).marker);

describe("自动保存", () => {
  it("改完 0.8 秒没再改才保存；连着改只存最后一次", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    await h.advance(SAVE_DEBOUNCE_MS - 1);
    h.autosave.changed(job("2"));
    await h.advance(SAVE_DEBOUNCE_MS - 1);
    expect(h.calls).toHaveLength(0);
    await h.advance(1);
    expect(labels(h.calls)).toEqual(["2"]);
    expect(h.autosave.state).toBe("saving");
    h.calls[0].succeed();
    await h.settle();
    expect(h.autosave.state).toBe("saved");
    expect(h.states).toEqual(["saving", "saving", "saved"]);
  });

  it("保存还没回来时又改了：先不显示已保存，等新的也存好", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    await h.advance(SAVE_DEBOUNCE_MS);
    h.autosave.changed(job("2"));
    h.calls[0].succeed();
    await h.settle();
    expect(h.autosave.state).toBe("saving");
    await h.advance(SAVE_DEBOUNCE_MS);
    expect(labels(h.calls)).toEqual(["1", "2"]);
    h.calls[1].succeed();
    await h.settle();
    expect(h.autosave.state).toBe("saved");
  });

  it("保存失败：显示失败，5 秒后重试同一份，成功后显示已保存", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    await h.advance(SAVE_DEBOUNCE_MS);
    h.calls[0].fail(new Error("offline"));
    await h.settle();
    expect(h.autosave.state).toBe("failed");
    expect(h.errors).toHaveLength(1);
    await h.advance(SAVE_RETRY_MS - 1);
    expect(h.calls).toHaveLength(1);
    await h.advance(1);
    expect(labels(h.calls)).toEqual(["1", "1"]);
    expect(h.autosave.state).toBe("failed"); // 重试期间还显示失败，成功了才改
    h.calls[1].succeed();
    await h.settle();
    expect(h.autosave.state).toBe("saved");
    expect(h.pendingTimers()).toBe(0);
  });

  it("等着重试的时候又改了：只存新的那份", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    await h.advance(SAVE_DEBOUNCE_MS);
    h.calls[0].fail();
    await h.settle();
    h.autosave.changed(job("2"));
    expect(h.autosave.state).toBe("saving");
    await h.advance(SAVE_RETRY_MS);
    expect(labels(h.calls)).toEqual(["1", "2"]);
    h.calls[1].succeed();
    await h.settle();
    expect(h.autosave.state).toBe("saved");
    expect(h.pendingTimers()).toBe(0);
  });

  it("保存途中又改了、随后那次保存失败：显示失败，但接着存的是新的那份", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    await h.advance(SAVE_DEBOUNCE_MS);
    h.autosave.changed(job("2"));
    h.calls[0].fail();
    await h.settle();
    expect(h.autosave.state).toBe("failed");
    await h.advance(SAVE_DEBOUNCE_MS);
    expect(labels(h.calls)).toEqual(["1", "2"]);
    h.calls[1].succeed();
    await h.settle();
    expect(h.autosave.state).toBe("saved");
    await h.advance(SAVE_RETRY_MS);
    expect(h.calls).toHaveLength(2); // 旧的那份不会再被重试
  });

  it("旧的一次保存在更新的保存发出之后才失败：不理它", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    await h.advance(SAVE_DEBOUNCE_MS);
    h.autosave.changed(job("2"));
    await h.advance(SAVE_DEBOUNCE_MS);
    expect(labels(h.calls)).toEqual(["1", "2"]);
    h.calls[0].fail();
    await h.settle();
    expect(h.autosave.state).toBe("saving");
    h.calls[1].succeed();
    await h.settle();
    expect(h.autosave.state).toBe("saved");
    await h.advance(SAVE_RETRY_MS);
    expect(h.calls).toHaveLength(2);
  });

  it("旧的一次保存在更新的保存之后才成功：不会提前显示已保存", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    await h.advance(SAVE_DEBOUNCE_MS);
    h.autosave.changed(job("2"));
    await h.advance(SAVE_DEBOUNCE_MS);
    h.calls[0].succeed();
    await h.settle();
    expect(h.autosave.state).toBe("saving");
  });

  it("换歌、切语言、关页面之前 flush：不等防抖，马上发出去；keepalive 原样传下去", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    const flushed = h.autosave.flush(true);
    expect(h.calls).toHaveLength(1);
    expect(h.calls[0].keepalive).toBe(true);
    h.calls[0].succeed();
    await flushed;
    expect(h.autosave.state).toBe("saved");
    await h.advance(SAVE_DEBOUNCE_MS);
    expect(h.calls).toHaveLength(1); // 防抖定时器已经取消
  });

  it("没有待存的改动时 flush 什么也不做", async () => {
    const h = harness();
    await h.autosave.flush();
    expect(h.calls).toHaveLength(0);
    expect(h.states).toEqual([]);
    expect(h.autosave.state).toBe("saved");
  });

  it("打开一首歌之后显示已保存", () => {
    const h = harness();
    h.autosave.changed(job("1"));
    h.autosave.opened();
    expect(h.autosave.state).toBe("saved");
  });

  it("删掉正在用的歌：没存的改动作废，不再保存", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    let deleted = false;
    await h.autosave.discardWhile(async () => {
      deleted = true;
    });
    await h.advance(SAVE_DEBOUNCE_MS + SAVE_RETRY_MS);
    expect(deleted).toBe(true);
    expect(h.calls).toHaveLength(0);
  });

  it("删歌没删成（后端说歌还在处理）：没存的改动还在，照常保存", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    const refused = h.autosave.discardWhile(() => Promise.reject(new Error("PROJECT_BUSY")));
    await expect(refused).rejects.toThrow("PROJECT_BUSY");
    expect(h.autosave.state).toBe("saving");
    await h.advance(SAVE_DEBOUNCE_MS);
    expect(labels(h.calls)).toEqual(["1"]);
    h.calls[0].succeed();
    await h.settle();
    expect(h.autosave.state).toBe("saved");
  });

  it("删歌没删成，但等结果的时候又改过：存的是更新的那份，只存一次", async () => {
    const h = harness();
    h.autosave.changed(job("1"));
    let refuse: (err: Error) => void = () => undefined;
    const refused = h.autosave.discardWhile(() => new Promise<void>((_, reject) => (refuse = reject)));
    h.autosave.changed(job("2"));
    refuse(new Error("PROJECT_BUSY"));
    await expect(refused).rejects.toThrow("PROJECT_BUSY");
    await h.advance(SAVE_DEBOUNCE_MS);
    expect(labels(h.calls)).toEqual(["2"]);
    await h.advance(SAVE_DEBOUNCE_MS + SAVE_RETRY_MS);
    expect(h.calls).toHaveLength(1);
  });

  it("每件事之后显示什么（状态表）", () => {
    expect(STATE_AFTER).toEqual({ changed: "saving", stored: "saved", storeFailed: "failed", opened: "saved" });
  });
});
