"""生成跨语言一致性测试数据：uv run python -m tests.make_golden

输出到仓库根目录 shared/golden/。Python 实现是基准，TS 实现必须与之逐点一致。
改动算法后需要重新生成并在提交说明里写明原因。
"""

import json
from pathlib import Path

import numpy as np

from orbit8d.engine.orbit import SHAPES, OrbitParams, orbit_position

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "shared" / "golden"
ORBIT_FILE = GOLDEN_DIR / "orbit_vectors.json"
CASES_PER_SHAPE = 40
SEED = 8


def orbit_cases(seed: int = SEED) -> list[dict]:
    rng = np.random.default_rng(seed)
    cases = []
    for shape in SHAPES:
        for _ in range(CASES_PER_SHAPE):
            p = OrbitParams(
                shape=shape,
                radius_m=float(rng.uniform(0.5, 4.0)),
                period_s=float(rng.uniform(2.0, 30.0)),
                direction=int(rng.choice([1, -1])),
                start_deg=float(rng.uniform(-360, 360)),
                height_deg=float(rng.uniform(-60, 60)),
                pitch_deg=float(rng.uniform(-90, 90)),
                roll_deg=float(rng.uniform(-90, 90)),
                yaw_deg=float(rng.uniform(-180, 180)),
                aspect=float(rng.uniform(0.3, 1.0)),
                swing_deg=float(rng.uniform(10, 180)),
                lift_deg=float(rng.uniform(0, 60)),
            )
            t = rng.uniform(0, 300, size=3)
            t_ref = float(rng.uniform(0, 10))
            offset = float(rng.choice([0.0, -20.0, 20.0, -45.0]))
            az, el, dist = orbit_position(p, t, t_ref, offset)
            cases.append(
                {
                    "params": p.__dict__,
                    "t": t.tolist(),
                    "t_ref": t_ref,
                    "offset_deg": offset,
                    "az": az.tolist(),
                    "el": el.tolist(),
                    "dist": dist.tolist(),
                }
            )
    return cases


def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    ORBIT_FILE.write_text(json.dumps({"version": 1, "cases": orbit_cases()}, indent=1) + "\n")
    print(f"wrote {ORBIT_FILE}")


if __name__ == "__main__":
    main()
