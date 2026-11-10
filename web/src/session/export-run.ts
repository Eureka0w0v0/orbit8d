// 一次导出从提交到拿到结果的等待过程：提交 → 每隔一会儿问一次进度 → 完成 / 失败。
// 同一时间只等一个：取消（关掉了导出对话框）或者又开始一次之后，之前那次的结果一概不理——后台照常把它做完，
// 同样的参数再导出一次会直接拿到。请求、等待和显示都由外面传入，这里只管先后次序。

import type { ExportFormat, ExportRecord, ExportState, Loudness, Scene } from "../types";

export const EXPORT_POLL_MS = 700;

export interface ExportJob {
  projectId: string;
  scene: Scene;
  format: ExportFormat;
  loudness: Loudness;
}

export interface ExportRunDeps {
  create(job: ExportJob): Promise<ExportRecord>;
  fetch(exportId: string): Promise<ExportRecord>;
  sleep(ms: number): Promise<void>;
  /** 进行到哪个阶段了（提交之前先报一次 QUEUED）。 */
  onProgress(state: ExportState): void;
  onDone(record: ExportRecord): void;
  /** 后台说这次导出失败了（记录里带错误码）。 */
  onFailed(record: ExportRecord): void;
  /** 请求本身出了错（连不上、被拒绝……）。 */
  onError(err: unknown): void;
}

export class ExportRun {
  private current = 0; // 正在等的是第几次；每次开始或取消都加一，之前那次就此作废

  constructor(private readonly deps: ExportRunDeps) {}

  /** 不再等正在进行的这次导出。 */
  cancel(): void {
    this.current++;
  }

  async start(job: ExportJob): Promise<void> {
    const run = ++this.current;
    const live = () => run === this.current;
    try {
      this.deps.onProgress("QUEUED");
      const created = await this.deps.create(job);
      while (live()) {
        const record = await this.deps.fetch(created.id);
        if (!live()) return;
        this.deps.onProgress(record.state);
        if (record.state === "DONE") return this.deps.onDone(record);
        if (record.state === "FAILED") return this.deps.onFailed(record);
        await this.deps.sleep(EXPORT_POLL_MS);
      }
    } catch (err) {
      if (live()) this.deps.onError(err);
    }
  }
}
