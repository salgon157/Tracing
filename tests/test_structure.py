"""
test_structure.py — strukturní pojistky repozitáře (bez spouštění logiky).

  1. requirements.txt pokrývá každý import třetí strany v projektových
     skriptech (audit 1.9: scipy chyběl → driver_assignment na čerstvém
     serveru spadne až v den nasazení).
"""
import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# lokální moduly projektu (top-level .py) — nejsou z pip
LOCAL_MODULES = {p.stem for p in ROOT.glob("*.py")} | {"webui", "tests"}

# jméno importu → jméno balíčku v requirements (kde se liší)
IMPORT_TO_PKG = {
    "sklearn": "scikit-learn",
    "ortools": "ortools",
    "yaml": "pyyaml",
    "PIL": "pillow",
    "dateutil": "python-dateutil",
    "multipart": "python-multipart",
}


def _stdlib(name: str) -> bool:
    return name in sys.stdlib_module_names


def _third_party_imports(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return {n for n in found if not _stdlib(n) and n not in LOCAL_MODULES}


def _requirements_pkgs() -> set[str]:
    pkgs = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pkgs.add(re.split(r"[<>=!~\[ ]", line, maxsplit=1)[0].lower())
    return pkgs


class TestRequirementsCoverage:
    def test_requirements_cover_third_party_imports(self):
        pkgs = _requirements_pkgs()
        missing = {}
        for py in sorted(ROOT.glob("*.py")):
            if "(DoNotUse)" in py.name:
                continue
            for imp in _third_party_imports(py):
                pkg = IMPORT_TO_PKG.get(imp, imp).lower()
                if pkg not in pkgs:
                    missing.setdefault(pkg, set()).add(py.name)
        assert not missing, (
            "Importy třetích stran bez řádku v requirements.txt: "
            + "; ".join(f"{p} ({', '.join(sorted(f))})" for p, f in sorted(missing.items())))

    def test_scipy_is_declared(self):
        # konkrétní regres auditu 1.9
        assert "scipy" in _requirements_pkgs()
