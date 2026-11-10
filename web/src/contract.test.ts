// 前后端契约：shared/golden/contract.json 是后端常量的快照（backend/tests/make_golden.py 生成，test_contract.py 校验），
// 这里断言 TS 的常量、类型与 i18n 和它一致。联合类型、接口在运行时不存在，所以用 `satisfies Record<类型, true>`
// 列成员清单：类型多一个或少一个成员都编译不过（npm run typecheck），清单再在运行时与 contract.json 比对。
// TS 侧没有导出的副本（如 audio/engine.ts 的 STEM_LAYOUT）暂不断言，导出后再补进来。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { SAMPLE_RATE, STEM_NAMES } from "./audio/engine";
import { INPUT_CHANNELS } from "./audio/params";
import { DICTIONARIES, LANGS, errorText } from "./i18n";
import { MAX_EVENTS, MAX_SECTIONS } from "./scene/edit";
import { LAYERS, type LayerName } from "./scene/layers";
import type {
  Analysis,
  Bars,
  EventKind,
  ExportFormat,
  ExportRecord,
  ExportState,
  Loudness,
  Mix,
  Orbit,
  Project,
  ProjectState,
  Room,
  RoomName,
  Scene,
  SceneEvent,
  Section,
  Shape,
  SourceInfo,
  Speed,
  TrackName,
} from "./types";
import { HOLD_RAMP_S, TRACKS } from "./types";
import { BUSY, MODEL_DOWNLOAD_STEP } from "./ui/progress";
import type { SchemaDef } from "./ui/schema";

interface ObjectSchema {
  properties: Record<string, { enum?: Array<string | number> }>;
}

interface Contract {
  version: number;
  tracks: string[];
  export_formats: string[];
  loudness: string[];
  project_states: string[];
  export_states: string[];
  project_steps: string[];
  source_channels: string[];
  preview_files: Record<string, number[]>;
  sample_rate: number;
  hold_ramp_s: number;
  layer_heights: Record<string, number>;
  limits: { max_sections: number; max_events: number };
  error_codes: string[];
  record_fields: Record<"project" | "source" | "analysis" | "export", string[]>;
  scene_schema: ObjectSchema & { $defs: Record<string, ObjectSchema> }; // = GET /api/scene/schema
}

const file = fileURLToPath(new URL("../../shared/golden/contract.json", import.meta.url));
const contract = JSON.parse(readFileSync(file, "utf8")) as Contract;
const schema = contract.scene_schema;

/** 成员清单的键（数字键也是字符串），排好序。 */
const keys = (members: object) => Object.keys(members).sort();
const sorted = (values: ReadonlyArray<string | number>) => values.map(String).sort();

const TRACK_NAMES = { vocals: true, drums: true, bass: true, other: true } satisfies Record<TrackName, true>;
const EXPORT_FORMATS = { m4a: true, mp3: true, flac: true, wav: true, ogg: true } satisfies Record<ExportFormat, true>;
const LOUDNESS = { loud: true, standard: true } satisfies Record<Loudness, true>;
const PROJECT_STATES = {
  UPLOADED: true,
  DECODING: true,
  SEPARATING: true,
  ANALYZING: true,
  READY: true,
  FAILED: true,
} satisfies Record<ProjectState, true>;
const EXPORT_STATES = { QUEUED: true, RENDERING: true, ENCODING: true, DONE: true, FAILED: true } satisfies Record<ExportState, true>;
const LAYER_NAMES = { surround: true, height: true, top: true } satisfies Record<LayerName, true>;
const SCHEMA_DEFS = { Orbit: true, Mix: true, Section: true, Speed: true, Event: true } satisfies Record<SchemaDef, true>;

/** 场景里的枚举字段（"定义名.字段名"）→ TS 联合类型的成员清单。 */
const SCENE_ENUMS: Record<string, object> = {
  "Orbit.shape": { circle: true, ellipse: true, pendulum: true, figure8: true, spiral: true, fixed: true } satisfies Record<Shape, true>,
  "Orbit.direction": { cw: true, ccw: true } satisfies Record<Orbit["direction"], true>,
  "Speed.mode": { bars: true, seconds: true } satisfies Record<Speed["mode"], true>,
  "Speed.bars": { 1: true, 2: true, 4: true, 8: true } satisfies Record<Bars, true>,
  "Event.kind": { hold: true, overhead: true } satisfies Record<EventKind, true>,
  "Room.name": { room: true, hall: true, church: true } satisfies Record<RoomName, true>,
};

