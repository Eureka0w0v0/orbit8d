import { describe, expect, it } from "vitest";
import { DICTIONARIES, LANGS, detectLang, errorText, presetName, sectionName } from "./index";

/** 把词典摊平成 “路径 → 值”，函数用固定参数调用一次。 */
function flatten(obj: object, prefix = ""): Map<string, string> {
  const out = new Map<string, string>();
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "function") out.set(path, String((value as (x: unknown) => unknown)(7)));
    else if (typeof value === "object" && value !== null) for (const [k, v] of flatten(value, path)) out.set(k, v);
    else out.set(path, String(value));
  }
  return out;
}

describe("三语词典", () => {
  const flat = Object.fromEntries(LANGS.map((l) => [l, flatten(DICTIONARIES[l])]));

  it("中日英三份词典的条目完全一致，没有空字符串", () => {
    const keys = [...flat.zh.keys()].sort();
    for (const l of LANGS) {
      expect([...flat[l].keys()].sort()).toEqual(keys);
      for (const [path, value] of flat[l]) expect(value.trim(), `${l}:${path}`).not.toBe("");
    }
  });

  it("日文、英文真的翻译过（界面文字不应与中文相同，品牌名和格式说明除外）", () => {
    const allowSame = /^(ui\.appName|ui\.listen8d|format\.ogg|pad\.|shape\.figure8|direction\.diag|shape\.circle|ui\.credits)/;
    for (const l of ["ja", "en"] as const) {
      const same = [...flat[l]].filter(([path, value]) => !allowSame.test(path) && value === flat.zh.get(path)).map(([p]) => p);
      if (l === "en") expect(same).toEqual([]);
      else expect(same.length).toBeLessThan(15); // 日文本来就有不少与中文同形的词：固定、水平、音量、速度、原曲、保存中…
    }
  });
});

describe("语言检测", () => {
  it("记住的选择优先，其次浏览器语言，最后英文", () => {
    expect(detectLang("ja", ["zh-CN"])).toBe("ja");
    expect(detectLang(null, ["zh-TW", "en-US"])).toBe("zh");
    expect(detectLang(null, ["fr-FR", "ja-JP"])).toBe("ja");
    expect(detectLang(null, ["fr-FR", "de"])).toBe("en");
    expect(detectLang("xx", ["ja"])).toBe("ja");
    expect(detectLang(null, [])).toBe("en");
  });
});

describe("显示名", () => {
  it("段落名按语言显示，自定义名字原样保留", () => {
    expect(sectionName("副歌", DICTIONARIES.en)).toBe("Chorus");
    expect(sectionName("副歌", DICTIONARIES.ja)).toBe("サビ");
    expect(sectionName("我的段落", DICTIONARIES.en)).toBe("我的段落");
  });

  it("预设名、错误码", () => {
    expect(presetName("classic", DICTIONARIES.ja)).toBe("クラシック 8D");
    expect(presetName("future", DICTIONARIES.en)).toBe("future");
    expect(errorText("TOO_LARGE", "server text", DICTIONARIES.en)).toBe("The file is too large");
    expect(errorText("DECODE_TIMEOUT", "", DICTIONARIES.en)).toBe("Processing timed out");
    expect(errorText("SOMETHING_NEW", "server text", DICTIONARIES.en)).toBe("server text");
    expect(errorText("SOMETHING_NEW", "", DICTIONARIES.zh)).toBe("内部错误");
  });
});
