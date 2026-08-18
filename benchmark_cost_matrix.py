"""
benchmark_cost_matrix.py — legacy vs exact nákladová matice (audit 1.1 + 2.9 + 1.6)
==================================================================================

Tenký obal nad regression_ab.py: obě strany = TÝŽ solver (pracovní kopie),
liší se jen `--cost-matrix-mode legacy` (A) vs `exact` (B). ABAB, stejné
prepared soubory, vozový park, routing instance, budget. Metrika =
SKUTEČNÁ cena (Σ km × přesná sazba + Σ start), linky, km, čas.

    python benchmark_cost_matrix.py --dates 2026-08-07 2026-08-10 2026-08-13 2026-08-17 \
        --reps 3 --budget 5

Rozhodnutí o defaultu (CONFIG cost_matrix_mode): exact jen když projde
stejné přísné kritérium jako regrese (medián ≤ +1 %, max ≤ +2 %, žádný
běh > nejlepší A +3 %, linky ≤). Jinak zůstává legacy a nález se uzavře
jako „ověřeno, ponecháno".

Pozn.: legacy vykazuje kamionu (hgv profil) km z osobní matice, exact
z hgv — regression_ab proto u OBOU stran přeceňuje kamionové linky hgv km
z ORS (stejný metr), takže rozdíl v ceně je jen rozdíl v trasách, ne v účtu.
Vypnout: --no-reprice-hgv (pak by legacy měl výhodu).
"""
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = [sys.executable, "regression_ab.py",
           "--baseline-dir", Path.cwd().as_posix(),
           "--candidate-dir", Path.cwd().as_posix(),
           "--a-args", "--cost-matrix-mode legacy",
           "--b-args", "--cost-matrix-mode exact",
           "--label-a", "legacy", "--label-b", "exact"]
    if not any(a.startswith("--out") for a in args):
        from datetime import datetime
        cmd += ["--out", (Path("data/results/_bench_cost_matrix")
                          / datetime.now().strftime("%Y%m%d_%H%M")).as_posix()]
    sys.exit(subprocess.run(cmd + args).returncode)