/** 场景各层的字段名：键是 /api/scene/schema 里的定义名（"Scene" 是顶层）。 */
const SCENE_FIELDS: Record<string, object> = {
  Scene: { version: true, mix: true, sections: true, events: true, room: true, rear_darken_db: true } satisfies Record<keyof Scene, true>,
  Section: { start_s: true, label: true, orbits: true, wet_db: true } satisfies Record<keyof Section, true>,
  Orbit: {
    shape: true,
    radius_m: true,
    speed: true,
    direction: true,
    start_deg: true,
    height_deg: true,
    pitch_deg: true,
    roll_deg: true,
    yaw_deg: true,
    aspect: true,
    swing_deg: true,
    lift_deg: true,
  } satisfies Record<keyof Orbit, true>,
  Speed: { mode: true, bars: true, seconds: true } satisfies Record<keyof Speed, true>,
  Mix: { gain_db: true, width_deg: true, reverb_send: true, mute: true, solo: true } satisfies Record<keyof Mix, true>,
  Event: { t_s: true, kind: true, duration_s: true, targets: true } satisfies Record<keyof SceneEvent, true>,
  Room: { name: true } satisfies Record<keyof Room, true>,
};

/** 前端从接口记录里读的字段（TS 接口的键）。后端记录可以多出前端不用的字段，但前端读的每一个都必须存在。 */
const RECORD_FIELDS: Record<keyof Contract["record_fields"], object> = {
  project: {
    id: true,
    state: true,
    progress: true,
    step: true,
    error: true,
    source: true,
    analysis: true,
    preview_scale: true,
  } satisfies Record<keyof Project, true>,
  source: {
    filename: true,
    size: true,
    duration_s: true,
    title: true,
    artist: true,
    album: true,
    has_cover: true,
  } satisfies Record<keyof SourceInfo, true>,
  analysis: {
    sample_rate: true,
    duration_s: true,
    bpm: true,
    bpm_norm: true,
    default_bars: true,
    t_ref: true,
    calibration: true,
    preview_gain: true,
    match_eq_db: true,
    spectra: true,
    sections: true,
    original_gain: true,
    envelope_db: true,
    envelope_hop_s: true,
    version: true,
  } satisfies Record<keyof Analysis, true>,
  export: {
    id: true,
    project_id: true,
    state: true,
    format: true,
    loudness: true,
    error: true,
    file_name: true,
  } satisfies Record<keyof ExportRecord, true>,
};

const propertiesOf = (def: string) => (def === "Scene" ? schema.properties : schema.$defs[def].properties);
const schemaDefs = () => ["Scene", ...Object.keys(schema.$defs)];

/** schema 里所有直接带 enum 的字段（"定义名.字段名" → 取值）。 */
function schemaEnums(): Record<string, Array<string | number>> {
  const out: Record<string, Array<string | number>> = {};
  for (const def of schemaDefs()) {
    for (const [name, prop] of Object.entries(propertiesOf(def))) if (prop.enum) out[`${def}.${name}`] = prop.enum;
  }
  return out;
}

