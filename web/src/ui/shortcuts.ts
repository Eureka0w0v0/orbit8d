// 键盘快捷键：按键 + 当时的情形 → 要做的动作（纯映射，不碰 DOM，也不执行动作）。
// 空格播放 / 暂停，← / → 跳 5 秒，B 切换 8D / 原曲，⌘Z / ⇧⌘Z（Ctrl+Y）撤销 / 重做，Delete 删除选中的事件，Esc 取消选中。

export type Shortcut = "undo" | "redo" | "toggleListen" | "deleteEvent" | "deselectEvent" | "togglePlay" | "seekBack" | "seekForward";

/** 键盘事件里用得到的部分（KeyboardEvent 天然满足）。 */
export interface KeyPress {
  code: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

/** 按键时焦点所在的元素里用得到的部分（HTMLElement 天然满足）。 */
export interface KeyTarget {
  tagName: string;
  isContentEditable: boolean;
  type?: string;
}

export interface KeyContext {
  /** 时间轴上有选中的事件。 */
  eventSelected: boolean;
  /** 歌已经打开、可以编辑。 */
  ready: boolean;
}

export interface ShortcutHit {
  action: Shortcut;
  /** 要不要拦下浏览器对这个键的默认行为（空格滚屏、Backspace 后退……）。 */
  preventDefault: boolean;
}

const TEXT_ENTRY = new Set(["TEXTAREA", "SELECT"]);
const TEXT_INPUT_TYPES = new Set(["text", "search", "number", "email", "password", "url"]);
const FOCUSABLE_CONTROLS = new Set(["INPUT", "BUTTON", "SELECT"]);

/** 可以打字的地方不抢快捷键（本应用目前没有文本框，防将来加了以后误触）。 */
function isTextEntry(el: KeyTarget): boolean {
  if (TEXT_ENTRY.has(el.tagName) || el.isContentEditable) return true;
  return el.tagName === "INPUT" && TEXT_INPUT_TYPES.has(el.type ?? "");
}

const hit = (action: Shortcut, preventDefault = true): ShortcutHit => ({ action, preventDefault });

export function shortcutFor(e: KeyPress, target: KeyTarget, ctx: KeyContext): ShortcutHit | null {
  if (isTextEntry(target)) return null;
  if ((e.metaKey || e.ctrlKey) && (e.code === "KeyZ" || e.code === "KeyY")) return hit(e.code === "KeyY" || e.shiftKey ? "redo" : "undo");
  if (e.metaKey || e.ctrlKey || e.altKey) return null;
  if (e.code === "KeyB") return hit("toggleListen");
  if ((e.code === "Delete" || e.code === "Backspace") && ctx.eventSelected && ctx.ready) return hit("deleteEvent");
  if (e.code === "Escape" && ctx.eventSelected) return hit("deselectEvent", false);
  if (FOCUSABLE_CONTROLS.has(target.tagName)) return null; // 空格、方向键留给聚焦的控件
  if (e.code === "Space") return hit("togglePlay");
  if (e.code === "ArrowLeft") return hit("seekBack");
  if (e.code === "ArrowRight") return hit("seekForward");
  return null;
}
