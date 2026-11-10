"""读 SOFA 文件（HDF5）里的 HRTF 测量数据（SPEC §5.6）。只读出原样的数组，换算成网格在 engine/hrtf.py。"""

from pathlib import Path

import h5py
import numpy as np

from orbit8d.engine.hrtf import SofaData


def read_sofa(path: Path) -> SofaData:
    with h5py.File(path, "r") as f:
        return SofaData(
            name=path.name,
            ir=np.array(f["Data.IR"], dtype=np.float64),  # (M, 2, N)
            sample_rate=int(round(float(np.array(f["Data.SamplingRate"]).ravel()[0]))),
            source_positions=np.array(f["SourcePosition"], dtype=np.float64),
            receiver_positions=np.array(f["ReceiverPosition"], dtype=np.float64),
        )
