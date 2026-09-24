"""回归护栏：Python 实现必须仍与已提交的黄金数据一致（TS 端用同一份数据校验）。"""
import json

import numpy as np

from orbit8d.engine.orbit import OrbitParams, orbit_position
from tests.make_golden import ORBIT_FILE


def test_orbit_matches_committed_golden_vectors():
    assert ORBIT_FILE.exists(), "缺少黄金数据：运行 uv run python -m tests.make_golden"
    data = json.loads(ORBIT_FILE.read_text())
    assert data["version"] == 1 and len(data["cases"]) >= 200
    worst = 0.0
    for case in data["cases"]:
        az, el, dist = orbit_position(OrbitParams(**case["params"]), np.array(case["t"]), case["t_ref"],
                                      case["offset_deg"])
        d_az = np.abs((az - np.array(case["az"]) + 180.0) % 360.0 - 180.0)
        worst = max(worst, d_az.max(), np.abs(el - np.array(case["el"])).max(),
                    np.abs(dist - np.array(case["dist"])).max())
    assert worst < 1e-9
