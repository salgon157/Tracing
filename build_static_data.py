"""
build_static_data.py — vytvoří statická CSV z dodaných Excel databází
=====================================================================
Vstupy:
  Databáze - Vozidla.xlsx
  Lokace CB.xlsx

Výstupy:
  data/static/vehicle_registry.csv
  data/static/vehicle_types.csv
  data/static/locations_lookup.csv

Poznámka:
- cost_per_km ve vehicle_types.csv je zatím inherited default z předchozího projektu.
- Před produkčním použitím ho potvrď nebo uprav.
"""

from pathlib import Path
import pandas as pd

VEHICLES_XLSX = Path("Databáze - Vozidla.xlsx")
LOCATIONS_XLSX = Path("Lokace CB.xlsx")
STATIC_DIR = Path("data/static")


def assumed_cost(weight: int) -> float:
    if weight <= 1500:
        return 18.5
    if weight <= 3500:
        return 28.0
    if weight <= 10000:
        return 42.0
    return 58.0


def build_vehicle_files():
    df = pd.read_excel(VEHICLES_XLSX)
    df.columns = [str(c).strip() for c in df.columns]
    df["Číslo"] = df["Číslo"].astype(str).str.strip()
    df["SPZ"] = df["SPZ"].astype(str).str.strip()
    df["Řidič"] = df["Řidič"].fillna("").astype(str).str.strip()
    df["Profil vozidla"] = df["Profil vozidla"].fillna("").astype(str).str.strip()
    df["Dostupnost"] = df["Dostupnost"].fillna("").astype(str).str.strip()
    df["Dopravce"] = df["Dopravce"].fillna("").astype(str).str.strip()
    df["GPS poskytovatel"] = df["GPS poskytovatel"].fillna("").astype(str).str.strip()
    df["Hmotnost"] = pd.to_numeric(df["Hmotnost"], errors="coerce")
    df["Třetí"] = pd.to_numeric(df["Třetí"], errors="coerce")
    df["Aktivní"] = pd.to_numeric(df["Aktivní"], errors="coerce").fillna(0).astype(int)
    df["Názvy typu vozidla"] = pd.to_numeric(df["Názvy typu vozidla"], errors="coerce")

    weights = sorted([int(w) for w in df["Hmotnost"].dropna().unique()])
    type_codes = {w: f"TYPE_{i+1:02d}" for i, w in enumerate(weights)}
    type_labels = {w: f"Type {chr(65+i)}" for i, w in enumerate(weights)}

    registry = pd.DataFrame({
        "vehicle_id": df["Číslo"],
        "plate_or_label": df["SPZ"],
        "driver_name": df["Řidič"],
        "carrier": df["Dopravce"],
        "vehicle_profile": df["Profil vozidla"],
        "availability_window": df["Dostupnost"],
        "max_kg": df["Hmotnost"],
        "third_raw": df["Třetí"],
        "active_flag": df["Aktivní"],
        "riro_vehicle_type_code": df["Názvy typu vozidla"],
        "gps_provider": df["GPS poskytovatel"],
        "gps_id": df["GPSId"],
        "type_code": df["Hmotnost"].map(lambda x: type_codes.get(int(x)) if pd.notna(x) else ""),
        "type_name": df["Hmotnost"].map(lambda x: type_labels.get(int(x)) if pd.notna(x) else ""),
    }).sort_values(["max_kg", "vehicle_id"])

    types = (
        df.groupby("Hmotnost", dropna=True)
          .agg(
              total_count=("Číslo", "count"),
              active_count=("Aktivní", "sum"),
              profiles=("Profil vozidla", lambda s: "|".join(sorted(set(map(str, s))))),
              riro_type_codes=("Názvy typu vozidla", lambda s: "|".join(str(int(x)) for x in sorted(set(pd.Series(s).dropna().astype(int).tolist())))),
              example_vehicle=("SPZ", "first"),
          )
          .reset_index()
    )
    types["max_kg"] = types["Hmotnost"].astype(int)
    types["type_code"] = types["max_kg"].map(type_codes)
    types["type_name"] = types["max_kg"].map(type_labels)
    types["cost_per_km"] = types["max_kg"].map(assumed_cost)
    types["available_count"] = types["total_count"]
    types["cost_per_km_source"] = "inherited_default_from_previous_project_assumptions"
    types["available_count_source"] = "defaulted_to_total_count_review_before_production"

    types = types[[
        "type_code", "type_name", "max_kg", "cost_per_km", "available_count",
        "total_count", "active_count", "profiles", "riro_type_codes",
        "example_vehicle", "cost_per_km_source", "available_count_source",
    ]]

    registry.to_csv(STATIC_DIR / "vehicle_registry.csv", index=False)
    types.to_csv(STATIC_DIR / "vehicle_types.csv", index=False)


def build_locations_file():
    df = pd.read_excel(LOCATIONS_XLSX)
    df.columns = [str(c).strip() for c in df.columns]
    for col in ["Číslo", "Název", "Adresa", "Časové okno", "AČ@Z", "Č@Z", "Komentář"]:
        df[col] = df[col].fillna("").astype(str).str.strip()
    df["Šířka"] = pd.to_numeric(df["Šířka"], errors="coerce")
    df["Délka"] = pd.to_numeric(df["Délka"], errors="coerce")
    df["Názvy typu vozidla"] = pd.to_numeric(df["Názvy typu vozidla"], errors="coerce")

    lookup = pd.DataFrame({
        "location_code": df["Číslo"].str.lower(),
        "name": df["Název"],
        "address": df["Adresa"],
        "lat": df["Šířka"],
        "lon": df["Délka"],
        "default_time_window": df["Časové okno"],
        "admin_time_default": df["AČ@Z"],
        "service_time_default": df["Č@Z"],
        "riro_vehicle_type_code": df["Názvy typu vozidla"],
        "comment": df["Komentář"],
    }).drop_duplicates(subset=["location_code"]).sort_values("location_code")

    lookup.to_csv(STATIC_DIR / "locations_lookup.csv", index=False)


def main():
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    if not VEHICLES_XLSX.exists():
        raise FileNotFoundError(VEHICLES_XLSX)
    if not LOCATIONS_XLSX.exists():
        raise FileNotFoundError(LOCATIONS_XLSX)

    build_vehicle_files()
    build_locations_file()

    print("Uloženo:")
    print(f"  {STATIC_DIR / 'vehicle_registry.csv'}")
    print(f"  {STATIC_DIR / 'vehicle_types.csv'}")
    print(f"  {STATIC_DIR / 'locations_lookup.csv'}")


if __name__ == "__main__":
    main()
