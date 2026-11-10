import { describe, expect, it } from "vitest";
import type { ExportRecord, ExportState, Scene } from "../types";
import { EXPORT_POLL_MS, ExportRun, type ExportJob } from "./export-run";

const JOB: ExportJob = { projectId: "song-a", scene: {} as Scene, format: "m4a", loudness: "loud" };

const record = (state: ExportState, id = "export-m4a"): ExportRecord =>
  ({ id, project_id: "song-a", state, format: "m4a", loudness: "loud", error: state === "FAILED" ? { code: "ENCODE_FAILED", message: "" } : null, file_name: state === "DONE" ? "Song (8D).m4a" : "" }) as ExportRecord;

/** 假的后端与界面：后端依次给出 states 里的状态；每一次请求、等待和界面更新都按次序记下来。 */
function harness(states: ExportState[], options: { createFails?: boolean; fetchFailsAt?: number; during?: Record<string, () => void> } = {}) {
  const log: string[] = [];
  let fetches = 0;
  /** 记一条；options.during 里登记了这一条的，就在这个时刻插进去执行（模拟用户此时关掉了对话框等）。 */
  const note = (line: string) => {
    log.push(line);
    options.during?.[line]?.();
  };
  const run = new ExportRun({
    create: async (job) => {
      note(`create ${job.format}/${job.loudness}`);
      if (options.createFails) throw new Error("refused");
      return record("QUEUED", `export-${job.format}`);
    },
    fetch: async (id) => {
      const index = fetches++;
      note(`fetch ${id}`);
      if (options.fetchFailsAt === index) throw new Error("offline");
      return record(states[Math.min(index, states.length - 1)], id);
    },
    sleep: async (ms) => {
      note(`sleep ${ms}`);
    },
    onProgress: (state) => log.push(`progress ${state}`),
    onDone: (rec) => log.push(`done ${rec.file_name}`),
    onFailed: (rec) => log.push(`failed ${rec.error?.code}`),
    onError: (err) => log.push(`error ${(err as Error).message}`),
  });
  return { run, log };
}

describe("导出的等待过程", () => {
  it("先显示排队，提交，然后每 0.7 秒问一次，直到完成", async () => {
    const h = harness(["QUEUED", "RENDERING", "ENCODING", "DONE"]);
    await h.run.start(JOB);
    const wait = `sleep ${EXPORT_POLL_MS}`;
    expect(h.log).toEqual([
      "progress QUEUED",
      "create m4a/loud",
      "fetch export-m4a",
      "progress QUEUED",
      wait,
      "fetch export-m4a",
      "progress RENDERING",
      wait,
      "fetch export-m4a",
      "progress ENCODING",
      wait,
      "fetch export-m4a",
      "progress DONE",
      "done Song (8D).m4a",
    ]);
  });

  it("已经导出过的（同样的参数）：第一次问就是完成，不用等", async () => {
    const h = harness(["DONE"]);
    await h.run.start(JOB);
    expect(h.log).toEqual(["progress QUEUED", "create m4a/loud", "fetch export-m4a", "progress DONE", "done Song (8D).m4a"]);
  });

  it("后台说导出失败：交出带错误码的记录，不再问", async () => {
    const h = harness(["RENDERING", "FAILED"]);
    await h.run.start(JOB);
    expect(h.log.slice(-3)).toEqual(["fetch export-m4a", "progress FAILED", "failed ENCODE_FAILED"]);
  });

  it("提交被拒绝、或者中途连不上：报错，停下", async () => {
    const refused = harness(["DONE"], { createFails: true });
    await refused.run.start(JOB);
    expect(refused.log).toEqual(["progress QUEUED", "create m4a/loud", "error refused"]);
    const dropped = harness(["RENDERING", "RENDERING", "DONE"], { fetchFailsAt: 1 });
    await dropped.run.start(JOB);
    expect(dropped.log.slice(-2)).toEqual(["fetch export-m4a", "error offline"]);
  });
});

describe("不再等的导出", () => {
  const wait = `sleep ${EXPORT_POLL_MS}`;

  it("等的时候取消（关掉了对话框）：不再问进度，界面不再更新，也不会冒出下载", async () => {
    let cancels = 0;
    const h = harness(["RENDERING", "RENDERING", "DONE"], { during: { [wait]: () => ++cancels === 2 && h.run.cancel() } });
    await h.run.start(JOB);
    expect(h.log).toEqual(["progress QUEUED", "create m4a/loud", "fetch export-m4a", "progress RENDERING", wait, "fetch export-m4a", "progress RENDERING", wait]);
  });

  it("进度请求还没回来时取消：回来的结果不显示", async () => {
    let fetches = 0;
    const h = harness(["RENDERING", "DONE"], { during: { "fetch export-m4a": () => ++fetches === 2 && h.run.cancel() } });
    await h.run.start(JOB);
    expect(h.log.slice(-3)).toEqual(["progress RENDERING", wait, "fetch export-m4a"]);
  });

  it("提交还没回来时取消：不去问进度；取消之后连不上也不报错", async () => {
    const early = harness(["DONE"], { during: { "create m4a/loud": () => early.run.cancel() } });
    await early.run.start(JOB);
    expect(early.log).toEqual(["progress QUEUED", "create m4a/loud"]);
    const offline = harness(["RENDERING"], { fetchFailsAt: 1, during: { [wait]: () => offline.run.cancel() } });
    await offline.run.start(JOB);
    expect(offline.log.filter((line) => line.startsWith("error"))).toEqual([]);
  });

  it("又开始一次导出：上一次不再理会，只有新的这次更新界面", async () => {
    const second: ExportJob = { ...JOB, format: "wav" };
    let started: Promise<void> | null = null;
    const h = harness(["RENDERING", "RENDERING", "DONE"], {
      during: { [wait]: () => (started ??= h.run.start(second)) },
    });
    await h.run.start(JOB);
    await started;
    const afterSecondStart = h.log.slice(h.log.indexOf("create wav/loud"));
    expect(afterSecondStart.filter((line) => line.includes("export-m4a"))).toEqual([]);
    expect(h.log.filter((line) => line.startsWith("done"))).toHaveLength(1);
    expect(h.log.at(-1)).toBe("done Song (8D).m4a");
  });
});
