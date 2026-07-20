# =============================================================================
#  build_shortest_osrm.ps1 - izolovana OSRM instance hledajici NEJKRATSI trasu
# =============================================================================
#  Stavi C:\osrm_shortest a kontejner osrm-shortest na portu 5002.
#
#  POZOR: soubor je zamerne CISTE ASCII (bez diakritiky a pomlcek).
#  PowerShell 5.1 cte .ps1 jako cp1250, takze UTF-8 znaky bez BOM rozbiji parser.
#
#  BEZPECNOST - produkce se nesmi dotknout:
#    * C:\osrm         - pouze CTEME (kopie PBF), nikdy nezapisujeme
#    * C:\osrm_current - vubec
#    * kontejnery osrm-server / ors-hgv / *-current - vubec
#    * porty 5000 / 5001 / 8080 / 8081 - vubec
#
#  JABLKA S JABLKY: PBF se KOPIRUJE ze stable (stejna OSM data, 8.4.2026),
#  nestahuje se nove. Algoritmus MLD stejny jako stable.
# =============================================================================

$ErrorActionPreference = "Stop"

$StableDir   = "C:\osrm"
$TargetDir   = "C:\osrm_shortest"
$ProfilesDir = "$TargetDir\profiles"
$PbfName     = "czech-republic-latest.osm.pbf"
$OsrmName    = "czech-republic-latest.osrm"
$Image       = "osrm/osrm-backend"
$Container   = "osrm-shortest"
$Port        = 5002

Write-Host "=== OSRM 'nejkratsi trasa' - build ===" -ForegroundColor Cyan

# --- 0) Kontroly -------------------------------------------------------------
if (-not (Test-Path "$StableDir\$PbfName")) {
    throw "Nenalezen PBF: $StableDir\$PbfName. Bez nej nelze zajistit stejna OSM data."
}
docker version *> $null
if (-not $?) { throw "Docker nebezi. Spust Docker Desktop." }

$exists = docker ps -a --format '{{.Names}}' | Select-String -Quiet "^$Container$"
if ($exists) {
    throw "Kontejner '$Container' uz existuje. Nejdriv: docker stop $Container; docker rm $Container"
}
$portBusy = docker ps --format '{{.Ports}}' | Select-String -Quiet ":$Port->"
if ($portBusy) { throw "Port $Port je obsazeny jinym kontejnerem." }

# --- 1) Cilova slozka + KOPIE stejneho PBF -----------------------------------
if (-not (Test-Path $TargetDir))   { New-Item -ItemType Directory $TargetDir   | Out-Null }
if (-not (Test-Path $ProfilesDir)) { New-Item -ItemType Directory $ProfilesDir | Out-Null }

if (-not (Test-Path "$TargetDir\$PbfName")) {
    Write-Host "`n[1/5] Kopiruji PBF ze stable (stejna data, ~880 MB)..." -ForegroundColor Yellow
    Copy-Item "$StableDir\$PbfName" "$TargetDir\$PbfName"
} else {
    Write-Host "`n[1/5] PBF uz zkopirovany - preskakuji." -ForegroundColor DarkGray
}
$src = Get-Item "$StableDir\$PbfName"
$dst = Get-Item "$TargetDir\$PbfName"
Write-Host ("      zdroj: {0}  {1:N1} MB  {2}" -f $src.Name, ($src.Length/1MB), $src.LastWriteTime)
if ($src.Length -ne $dst.Length) { throw "Kopie PBF ma jinou velikost nez zdroj!" }

# --- 2) Profil car.lua s weight_name = 'distance' ----------------------------
# car.lua potrebuje i svoje lib/, proto kopirujeme cely /opt z image.
if (-not (Test-Path "$ProfilesDir\car.lua")) {
    Write-Host "`n[2/5] Vytahuji profily z image a patchuji na 'distance'..." -ForegroundColor Yellow
    docker run --rm -v "${ProfilesDir}:/out" $Image sh -c "cp -r /opt/. /out/"
    if (-not $?) { throw "Nepodarilo se zkopirovat profily z image." }

    $luaPath = "$ProfilesDir\car.lua"
    $lua = Get-Content $luaPath -Raw
    if ($lua -notmatch "weight_name\s*=") {
        throw "V car.lua nenalezen 'weight_name' - zmenila se struktura profilu."
    }
    # default 'routability' -> 'distance' = OSRM minimalizuje vzdalenost
    $lua = $lua -replace "weight_name\s*=\s*'[^']*'", "weight_name = 'distance'"
    Set-Content $luaPath $lua -Encoding UTF8
    Write-Host "      car.lua: weight_name = 'distance'" -ForegroundColor Green
} else {
    Write-Host "`n[2/5] Profil uz existuje - preskakuji." -ForegroundColor DarkGray
}

# --- 3) osrm-extract (nejdelsi krok, desitky minut) --------------------------
if (-not (Test-Path "$TargetDir\$OsrmName")) {
    Write-Host "`n[3/5] osrm-extract (dlouhe - klidne 30+ min)..." -ForegroundColor Yellow
    docker run --rm -v "${TargetDir}:/data" -v "${ProfilesDir}:/profiles" $Image osrm-extract -p /profiles/car.lua "/data/$PbfName"
    if (-not $?) { throw "osrm-extract selhal." }
} else {
    Write-Host "`n[3/5] Extract uz hotovy - preskakuji." -ForegroundColor DarkGray
}

# --- 4) partition + customize (MLD, stejne jako stable) ----------------------
Write-Host "`n[4/5] osrm-partition + osrm-customize..." -ForegroundColor Yellow
docker run --rm -v "${TargetDir}:/data" $Image osrm-partition "/data/$OsrmName"
if (-not $?) { throw "osrm-partition selhal." }
docker run --rm -v "${TargetDir}:/data" $Image osrm-customize "/data/$OsrmName"
if (-not $?) { throw "osrm-customize selhal." }

# --- 5) Start kontejneru na 5002 ---------------------------------------------
# --max-table-size 1000 MUSI sedet s produkcnim osrm-server, jinak by /table
# odmitl vetsi matice (default je 100) a merení by nebylo srovnatelne.
Write-Host "`n[5/5] Startuji '$Container' na portu $Port..." -ForegroundColor Yellow
docker run -d --name $Container -p "${Port}:5000" -v "${TargetDir}:/data" $Image osrm-routed --algorithm mld --max-table-size 1000 "/data/$OsrmName" | Out-Null
if (-not $?) { throw "Start kontejneru selhal." }

Start-Sleep -Seconds 5
Write-Host "`n=== HOTOVO ===" -ForegroundColor Green
Write-Host "Overeni (nejkratsi musi mit MENSI distance a VETSI duration):"
Write-Host '  (Invoke-RestMethod "http://localhost:5000/route/v1/driving/15.595,49.506;14.259,48.810?overview=false").routes[0] | Select-Object distance, duration, weight_name'
Write-Host '  (Invoke-RestMethod "http://localhost:5002/route/v1/driving/15.595,49.506;14.259,48.810?overview=false").routes[0] | Select-Object distance, duration, weight_name'
Write-Host '  (pozor: v PowerShellu je "curl" alias pro Invoke-WebRequest a hlasi bezpecnostni varovani)'
Write-Host ""
Write-Host "Uklid az skoncis:"
Write-Host "  docker stop $Container; docker rm $Container"
Write-Host "  Remove-Item -Recurse -Force $TargetDir"
