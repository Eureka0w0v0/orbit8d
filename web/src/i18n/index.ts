// 界面语言：中文 / 日本語 / English。页面启动时确定一次（记住的选择 > 浏览器语言 > 英文）；
// 切换语言会记住选择并刷新页面（界面文字在启动时一次性生成；场景已自动保存，不会丢）。

import { en } from "./en";
import { ja } from "./ja";
import { zh, type Messages } from "./zh";

export type Lang = "zh" | "ja" | "en";
export type { Messages };
export const LANGS: readonly Lang[] = ["zh", "ja", "en"];
export const LANG_NAMES: Record<Lang, string> = { zh: "中文", ja: "日本語", en: "English" };
export const DICTIONARIES: Record<Lang, Messages> = { zh, ja, en };
const STORAGE_KEY = "orbit8d.lang";

/** 纯函数：记住的选择优先；否则按浏览器语言列表依次找 zh / ja / en；都没有用英文。 */
export function detectLang(stored: string | null, preferred: readonly string[]): Lang {
  if (stored !== null && (LANGS as readonly string[]).includes(stored)) return stored as Lang;
  for (const tag of preferred) {
    const base = tag.toLowerCase().split(/[-_]/)[0];
    if (base === "zh" || base === "ja" || base === "en") return base;
  }
  return "en";
}

function storedLang(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch (err) {
    console.warn("[orbit8d] localStorage unavailable, falling back to the browser language", err);
    return null;
  }
}

function browserLangs(): readonly string[] {
  if (typeof navigator === "undefined") return [];
  return navigator.languages?.length ? navigator.languages : [navigator.language];
}

export const lang: Lang = typeof window === "undefined" ? "en" : detectLang(storedLang(), browserLangs());
export const T: Messages = DICTIONARIES[lang];

/** 记住选择并刷新页面。存不下（隐私模式）时只影响这一次，刷新后回到浏览器语言。 */
export function switchLang(next: Lang): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, next);
  } catch (err) {
    console.warn("[orbit8d] could not remember the language choice", err);
  }
  window.location.reload();
}

/** 存档里的段落名是后端给的中文规范名；界面按当前语言显示，用户自定义的名字原样显示。 */
const SECTION_KEYS: Record<string, keyof Messages["section"]> = {
  前奏: "intro",
  主歌: "verse",
  副歌: "chorus",
  桥段: "bridge",
  尾声: "outro",
  全曲: "whole",
};

export function sectionName(label: string, m: Messages = T): string {
  const key = SECTION_KEYS[label];
  return key ? m.section[key] : label;
}

export function presetName(name: string, m: Messages = T): string {
  return (m.preset as Record<string, string>)[name] ?? name;
}

export function directionName(key: string, m: Messages = T): string {
  return (m.direction as Record<string, string>)[key] ?? key;
}

/** 按错误码给出当前语言的提示；不认识的码用服务端原文。 */
export function errorText(code: string, fallback: string, m: Messages = T): string {
  const errors = m.error as Record<string, string>;
  return errors[code] ?? (code.endsWith("_TIMEOUT") ? errors.TIMEOUT : undefined) ?? (fallback || errors.INTERNAL);
}
