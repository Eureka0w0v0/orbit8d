import { describe, expect, it } from "vitest";
import { shortcutFor, type KeyContext, type KeyPress, type KeyTarget } from "./shortcuts";

const BODY: KeyTarget = { tagName: "BODY", isContentEditable: false };
const IDLE: KeyContext = { eventSelected: false, ready: true };
const SELECTED: KeyContext = { eventSelected: true, ready: true };

function key(code: string, extra: Partial<KeyPress> = {}): KeyPress {
  return { code, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...extra };
}

const action = (press: KeyPress, ctx: KeyContext = IDLE, target: KeyTarget = BODY) => shortcutFor(press, target, ctx)?.action ?? null;

describe("键盘快捷键", () => {
  it("空格播放 / 暂停，方向键前后跳，B 切换 8D / 原曲", () => {
    expect(shortcutFor(key("Space"), BODY, IDLE)).toEqual({ action: "togglePlay", preventDefault: true });
    expect(action(key("ArrowLeft"))).toBe("seekBack");
    expect(action(key("ArrowRight"))).toBe("seekForward");
    expect(action(key("KeyB"))).toBe("toggleListen");
    expect(action(key("KeyQ"))).toBeNull();
  });

  it("⌘Z / Ctrl+Z 撤销；加 Shift 或者 Ctrl+Y 重做", () => {
    expect(action(key("KeyZ", { metaKey: true }))).toBe("undo");
    expect(action(key("KeyZ", { ctrlKey: true }))).toBe("undo");
    expect(action(key("KeyZ", { metaKey: true, shiftKey: true }))).toBe("redo");
    expect(action(key("KeyY", { ctrlKey: true }))).toBe("redo");
    expect(action(key("KeyZ"))).toBeNull();
  });

  it("按着 ⌘ / Ctrl / Alt 时其余快捷键都不触发（留给浏览器和系统）", () => {
    for (const mod of [{ metaKey: true }, { ctrlKey: true }, { altKey: true }]) {
      expect(action(key("KeyB", mod))).toBeNull();
      expect(action(key("Space", mod))).toBeNull();
      expect(action(key("Delete", mod), SELECTED)).toBeNull();
    }
    expect(action(key("KeyB", { shiftKey: true }))).toBe("toggleListen");
  });

  it("Delete / Backspace 只在选中了事件、歌已打开时删除；Esc 取消选中且不拦默认行为", () => {
    expect(action(key("Delete"), SELECTED)).toBe("deleteEvent");
    expect(action(key("Backspace"), SELECTED)).toBe("deleteEvent");
    expect(action(key("Delete"), IDLE)).toBeNull();
    expect(action(key("Delete"), { eventSelected: true, ready: false })).toBeNull();
    expect(shortcutFor(key("Escape"), BODY, SELECTED)).toEqual({ action: "deselectEvent", preventDefault: false });
    expect(action(key("Escape"), IDLE)).toBeNull();
  });

  it("焦点在按钮、滑杆、下拉框上时，空格和方向键留给它们；B、撤销、删除事件照常", () => {
    for (const tagName of ["BUTTON", "INPUT"]) {
      const target = { tagName, isContentEditable: false, type: tagName === "INPUT" ? "range" : "button" };
      expect(action(key("Space"), IDLE, target)).toBeNull();
      expect(action(key("ArrowRight"), IDLE, target)).toBeNull();
      expect(action(key("KeyB"), IDLE, target)).toBe("toggleListen");
      expect(action(key("KeyZ", { metaKey: true }), IDLE, target)).toBe("undo");
      expect(action(key("Delete"), SELECTED, target)).toBe("deleteEvent");
    }
  });

  it("可以打字的地方什么快捷键都不抢", () => {
    const targets = [
      { tagName: "TEXTAREA", isContentEditable: false },
      { tagName: "SELECT", isContentEditable: false },
      { tagName: "DIV", isContentEditable: true },
      { tagName: "INPUT", isContentEditable: false, type: "text" },
      { tagName: "INPUT", isContentEditable: false, type: "number" },
    ];
    for (const target of targets) {
      expect(action(key("KeyB"), IDLE, target)).toBeNull();
      expect(action(key("KeyZ", { metaKey: true }), IDLE, target)).toBeNull();
      expect(action(key("Backspace"), SELECTED, target)).toBeNull();
    }
  });
});
