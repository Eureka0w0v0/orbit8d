// HRTF 表：解析后端 /api/assets/hrtf.bin（格式见 docs/SPEC.md §5.6），双线性插值与 Python 版一致。

export interface HrtfTable {
  sampleRate: number;
  taps: number;
  azStepDeg: number;
  nAz: number;
  elNodes: Float64Array;
  data: Float32Array; // 布局 [el][az][ear][tap]
}

const MAGIC = "O8DH";
const FORMAT_VERSION = 1;
const HEADER_OFFSET = 8;

interface HrtfHeader {
  version: number;
  sample_rate: number;
  taps: number;
  az_step_deg: number;
  n_az: number;
  el_nodes: number[];
  layout: string;
}

export function parseHrtf(buffer: ArrayBuffer): HrtfTable {
  const bytes = new Uint8Array(buffer);
  if (String.fromCharCode(...bytes.subarray(0, 4)) !== MAGIC) throw new Error("不是 Orbit 8D 的 HRTF 数据");
  const headerLen = new DataView(buffer).getUint32(4, true);
  const header = JSON.parse(new TextDecoder().decode(bytes.subarray(HEADER_OFFSET, HEADER_OFFSET + headerLen))) as HrtfHeader;
  if (header.version !== FORMAT_VERSION || header.layout !== "el,az,ear,tap") {
    throw new Error(`不支持的 HRTF 数据版本: ${header.version}`);
  }
  const count = header.el_nodes.length * header.n_az * 2 * header.taps;
  const data = new Float32Array(buffer, HEADER_OFFSET + headerLen, count);
  return {
    sampleRate: header.sample_rate,
    taps: header.taps,
    azStepDeg: header.az_step_deg,
    nAz: header.n_az,
    elNodes: Float64Array.from(header.el_nodes),
    data,
  };
}

/** 在 (el, az) 网格上双线性插值，写入 outL / outR（长度 = taps）。 */
export function interpolateHrir(t: HrtfTable, azDeg: number, elDeg: number, outL: Float64Array, outR: Float64Array): void {
  const nodes = t.elNodes;
  const nEl = nodes.length;
  let i0 = 0;
  while (i0 < nEl - 2 && nodes[i0 + 1] <= elDeg) i0++;
  const i1 = i0 + 1;
  const tEl = Math.min(1, Math.max(0, (elDeg - nodes[i0]) / (nodes[i1] - nodes[i0])));
  const az = ((azDeg % 360) + 360) % 360;
  const u = az / t.azStepDeg;
  const j0f = Math.floor(u);
  const tAz = u - j0f;
  const j0 = ((j0f % t.nAz) + t.nAz) % t.nAz;
  const j1 = (j0 + 1) % t.nAz;
  const w00 = (1 - tEl) * (1 - tAz);
  const w01 = (1 - tEl) * tAz;
  const w10 = tEl * (1 - tAz);
  const w11 = tEl * tAz;
  const taps = t.taps;
  const stride = 2 * taps;
  const b00 = (i0 * t.nAz + j0) * stride;
  const b01 = (i0 * t.nAz + j1) * stride;
  const b10 = (i1 * t.nAz + j0) * stride;
  const b11 = (i1 * t.nAz + j1) * stride;
  const d = t.data;
  for (let k = 0; k < taps; k++) {
    outL[k] = w00 * d[b00 + k] + w01 * d[b01 + k] + w10 * d[b10 + k] + w11 * d[b11 + k];
    const r = taps + k;
    outR[k] = w00 * d[b00 + r] + w01 * d[b01 + r] + w10 * d[b10 + r] + w11 * d[b11 + r];
  }
}
