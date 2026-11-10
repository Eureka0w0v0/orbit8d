import { describe, expect, it } from "vitest";
import type { ExportRecord, ExportState, Scene } from "../types";
import { EXPORT_POLL_MS, ExportRun, type ExportJob } from "./export-run";

const JOB: ExportJob = { projectId: "song-a", scene: {} as Scene, format: "m4a", loudness: "loud" };

const record = (state: ExportState, id = "export-1"): ExportRecord =>
  ({ id, project_id: "song-a", state, format: "m4a", loudness: "loud", error: state === "FAILED" ? { code: "ENCODE_FAILED", message: "" } : null, file_name: state === "DONE" ? "Song (8D).m4a" : "" }) as ExportRecord;

/** 假的后端与界面：后端依次给出 states 里的状态；每一次请求、等待和界面更新都按次序记下来。 */
function harness(states: ExportState[], options: { createFails?: boolean; fetchFailsAt?: number } = {}) {
  const log: string[] = [];
  let fetches = 0;
  const run = new ExportRun({
    create: async (job) => {
      log.push(`create ${job.format}/${job.loudness}`);
      if (options.createFails) throw new Error("refused");
      return record("QUEUED");
    },
    fetch: async (id) => {
      const index = fetches++;
      log.push(`fetch ${id}`);
      if (options.fetchFailsAt === index) throw new Error("offline");
      return record(states[Math.min(index, states.length - 1)], id);
    },
    sleep: async (ms) => {
      log.push(`sleep ${ms}`);
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
      "fetch export-1",
      "progress QUEUED",
      wait,
      "fetch export-1",
      "progress RENDERING",
      wait,
      "fetch export-1",
      "progress ENCODING",
      wait,
      "fetch export-1",
      "progress DONE",
      "done Song (8D).m4a",
    ]);
  });

  it("已经导出过的（同样的参数）：第一次问就是完成，不用等", async () => {
    const h = harness(["DONE"]);
    await h.run.start(JOB);
    expect(h.log).toEqual(["progress QUEUED", "create m4a/loud", "fetch export-1", "progress DONE", "done Song (8D).m4a"]);
  });

  it("后台说导出失败：交出带错误码的记录，不再问", async () => {
    const h = harness(["RENDERING", "FAILED"]);
    await h.run.start(JOB);
    expect(h.log.slice(-3)).toEqual(["fetch export-1", "progress FAILED", "failed ENCODE_FAILED"]);
  });

  it("提交被拒绝、或者中途连不上：报错，停下", async () => {
    const refused = harness(["DONE"], { createFails: true });
    await refused.run.start(JOB);
    expect(refused.log).toEqual(["progress QUEUED", "create m4a/loud", "error refused"]);
    const dropped = harness(["RENDERING", "RENDERING", "DONE"], { fetchFailsAt: 1 });
    await dropped.run.start(JOB);
    expect(dropped.log.slice(-2)).toEqual(["fetch export-1", "error offline"]);
  });
});
