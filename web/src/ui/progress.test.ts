import { describe as suite, expect, it } from "vitest";
import { ApiError } from "../api";
import { DICTIONARIES } from "../i18n";
import { BUSY, describe, overallProgress, progressOf } from "./progress";

const en = DICTIONARIES.en; // 测试环境没有浏览器：界面语言是英文

suite("导入进度", () => {
  it("各阶段在总进度条上首尾相接，分轨占大头", () => {
    expect(overallProgress({ state: "UPLOADED", progress: 0 })).toBe(0);
    expect(overallProgress({ state: "UPLOADED", progress: 1 })).toBeCloseTo(0.02, 12);
    expect(overallProgress({ state: "DECODING", progress: 0.5 })).toBeCloseTo(0.04, 12);
    expect(overallProgress({ state: "SEPARATING", progress: 0 })).toBeCloseTo(0.06, 12);
    expect(overallProgress({ state: "SEPARATING", progress: 0.5 })).toBeCloseTo(0.47, 12);
    expect(overallProgress({ state: "ANALYZING", progress: 0 })).toBeCloseTo(0.88, 12);
    expect(overallProgress({ state: "ANALYZING", progress: 1 })).toBe(1);
  });

  it("已经结束的状态（就绪、失败）进度是满的", () => {
    expect(overallProgress({ state: "READY", progress: 0 })).toBe(1);
    expect(overallProgress({ state: "FAILED", progress: 0.3 })).toBe(1);
  });

  it("平时显示状态名与总进度；第一次下载分轨模型时显示下载本身的进度", () => {
    expect(progressOf({ state: "SEPARATING", progress: 0.5, step: "" })).toEqual({ title: en.projectState.SEPARATING, fraction: overallProgress({ state: "SEPARATING", progress: 0.5 }) });
    expect(progressOf({ state: "SEPARATING", progress: 0.3, step: "model_download" })).toEqual({ title: en.ui.modelDownload, fraction: 0.3 });
  });

  it("处理中的状态 = 就绪和失败以外的全部", () => {
    expect([...BUSY].sort()).toEqual(["ANALYZING", "DECODING", "SEPARATING", "UPLOADED"]);
  });
});

suite("给用户看的出错说明", () => {
  it("带错误码的按当前语言翻译，后面带上错误码；不认识的码用服务端的说明", () => {
    expect(describe(new ApiError(413, "TOO_LARGE", "server text"))).toBe(`${en.error.TOO_LARGE} (TOO_LARGE)`);
    expect(describe(new ApiError(500, "SOMETHING_NEW", "server text"))).toBe("server text (SOMETHING_NEW)");
  });

  it("普通异常用它的消息，其他东西转成字符串", () => {
    expect(describe(new Error("boom"))).toBe("boom");
    expect(describe("plain")).toBe("plain");
    expect(describe(undefined)).toBe("undefined");
  });
});
