// 界面语言的下拉框（顶栏与导入页各放一个）。

import { LANGS, LANG_NAMES, lang, type Lang } from "../i18n";
import { h } from "./dom";
import { TEXT } from "./labels";

/** 语言下拉框（中文 / 日本語 / English），选了就交给 onPick（记住并刷新页面）。 */
export function languageSelect(onPick: (next: Lang) => void): HTMLSelectElement {
  const select = h("select", { class: "lang-select", title: TEXT.language, "aria-label": TEXT.language });
  for (const l of LANGS) select.append(h("option", { value: l, selected: l === lang }, LANG_NAMES[l]));
  select.addEventListener("change", () => onPick(select.value as Lang));
  return select;
}
