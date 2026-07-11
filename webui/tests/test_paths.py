"""
Bezpečnost cest — resolve_results_path nesmí pustit ven z data/results.

Spouštět explicitně:  python -m pytest webui/tests -q
"""

import pytest

from webui.app import config
from webui.app.scan import resolve_results_path, UnsafePath


@pytest.mark.parametrize("bad", [
    "..",
    "../run_log.jsonl",
    "../../vrp_solver_lines_v6.py",
    "CB/../../..",
    "..\\..\\vrp_solver_lines_v6.py",   # zpětná lomítka
    "/etc/passwd",                       # absolutní POSIX
    "C:\\Windows\\system32",             # disk Windows
    "C:/Windows",                        # disk s dopřednými lomítky
    "",                                  # prázdná
    "   ",                               # jen mezery
])
def test_rejects_unsafe(bad):
    with pytest.raises(UnsafePath):
        resolve_results_path(bad)


@pytest.mark.parametrize("good", [
    "CB",
    "CB/2026-04-10",
    "ALL/2026-04-29_budget5min",
    "ALL_BENCHMARK/session_x/variant_y",
    "CB/Zaloha 2026-04-29",              # mezery v názvu jsou OK
])
def test_accepts_safe_relative(good):
    p = resolve_results_path(good)
    root = config.RESULTS_ROOT.resolve()
    assert p == root or p.is_relative_to(root)


def test_decoded_dotdot_is_blocked():
    # Web vrstva URL-dekóduje %2e%2e na '..', do funkce dorazí už '..'.
    with pytest.raises(UnsafePath):
        resolve_results_path("a/../../../etc")