describe("前后端契约（shared/golden/contract.json）", () => {
  it("格式版本", () => {
    expect(contract.version).toBe(1);
  });

  it("音轨：TRACKS 的顺序与后端一致，TrackName 不多不少", () => {
    expect([...TRACKS]).toEqual(contract.tracks);
    expect(keys(TRACK_NAMES)).toEqual(sorted(contract.tracks));
  });

  it("导出格式、响度、项目与导出的状态", () => {
    expect(keys(EXPORT_FORMATS)).toEqual(sorted(contract.export_formats));
    expect(keys(LOUDNESS)).toEqual(sorted(contract.loudness));
    expect(keys(PROJECT_STATES)).toEqual(sorted(contract.project_states));
    expect(keys(EXPORT_STATES)).toEqual(sorted(contract.export_states));
  });

  it("处理中的状态与“下载模型”这个子步骤", () => {
    expect([...BUSY].sort()).toEqual(contract.project_states.filter((s) => s !== "READY" && s !== "FAILED").sort());
    expect(contract.project_steps).toEqual([MODEL_DOWNLOAD_STEP]);
  });

  it("场景的枚举字段（形状、转动方向、速度模式、小节数、事件、房间）与 /api/scene/schema 一致", () => {
    const enums = schemaEnums();
    expect(Object.keys(SCENE_ENUMS).sort()).toEqual(Object.keys(enums).sort());
    for (const [field, members] of Object.entries(SCENE_ENUMS)) expect(keys(members), field).toEqual(sorted(enums[field]));
  });

  it("场景各层的字段名与 /api/scene/schema 的 properties 一致，界面读范围用的定义名都在", () => {
    expect(Object.keys(SCENE_FIELDS).sort()).toEqual(schemaDefs().sort());
    for (const [def, fields] of Object.entries(SCENE_FIELDS)) expect(keys(fields), def).toEqual(Object.keys(propertiesOf(def)).sort());
    for (const def of Object.keys(SCHEMA_DEFS)) expect(Object.keys(schema.$defs), def).toContain(def);
  });

  it("接口记录：前端读的字段后端都有；分析结果的字段两边完全一致", () => {
    expect(Object.keys(RECORD_FIELDS).sort()).toEqual(Object.keys(contract.record_fields).sort());
    for (const [record, fields] of Object.entries(RECORD_FIELDS)) {
      expect(contract.record_fields[record as keyof typeof RECORD_FIELDS], record).toEqual(expect.arrayContaining(keys(fields)));
    }
    expect(keys(RECORD_FIELDS.analysis)).toEqual([...contract.record_fields.analysis].sort());
  });

  it("试听：9 声道顺序、要下载的试听文件、采样率", () => {
    expect([...INPUT_CHANNELS]).toEqual(contract.source_channels);
    expect([...STEM_NAMES].sort()).toEqual(Object.keys(contract.preview_files).sort());
    expect(SAMPLE_RATE).toBe(contract.sample_rate);
  });

  it("停顿的减速时长、三层的一键高度、段数与事件数上限", () => {
    expect(HOLD_RAMP_S).toBe(contract.hold_ramp_s);
    expect(keys(LAYER_NAMES)).toEqual(Object.keys(contract.layer_heights).sort());
    expect(Object.fromEntries(LAYERS.map((l) => [l.name, l.center]))).toEqual(contract.layer_heights);
    expect(MAX_SECTIONS).toBe(contract.limits.max_sections);
    expect(MAX_EVENTS).toBe(contract.limits.max_events);
  });
});

/** 后端会返回、但界面不会拿来显示的错误码，所以 i18n 里没有。哪个有了翻译就从这里删掉。 */
const UNTRANSLATED: Record<string, string> = {
  BAD_BARS: "只在 /api/presets/{name}?bars= 不是 1/2/4/8 时返回；前端只用分析给出的 default_bars 调用",
  NO_SAVED_SCENE: "api.savedScene 把这个 404 当成“还没保存过”返回 null，改用自动编排，不显示",
  ASSET_DOWNLOAD_FAILED: "只在下载 HRTF（服务启动时，失败则服务起不来）与分轨模型时出现；后者包装成 MODEL_DOWNLOAD_FAILED",
};
const NO_TRANSLATION = "(no translation)"; // errorText 不认识这个码时原样返回的兜底文字

describe("错误码", () => {
  it("后端的每个错误码在三种语言里都有提示（*_TIMEOUT 统一显示 error.TIMEOUT）", () => {
    for (const code of contract.error_codes.filter((c) => !(c in UNTRANSLATED))) {
      for (const l of LANGS) expect(errorText(code, NO_TRANSLATION, DICTIONARIES[l]), `${l}: ${code}`).not.toBe(NO_TRANSLATION);
    }
  });

  it("白名单里都是后端真有、而且确实还没翻译的码", () => {
    for (const code of Object.keys(UNTRANSLATED)) {
      expect(contract.error_codes, code).toContain(code);
      for (const l of LANGS) expect(errorText(code, NO_TRANSLATION, DICTIONARIES[l]), `${l}: ${code}`).toBe(NO_TRANSLATION);
    }
  });
});
