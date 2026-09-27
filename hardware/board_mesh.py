"""Mesh Waveshare's ESP32-S3-Touch-AMOLED-1.75 STEP for ball.py.

    uv run --no-project --with cadquery-ocp python hardware/board_mesh.py

Downloads Waveshare's 3D zip once and writes ``out/vendor/amoled_1_75.stl`` in
the STEP's own frame (mm): board axis +Y, cover glass front at y = 2.80, USB-C
toward -X. The model is Waveshare's, so it stays in the gitignored ``out/``.
"""

from __future__ import annotations

import io
import urllib.request
import zipfile
from pathlib import Path

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_Reader
from OCP.StlAPI import StlAPI_Writer

URL = (
    "https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.75/ESP32-S3-Touch-AMOLED-1.75-3D.zip"
)
VENDOR = Path(__file__).resolve().parent / "out" / "vendor"


def main() -> None:
    """Fetch the STEP if missing, then tessellate it into one binary STL."""
    VENDOR.mkdir(parents=True, exist_ok=True)
    step = VENDOR / "amoled_1_75.stp"
    if not step.exists():
        with urllib.request.urlopen(URL) as resp:  # noqa: S310 - fixed https URL
            archive = zipfile.ZipFile(io.BytesIO(resp.read()))
        name = next(n for n in archive.namelist() if n.lower().endswith((".stp", ".step")))
        step.write_bytes(archive.read(name))
    reader = STEPControl_Reader()
    if reader.ReadFile(str(step)) != IFSelect_RetDone:
        msg = f"cannot read {step}"
        raise SystemExit(msg)
    reader.TransferRoots()
    shape = reader.OneShape()
    BRepMesh_IncrementalMesh(shape, 0.1, False, 0.5, True)  # noqa: FBT003 - positional-only binding
    writer = StlAPI_Writer()
    writer.ASCIIMode = False
    writer.Write(shape, str(VENDOR / "amoled_1_75.stl"))


main()
