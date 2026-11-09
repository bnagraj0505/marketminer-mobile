"""Pack every phone-engine Python file (py/files.json) into py/bundle.json, so the browser downloads the analysis
engine's code in ONE request instead of ~65. Run after changing anything under py/:   python tools/bundle_py.py"""
import json
from pathlib import Path

PY = Path(__file__).resolve().parents[1] / "py"
files = sorted(str(p.relative_to(PY)).replace("\\", "/") for p in PY.rglob("*.py") if "__pycache__" not in p.parts)
(PY / "files.json").write_text(json.dumps(files, indent=0), encoding="utf-8")
(PY / "bundle.json").write_text(json.dumps({f: (PY / f).read_text(encoding="utf-8") for f in files}, separators=(",", ":")), encoding="utf-8")
print(len(files), "files,", round((PY / "bundle.json").stat().st_size / 1024), "KB")
