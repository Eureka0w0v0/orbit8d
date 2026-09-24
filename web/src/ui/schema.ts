// 从后端 /api/scene/schema（pydantic 生成的 JSON Schema）读取参数范围，前端不再重复定义白名单。

export interface Range {
  min: number;
  max: number;
}

interface PropSchema {
  minimum?: number;
  maximum?: number;
}

interface SceneSchema {
  properties: Record<string, PropSchema>;
  $defs: Record<string, { properties: Record<string, PropSchema> }>;
}

export type SchemaDef = "Orbit" | "Mix" | "Section" | "Speed" | "Event";

export class SchemaRanges {
  constructor(private readonly schema: SceneSchema) {}

  static from(raw: Record<string, unknown>): SchemaRanges {
    const s = raw as unknown as SceneSchema;
    if (!s.$defs || !s.properties) throw new Error("Unexpected scene schema format");
    return new SchemaRanges(s);
  }

  /** def 为 null 表示场景顶层字段。 */
  of(def: SchemaDef | null, prop: string): Range {
    const props = def ? this.schema.$defs[def]?.properties : this.schema.properties;
    const p = props?.[prop];
    if (p?.minimum === undefined || p.maximum === undefined) throw new Error(`Scene schema has no range for ${def ?? "Scene"}.${prop}`);
    return { min: p.minimum, max: p.maximum };
  }
}
