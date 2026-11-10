// 依赖方向（SPEC §3）：app → session / ui / scene / audio → model / orbit → types，下层不反过来依赖上层，
// 同一层的 ui、scene、audio 互不依赖，模块之间没有环。只读源码里的 import 语句，不执行任何模块。

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = fileURLToPath(new URL(".", import.meta.url));

/**
 * 每个单元（src 下的一级目录，或根目录下的单个文件）允许依赖的单元。没列出的依赖一律不许出现；
 * 新增单元或新增依赖方向时要改这张表，等于明确做一次决定。
 */
const ALLOWED: Record<string, readonly string[]> = {
  types: [],
  i18n: [],
  orbit: ["types"],
  model: ["orbit", "types"],
  api: ["types"],
  audio: ["model", "orbit", "types"],
  scene: ["i18n", "model", "orbit", "types"],
  ui: ["api", "i18n", "model", "orbit", "types"],
  session: ["audio", "model", "types"],
  app: ["api", "audio", "i18n", "model", "orbit", "scene", "session", "types", "ui"],
  main: ["app", "i18n"],
};

const IMPORTS = /(?:^|\n)\s*(?:import|export)\s[^;'"]*?from\s*["']([^"']+)["']|(?:^|\n)\s*import\s*["']([^"']+)["']|\bimport\(\s*["']([^"']+)["']\s*\)/g;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return name.endsWith(".ts") ? [path] : [];
  });
}

const isTestCode = (file: string) => file.endsWith(".test.ts") || file.split(sep).includes("testing");
const unitOf = (file: string) => relative(SRC, file).split(sep)[0].replace(/\.ts$/, "");
const label = (file: string) => relative(SRC, file).split(sep).join("/");

/** 这个文件 import 的项目内文件（相对路径解析成实际的 .ts 文件；样式表等非 TS 文件不算）。 */
function importsOf(file: string): string[] {
  const out: string[] = [];
  for (const match of readFileSync(file, "utf8").matchAll(IMPORTS)) {
    const spec = (match[1] ?? match[2] ?? match[3]).split("?")[0];
    if (!spec.startsWith(".")) continue;
    const base = join(dirname(file), spec);
    const found = [base, `${base}.ts`, join(base, "index.ts")].find((p) => p.endsWith(".ts") && existsSync(p));
    if (found) out.push(found);
    else if (!/\.(css|json)$/.test(spec)) throw new Error(`${label(file)} imports ${spec}, which does not resolve to a file`);
  }
  return out;
}

const production = sourceFiles(SRC).filter((file) => !isTestCode(file));
const graph = new Map(production.map((file) => [file, importsOf(file)]));

describe("依赖方向", () => {
  it("每个单元都登记在表里，表本身没有环", () => {
    expect([...new Set(production.map(unitOf))].sort()).toEqual(Object.keys(ALLOWED).sort());
    const resolved = new Set<string>();
    while (resolved.size < Object.keys(ALLOWED).length) {
      const ready = Object.keys(ALLOWED).filter((unit) => !resolved.has(unit) && ALLOWED[unit].every((dep) => resolved.has(dep)));
      expect(ready.length, `ALLOWED has a cycle among ${Object.keys(ALLOWED).filter((u) => !resolved.has(u))}`).toBeGreaterThan(0);
      for (const unit of ready) resolved.add(unit);
    }
  });

  it("各单元只依赖表里允许的单元", () => {
    const offending: string[] = [];
    for (const [file, targets] of graph) {
      for (const target of targets) {
        const [from, to] = [unitOf(file), unitOf(target)];
        if (from !== to && !ALLOWED[from]?.includes(to)) offending.push(`${label(file)} -> ${label(target)}`);
      }
    }
    expect(offending).toEqual([]);
  });

  it("正式代码不依赖测试代码", () => {
    const offending = [...graph].flatMap(([file, targets]) => targets.filter(isTestCode).map((target) => `${label(file)} -> ${label(target)}`));
    expect(offending).toEqual([]);
  });

  it("模块之间没有循环依赖", () => {
    const done = new Set<string>();
    const cycles: string[] = [];
    const visit = (file: string, trail: string[]): void => {
      if (trail.includes(file)) {
        cycles.push([...trail.slice(trail.indexOf(file)), file].map(label).join(" -> "));
        return;
      }
      if (done.has(file)) return;
      for (const target of graph.get(file) ?? []) visit(target, [...trail, file]);
      done.add(file);
    };
    for (const file of graph.keys()) visit(file, []);
    expect(cycles).toEqual([]);
  });
});
