# overnight_cost_matrix.ps1 - nocni benchmark nakladove matice: legacy vs exact (vlna 4)
#                             tyz solver (pracovni kopie), lisi se jen --cost-matrix-mode
# =====================================================================================
# Spoustet z korene projektu v PowerShellu, kdyz NEBEZI ostry beh ani nic jineho,
# co bere CPU (solver pouziva vsechna jadra; soubeh by rozbil mereni).
# ORS (8081) a OSRM (5001) musi bezet:  docker start osrm-current ors-current
#
#   .\overnight_cost_matrix.ps1                   # 4 dny x 4 depa x 3 reps x 2 rezimy
#                                                 # + extras (PR dvojlinky, L3), budget 5
#                                                 # = 108 behu, cca 10 h
#   .\overnight_cost_matrix.ps1 -Budget 3 -Reps 3 # cca 6 h
#   .\overnight_cost_matrix.ps1 -Reps 2           # cca 7 h
#
# A = --cost-matrix-mode legacy (dnesni default: Python callbacky, sazba int(19,5)=19,
#     km kamionu z osobni matice), B = exact (RegisterTransitMatrix, presna sazba,
#     km per profil vozidla). Rozhoduje o defaultu CONFIG cost_matrix_mode: exact jen
#     kdyz projde stejne prisne kriterium jako regrese.
# Kriterium (per depo-den): linky median B <= A; skutecna cena median <= +1 %,
# max <= +2 %, zadny beh > nejlepsi A +3 %; exit 0 kde A 0; run_status.json;
# shodne hlavicky vystupu; cas <= budget + 60 s. Kamionove linky se u OBOU stran
# precenuji hgv km z ORS (stejny metr - legacy jinak vykazuje kamionu osobni km).
# Vystup: data\results\_bench_cost_matrix\<stamp>\report.md (+ results.jsonl per beh)
#         a log data\results\overnight_cost_<stamp>.log
param(
    [double]$Budget = 5,
    [int]$Reps = 3,
    [string[]]$Dates = @("2026-08-07", "2026-08-10", "2026-08-13", "2026-08-17"),
    [string[]]$Depots = @("CB", "MO", "HK", "PR")
)
$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$env:SKIP_STARTUP_TESTS = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Set-Location $PSScriptRoot

$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$log = "data\results\overnight_cost_$stamp.log"
New-Item -ItemType Directory -Force -Path "data\results" | Out-Null
("=== overnight cost matrix (legacy vs exact) {0} | budget={1} reps={2} dates={3} ===" -f $stamp, $Budget, $Reps, ($Dates -join ',')) | Tee-Object -FilePath $log
("### start {0}" -f (Get-Date -Format "HH:mm")) | Tee-Object -FilePath $log -Append

python benchmark_cost_matrix.py --dates @Dates --depots @Depots `
    --reps $Reps --budget $Budget --extras --out "data\results\_bench_cost_matrix\$stamp" 2>&1 |
    Tee-Object -FilePath $log -Append

("### hotovo {0} - report: data\results\_bench_cost_matrix\{1}\report.md" -f (Get-Date -Format "HH:mm"), $stamp) | Tee-Object -FilePath $log -Append
("=== DONE {0} ===" -f (Get-Date -Format "HH:mm")) | Tee-Object -FilePath $log -Append
