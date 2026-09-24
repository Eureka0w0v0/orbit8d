// 极简 DOM 构造工具 + 通用控件（滑杆、分段选择）。不引入 UI 框架。

type Child = Node | string | null | undefined | false;
type Attrs = Record<string, string | number | boolean | EventListener | undefined>;

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Attrs = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = String(value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child);
  }
  return el;
}

export interface SliderOptions {
  label: string;
  min: number;
  max: number;
  step: number;
  value: number;
  format: (v: number) => string;
  onInput: (v: number) => void;
}

/** 带标签和数值显示的滑杆；set() 在用户没拖动时才更新，避免和拖动打架。 */
export class Slider {
  readonly el: HTMLLabelElement;
  private readonly input: HTMLInputElement;
  private readonly readout: HTMLSpanElement;
  private readonly format: (v: number) => string;

  constructor(opts: SliderOptions) {
    this.format = opts.format;
    this.readout = h("span", { class: "readout" }, opts.format(opts.value));
    this.input = h("input", {
      type: "range",
      min: opts.min,
      max: opts.max,
      step: opts.step,
      value: opts.value,
    });
    this.input.addEventListener("input", () => {
      const v = Number(this.input.value);
      this.readout.textContent = this.format(v);
      opts.onInput(v);
    });
    this.el = h("label", { class: "slider" }, h("span", { class: "slider-head" }, h("span", {}, opts.label), this.readout), this.input);
  }

  set(v: number): void {
    if (document.activeElement === this.input) return;
    this.input.value = String(v);
    this.readout.textContent = this.format(v);
  }
}

/** 分段选择（一排按钮，单选）。 */
export function segmented<T extends string | number>(
  options: ReadonlyArray<{ value: T; label: string }>,
  current: T,
  onPick: (v: T) => void,
  extraClass = "",
): HTMLDivElement {
  const box = h("div", { class: `segmented ${extraClass}` });
  for (const opt of options) {
    const btn = h("button", { type: "button", class: opt.value === current ? "on" : "" }, opt.label);
    btn.addEventListener("click", () => {
      for (const b of box.querySelectorAll("button")) b.classList.remove("on");
      btn.classList.add("on");
      onPick(opt.value);
    });
    box.append(btn);
  }
  return box;
}

export const fmt = {
  deg: (v: number) => `${Math.round(v)}°`,
  db: (v: number) => `${v > 0 ? "+" : ""}${v.toFixed(1)} dB`,
  meters: (v: number) => `${v.toFixed(2)} m`,
  seconds: (v: number) => `${v.toFixed(1)} 秒/圈`,
  ratio: (v: number) => v.toFixed(2),
  percent: (v: number) => `${Math.round(v * 100)}%`,
  clock: (s: number) => {
    const t = Math.max(0, Math.floor(s));
    return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
  },
};
