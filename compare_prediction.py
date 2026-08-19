"""
compare_prediction.py — porovnání predikčních běhů s ostrými (realita)
=======================================================================

Jediný vlastník porovnávacích vzorců. Čte oba run logy, páruje běhy přes
(depo, datum doručení) a zapisuje strojově čitelný výstup, který konzumuje
terminál, webui i budoucí integrace (server/Eso9) — nikdo nic nepřepočítává.

  predikce: data/prediction/results/run_log.jsonl
  realita:  data/results/run_log.jsonl
  výstup:   data/prediction/results/comparison.jsonl  (1 řádek per depo+datum,
            opakované porovnání týž den záznam nahradí — vyhrává poslední volba)

Použití:
  python compare_prediction.py                     # vše, poslední predikce ke každému dni
  python compare_prediction.py --date 2026-07-15   # jen jeden den
  python compare_prediction.py --pred-stamp 1811   # vybrat KTEROU predikci porovnat
  python compare_prediction.py --depots CB,MO
  python compare_prediction.py --list              # co je k dispozici (žádné porovnání)
  python compare_prediction.py --no-write          # jen zobrazit, nezapisovat

Konvence: Δ = predikce − realita (kladné číslo = predikce nadstřelila).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

PREDICTION_LOG  = Path("data/prediction/results/run_log.jsonl")
REAL_LOG        = Path("data/results/run_log.jsonl")
COMPARISON_PATH = Path("data/prediction/results/comparison.jsonl")
VEHICLE_TYPES   = None      # None = nejnovější vehicle_types-YYYYMMDD.csv

# Poslední segment output_dir: {YYYY-MM-DD}, {YYYY-MM-DD}_{HHMM} nebo
# {YYYY-MM-DD}_{HHMM}_{label} (predict_day --label pro porovnávací běhy).
# Bez volitelného labelu by se označené predikce nespárovaly s realitou.
_DIR_DATE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})(?:_(\d{4}))?(?:_(?P<label>[^\\/]+))?$")


# ────────────────────────────────────────────────────────────────
#  Čtení run logů
# ────────────────────────────────────────────────────────────────

def load_run_log(path: Path) -> list[dict]:
    """Tolerantní čtení JSONL — vadné řádky přeskočí."""
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _dir_leaf(rec: dict) -> str:
    out = str(rec.get("results", {}).get("output_dir", ""))
    return out.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def rec_zone(rec: dict) -> str:
    return str(rec.get("input", {}).get("zone", "")).strip()


def rec_date(rec: dict) -> str:
    """Datum doručení; starší predikční záznamy ho mají prázdné —
    fallback na parsování z output_dir (.../CB/2026-07-15_1811)."""
    date = str(rec.get("input", {}).get("delivery_date", "")).strip()
    if date:
        return date
    m = _DIR_DATE_RE.search(_dir_leaf(rec))
    return m.group(1) if m else ""


def rec_stamp(rec: dict) -> str | None:
    """HHMM razítko predikční session z output_dir; ostré běhy ho nemají."""
    m = _DIR_DATE_RE.search(_dir_leaf(rec))
    return m.group(2) if m and m.group(2) else None


def rec_label(rec: dict) -> str | None:
    """
    Label z output_dir: u plan_day predict 'P1'/'P2', u ručních
    porovnávacích běhů co si kdo zadal (`b5`, `s-koeficientem`).
    Starší predikce (před plan_day) ho nemají → None.
    """
    m = _DIR_DATE_RE.search(_dir_leaf(rec))
    label = m.group("label") if m else None
    return label or None


def group_by_zone_date(records: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Seskupí per (zone, date), uvnitř seřazené podle run_id (= času běhu)."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        zone, date = rec_zone(rec), rec_date(rec)
        if not zone or not date:
            continue
        groups.setdefault((zone, date), []).append(rec)
    for runs in groups.values():
        runs.sort(key=lambda r: str(r.get("run_id", "")))
    return groups


def select_run(runs: list[dict], stamp: str | None = None,
               label: str | None = None) -> dict | None:
    """
    Poslední běh; filtry zužují výběr, ne pořadí.

    `stamp` = HHMM session, `label` = fáze / popisek z názvu složky
    ('P1', 'P2'). Bez labelu vyhrává poslední běh v čase, což je u
    plan_day vždycky P2 — ale jen náhodou (P2 běží po P1). Kdo chce
    jistotu, ať `label` zadá.
    """
    if stamp is not None:
        runs = [r for r in runs if rec_stamp(r) == stamp]
    if label is not None:
        runs = [r for r in runs if (rec_label(r) or "").upper() == label.upper()]
    return runs[-1] if runs else None


# ────────────────────────────────────────────────────────────────
#  Typy vozidel — agregace malá/velká
# ────────────────────────────────────────────────────────────────

def load_type_profiles(path: Path | None = VEHICLE_TYPES) -> dict[str, str]:
    """
    type_name -> 'mala' | 'velka'. Primárně podle sloupce `profiles` ve
    vozovém parku; když sloupec chybí (export od 20. 8. 2026 ho nenese),
    podle nosnosti jako fleet_budget (max_kg <= SMALL_MAX_KG = malé).

    Bez cesty se vezme nejnovější vehicle_types-YYYYMMDD.csv. Chybějící
    soubor není fatální — porovnání pak jen neagreguje malá/velká.
    """
    profiles: dict[str, str] = {}
    if path is None:
        try:
            from vrp_solver_lines_v6 import find_vehicle_types_file
            path = find_vehicle_types_file()
        except (ImportError, FileNotFoundError):
            return profiles
    if not path.exists():
        return profiles
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            name = str(row.get("type_name", "")).strip()
            prof = str(row.get("profiles", "") or "").strip().lower()
            if not name:
                continue
            if prof:
                small = "mal" in prof
            else:
                from fleet_budget import SMALL_MAX_KG
                try:
                    small = float(row.get("max_kg", "") or "inf") <= SMALL_MAX_KG
                except ValueError:
                    small = False
            # víc typů se stejným type_name (do 3t: 1 200 i 1 350 kg) — stačí,
            # když je některý z nich malý, jinak by "do 3t" bylo velké
            profiles[name] = "mala" if (small or profiles.get(name) == "mala") else "velka"
    return profiles


def aggregate_mix(mix: dict, profiles: dict[str, str]) -> dict[str, int]:
    """vehicle_type_mix -> {'mala': N, 'velka': M} (neznámý typ počítá jako velký)."""
    out = {"mala": 0, "velka": 0}
    for type_name, count in (mix or {}).items():
        out[profiles.get(type_name, "velka")] += int(count)
    return out


# ────────────────────────────────────────────────────────────────
#  Porovnání
# ────────────────────────────────────────────────────────────────

def excluded_for(rec: dict) -> int | None:
    """Kolik objednávek prepare vyřadil (chybějící lokace + vadná okna).
    Čte prepare_stats_*.json vedle orders souboru; pro starší běhy fallback
    na počet řádků missing_locs (bez vadných oken). None = nezjistitelné."""
    orders_file = str(rec.get("input", {}).get("orders_file", "")).replace("\\", "/")
    zone, date = rec_zone(rec), rec_date(rec)
    if not orders_file or not zone or not date:
        return None
    prepared_dir = Path(orders_file).parent
    stats_path = prepared_dir / f"prepare_stats_{zone}_{date}.json"
    if stats_path.exists():
        try:
            return int(json.loads(stats_path.read_text(encoding="utf-8"))
                       ["excluded_total"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return None
    missing_path = prepared_dir / f"missing_locs_{zone}_{date}.txt"
    if missing_path.exists():
        try:
            return sum(1 for line in missing_path.read_text(encoding="utf-8")
                       .splitlines() if line.strip() and not line.startswith("#"))
        except OSError:
            return None
    return None


def _side(rec: dict, profiles: dict[str, str]) -> dict:
    inp, res = rec.get("input", {}), rec.get("results", {})
    agg = aggregate_mix(res.get("vehicle_type_mix", {}), profiles)
    return {
        "excluded":         excluded_for(rec),
        "run_id":           rec.get("run_id"),
        "stamp":            rec_stamp(rec),
        "label":            rec_label(rec),
        "orders_count":     inp.get("orders_count", 0),
        "orders_total_kg":  inp.get("orders_total_kg", 0),
        "lines_count":      res.get("lines_count", 0),
        "vehicle_type_mix": res.get("vehicle_type_mix", {}),
        "mala":             agg["mala"],
        "velka":            agg["velka"],
        "total_cost_kc":    res.get("total_cost_kc", 0),
        "total_km":         res.get("total_km", 0),
        "output_dir":       res.get("output_dir", ""),
    }


def build_comparison(pred: dict, real: dict, profiles: dict[str, str]) -> dict:
    """Jeden záznam porovnání. Δ = predikce − realita."""
    p, r = _side(pred, profiles), _side(real, profiles)
    all_types = sorted(set(p["vehicle_type_mix"]) | set(r["vehicle_type_mix"]))
    return {
        "date":        rec_date(real),
        "zone":        rec_zone(real),
        "compared_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "prediction":  p,
        "real":        r,
        "delta": {
            "lines":    p["lines_count"] - r["lines_count"],
            "mala":     p["mala"] - r["mala"],
            "velka":    p["velka"] - r["velka"],
            "orders":   p["orders_count"] - r["orders_count"],
            "kg":       round(p["orders_total_kg"] - r["orders_total_kg"], 1),
            "cost_kc":  round(p["total_cost_kc"] - r["total_cost_kc"], 0),
            "vehicle_types": {
                t: p["vehicle_type_mix"].get(t, 0) - r["vehicle_type_mix"].get(t, 0)
                for t in all_types
            },
        },
    }


def upsert_comparisons(path: Path, new_records: list[dict]) -> None:
    """Nahradí záznamy se stejným (zone, date), zbytek zachová. Přepíše soubor."""
    existing = load_run_log(path)   # stejný tolerantní JSONL formát
    replaced_keys = {(r["zone"], r["date"]) for r in new_records}
    kept = [r for r in existing
            if (r.get("zone"), r.get("date")) not in replaced_keys]
    merged = kept + new_records
    merged.sort(key=lambda r: (str(r.get("date", "")), str(r.get("zone", ""))))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ────────────────────────────────────────────────────────────────
#  Výpis
# ────────────────────────────────────────────────────────────────

def _pr(p, r, d, width=12) -> str:
    return f"{p}/{r} ({d:+d})".ljust(width)


def format_report(comparisons: list[dict]) -> str:
    lines = ["", "PREDIKCE vs. REALITA    (delta = predikce - realita)",
             "=" * 78]
    by_date: dict[str, list[dict]] = {}
    for c in comparisons:
        by_date.setdefault(c["date"], []).append(c)

    for date in sorted(by_date):
        day = sorted(by_date[date], key=lambda c: c["zone"])
        lines.append(f"\nDatum doručení: {date}")
        lines.append(f"{'depo':<6}{'predikce':<16}{'trasy P/R':<14}{'mala P/R':<14}"
                     f"{'velka P/R':<13}{'obj P/R':<12}{'vyraz P/R':<11}"
                     f"{'cena P/R [Kc]':<22}")
        lines.append("-" * 89)
        tot = {"pl": 0, "rl": 0, "pm": 0, "rm": 0, "pv": 0, "rv": 0,
               "po": 0, "ro": 0, "px": 0, "rx": 0, "pc": 0.0, "rc": 0.0}
        for c in day:
            p, r, d = c["prediction"], c["real"], c["delta"]
            def _x(v):
                return "?" if v is None else str(v)
            # razítko + fáze (P1/P2), ať není potřeba hádat, co se porovnává
            _src = (p["stamp"] or "-") + (f" {p['label']}" if p.get("label") else "")
            lines.append(
                f"{c['zone']:<6}"
                f"{_src:<16}"
                f"{_pr(p['lines_count'], r['lines_count'], d['lines'], 14)}"
                f"{_pr(p['mala'], r['mala'], d['mala'], 14)}"
                f"{_pr(p['velka'], r['velka'], d['velka'], 13)}"
                f"{str(p['orders_count']) + '/' + str(r['orders_count']):<12}"
                f"{_x(p['excluded']) + '/' + _x(r['excluded']):<11}"
                f"{p['total_cost_kc']:,.0f} / {r['total_cost_kc']:,.0f} ({d['cost_kc']:+,.0f})"
            )
            tot["pl"] += p["lines_count"];  tot["rl"] += r["lines_count"]
            tot["pm"] += p["mala"];         tot["rm"] += r["mala"]
            tot["pv"] += p["velka"];        tot["rv"] += r["velka"]
            tot["po"] += p["orders_count"]; tot["ro"] += r["orders_count"]
            tot["px"] += p["excluded"] or 0; tot["rx"] += r["excluded"] or 0
            tot["pc"] += p["total_cost_kc"]; tot["rc"] += r["total_cost_kc"]
        lines.append("-" * 89)
        lines.append(
            f"{'CELKEM':<22}"
            f"{_pr(tot['pl'], tot['rl'], tot['pl'] - tot['rl'], 14)}"
            f"{_pr(tot['pm'], tot['rm'], tot['pm'] - tot['rm'], 14)}"
            f"{_pr(tot['pv'], tot['rv'], tot['pv'] - tot['rv'], 13)}"
            f"{str(tot['po']) + '/' + str(tot['ro']):<12}"
            f"{str(tot['px']) + '/' + str(tot['rx']):<11}"
            f"{tot['pc']:,.0f} / {tot['rc']:,.0f} ({tot['pc'] - tot['rc']:+,.0f})"
        )
        lines.append("\n  Detail typu (P/R, jen kde se objevily):")
        for c in day:
            parts = []
            for t, delta in c["delta"]["vehicle_types"].items():
                pc = c["prediction"]["vehicle_type_mix"].get(t, 0)
                rc = c["real"]["vehicle_type_mix"].get(t, 0)
                parts.append(f"{t} {pc}/{rc} ({delta:+d})")
            lines.append(f"    {c['zone']}: " + (" | ".join(parts) or "-"))
    lines.append("")
    return "\n".join(lines)


def format_available(pred_groups: dict, real_groups: dict) -> str:
    """--list: co je k dispozici k porovnání."""
    lines = ["", "Dostupné běhy (P = predikce, R = realita):", "=" * 60]
    all_keys = sorted(set(pred_groups) | set(real_groups),
                      key=lambda k: (k[1], k[0]))
    for (zone, date) in all_keys:
        preds = pred_groups.get((zone, date), [])
        reals = real_groups.get((zone, date), [])
        pdesc = ", ".join(
            f"stamp {rec_stamp(r) or '-'}"
            + (f"/{rec_label(r)}" if rec_label(r) else "")
            + f" ({r.get('run_id', '?')}, "
              f"{r.get('results', {}).get('lines_count', '?')} tras)"
            for r in preds) or "žádná"
        lines.append(f"{date} {zone}:")
        lines.append(f"    P: {pdesc}")
        lines.append(f"    R: {len(reals)}x"
                     + (f" (poslední {reals[-1].get('run_id')})" if reals else ""))
    lines.append("")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────
#  Main
# ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Porovnání predikčních běhů s ostrými. Delta = predikce - realita.")
    parser.add_argument("--date", default=None, help="Jen jeden den (YYYY-MM-DD)")
    parser.add_argument("--depots", default=None, help="Filtr dep: CB,MO")
    parser.add_argument("--pred-stamp", default=None,
                        help="Vybrat konkrétní predikční session podle HHMM razítka "
                             "(z názvu složky, např. 1811). Default: poslední predikce.")
    parser.add_argument("--pred-phase", default=None,
                        help="Kterou fázi predikce porovnat: P2 (generálka se "
                             "sekvenčním ubíráním flotily — to, co se má srovnávat "
                             "s realitou) nebo P1 (přání dep s celým skladem). "
                             "Bez zadání se bere poslední běh v čase, což u "
                             "plan_day vychází na P2 — ale jen shodou okolností. "
                             "Starší predikce (před plan_day) fázi nemají.")
    parser.add_argument("--list", action="store_true", dest="list_only",
                        help="Jen vypsat dostupné běhy, neporovnávat")
    parser.add_argument("--no-write", action="store_true",
                        help="Nezapisovat comparison.jsonl, jen zobrazit")
    args = parser.parse_args()

    pred_groups = group_by_zone_date(load_run_log(PREDICTION_LOG))
    real_groups = group_by_zone_date(load_run_log(REAL_LOG))

    depots = ([d.strip().upper() for d in args.depots.split(",") if d.strip()]
              if args.depots else None)

    def _wanted(key: tuple[str, str]) -> bool:
        zone, date = key
        return (depots is None or zone in depots) and \
               (args.date is None or date == args.date)

    pred_groups = {k: v for k, v in pred_groups.items() if _wanted(k)}
    real_groups = {k: v for k, v in real_groups.items() if _wanted(k)}

    if args.list_only:
        print(format_available(pred_groups, real_groups))
        return

    profiles = load_type_profiles()
    comparisons, missing = [], []
    for key, preds in sorted(pred_groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        pred = select_run(preds, stamp=args.pred_stamp, label=args.pred_phase)
        real = select_run(real_groups.get(key, []))
        if pred is None:
            _filt = ", ".join(filter(None, [
                f"stamp {args.pred_stamp}" if args.pred_stamp else "",
                f"fáze {args.pred_phase}" if args.pred_phase else ""])) or "žádný filtr"
            missing.append(f"{key[1]} {key[0]}: predikce ({_filt}) neexistuje")
            continue
        if real is None:
            missing.append(f"{key[1]} {key[0]}: chybí ostrý běh (predikce čeká na realitu)")
            continue
        comparisons.append(build_comparison(pred, real, profiles))

    if not comparisons:
        print("\nŽádný pár predikce+realita k porovnání."
              "\nZkus: python compare_prediction.py --list")
        for m in missing:
            print(f"  - {m}")
        return

    print(format_report(comparisons))
    for m in missing:
        print(f"  [i] {m}")

    if not args.no_write:
        upsert_comparisons(COMPARISON_PATH, comparisons)
        print(f"\n  zapsáno: {COMPARISON_PATH.as_posix()} "
              f"({len(comparisons)} záznamů aktualizováno)")


if __name__ == "__main__":
    main()
