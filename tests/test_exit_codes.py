"""
test_exit_codes.py — exit kódy solveru + run_status.json (audit 1.4 / 2.11)

Konvence (čte plan_day i server / UI):
  0 OK · 1 technická chyba · 2 vadná data (validace) · 3 řešení neexistuje
Každý konec běhu zapíše run_status.json do výstupní složky, jakmile je
známá. Vše bez OSRM/OR-Tools — přes abort()/write_run_status a statickou
kontrolu, že v solveru nezůstal holý SystemExit mimo jednotné místo.
"""
import json
import re
from pathlib import Path

import pytest

import vrp_solver_lines_v6 as S
from vrp_solver_lines_v6 import (
    EXIT_DATA, EXIT_ERROR, EXIT_INFEASIBLE, EXIT_OK, RUN_CONTEXT,
    SolverAbort, abort, write_run_status,
)


@pytest.fixture
def run_ctx(tmp_path):
    saved = dict(RUN_CONTEXT)
    RUN_CONTEXT.update({"output_dir": tmp_path, "zone": "CB",
                        "delivery_date": "2026-08-17", "started": None,
                        "run_id": None})
    yield tmp_path
    RUN_CONTEXT.clear()
    RUN_CONTEXT.update(saved)


def _status(d: Path) -> dict:
    return json.loads((d / "run_status.json").read_text(encoding="utf-8"))


class TestAbortAndStatus:
    def test_status_written_on_data_error(self, run_ctx):
        with pytest.raises(SystemExit) as e:
            abort("[CHYBA] vadné řádky\n  - řádek 3 (O126115314): chybí lat", EXIT_DATA)
        assert isinstance(e.value, SolverAbort) and e.value.code == EXIT_DATA
        st = _status(run_ctx)
        assert st["status"] == "data_error" and st["exit_code"] == 2
        assert st["orders"] == ["O126115314"]
        assert st["reason"].startswith("[CHYBA] vadné řádky")
        assert st["zone"] == "CB" and st["delivery_date"] == "2026-08-17"

    def test_status_written_on_infeasible(self, run_ctx):
        with pytest.raises(SystemExit) as e:
            abort("[CHYBA] Cluster 0 seedu 'sweep' je NEŘEŠITELNÝ", EXIT_INFEASIBLE)
        assert e.value.code == 3
        assert _status(run_ctx)["status"] == "infeasible"

    def test_status_written_on_ok(self, run_ctx):
        write_run_status("ok", EXIT_OK, "plán uložen", orders=[],
                         extra={"lines_count": 12, "total_cost_kc": 80405.0})
        st = _status(run_ctx)
        assert st["status"] == "ok" and st["exit_code"] == 0
        assert st["lines_count"] == 12 and st["orders"] == []

    def test_status_written_on_unexpected_error(self, run_ctx):
        write_run_status("error", EXIT_ERROR, "[CHYBA] Neočekávaná výjimka: KeyError")
        assert _status(run_ctx)["status"] == "error"

    def test_no_output_dir_no_file_but_exit_code_kept(self):
        saved = dict(RUN_CONTEXT)
        RUN_CONTEXT.update({"output_dir": None})
        try:
            with pytest.raises(SystemExit) as e:
                abort("[CHYBA] Chybí --orders-file.", EXIT_DATA)
            assert e.value.code == EXIT_DATA
            assert write_run_status("ok", 0) is None
        finally:
            RUN_CONTEXT.clear(); RUN_CONTEXT.update(saved)

    def test_str_of_abort_is_message(self, run_ctx):
        with pytest.raises(SystemExit) as e:
            abort("konkrétní hláška", EXIT_ERROR)
        assert str(e.value) == "konkrétní hláška"

    def test_reason_skips_decoration_lines(self, run_ctx):
        write_run_status("infeasible", 3, "\n=====\n[CHYBA] skutečný důvod\n=====")
        assert _status(run_ctx)["reason"] == "[CHYBA] skutečný důvod"


class TestExitSitesUseAbort:
    """Statická pojistka: solver nesmí končit holým SystemExit/RuntimeError
    mimo abort() — jinak by plan_day dostal kód 1 a status soubor by chyběl.
    Povolené výjimky: startup testy (před známou výstupní složkou) a
    definice abort() sama."""

    def test_all_systemexit_sites_use_abort(self):
        src = Path(S.__file__).read_text(encoding="utf-8").splitlines()
        offenders = []
        for i, line in enumerate(src, 1):
            code = line.split("#", 1)[0]
            if re.search(r"\braise SystemExit\(|\braise RuntimeError\(", code):
                offenders.append(f"{i}: {line.strip()}")
            if re.search(r"\bsys\.exit\(", code) and "EXIT_ERROR" not in code:
                # jediné povolené: __main__ fallback (sys.exit(EXIT_ERROR))
                offenders.append(f"{i}: {line.strip()}")
        # startup testy: _sys.exit(1) je záměr (běží před main), povoleno
        offenders = [o for o in offenders if "_sys.exit(1)" not in o]
        assert not offenders, "holé konce mimo abort():\n" + "\n".join(offenders)

    def test_exit_code_constants(self):
        assert (EXIT_OK, EXIT_ERROR, EXIT_DATA, EXIT_INFEASIBLE) == (0, 1, 2, 3)
        import fleet_budget as fb
        assert (fb.SOLVER_EXIT_OK, fb.SOLVER_EXIT_ERROR, fb.SOLVER_EXIT_DATA,
                fb.SOLVER_EXIT_INFEASIBLE) == (0, 1, 2, 3)
