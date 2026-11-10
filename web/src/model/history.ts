// 撤销 / 重做记录（纯数据结构，不依赖浏览器）。
// 每次修改前把“修改前的状态”交给 record；间隔不到 coalesceMs 的连续修改（拖滑杆、拖倾斜盘、拖分界）合并成一步。

export const HISTORY_LIMIT = 100;
export const COALESCE_MS = 500;

export class History<T> {
  private undoStack: T[] = [];
  private redoStack: T[] = [];
  private lastAt = Number.NEGATIVE_INFINITY;

  constructor(
    private readonly limit = HISTORY_LIMIT,
    private readonly coalesceMs = COALESCE_MS,
  ) {}

  /** before：这次修改之前的状态；now：当前时刻（毫秒）。任何新修改都会清空重做记录。 */
  record(before: T, now: number): void {
    if (now - this.lastAt >= this.coalesceMs) {
      this.undoStack.push(before);
      if (this.undoStack.length > this.limit) this.undoStack.shift();
    }
    this.redoStack = [];
    this.lastAt = now;
  }

  /** 返回要恢复的状态；没有可撤销的返回 null。current 进入重做记录。 */
  undo(current: T): T | null {
    const previous = this.undoStack.pop();
    if (previous === undefined) return null;
    this.redoStack.push(current);
    this.lastAt = Number.NEGATIVE_INFINITY; // 撤销之后的下一次修改一定单独成一步
    return previous;
  }

  redo(current: T): T | null {
    const next = this.redoStack.pop();
    if (next === undefined) return null;
    this.undoStack.push(current);
    this.lastAt = Number.NEGATIVE_INFINITY;
    return next;
  }

  clear(): void {
    this.undoStack = [];
    this.redoStack = [];
    this.lastAt = Number.NEGATIVE_INFINITY;
  }

  get canUndo(): boolean {
    return this.undoStack.length > 0;
  }

  get canRedo(): boolean {
    return this.redoStack.length > 0;
  }
}
