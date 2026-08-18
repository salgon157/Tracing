# overnight_regression.ps1 - nocni regresni A/B: solver PRED vlnami 0-3 (commit 4f0f879)
#                            vs pracovni kopie (po vlnach 0-3)
# =====================================================================================
# Spoustet z korene projektu v PowerShellu, kdyz NEBEZI ostry beh ani nic jineho,
# co bere CPU (solver pouziva vsechna jadra; soubeh by rozbil mereni).
# ORS (8081) a OSRM (5001) musi bezet:  docker start osrm-current ors-current
#
#   .\overnight_regression.ps1                    # 4 dny x 4 depa x 3 reps x 2 strany
#                                                 # + extras (PR dvojlinky, L3), budget 5
#                                                 # = 108 behu, cca 10 h
#   .\overnight_regression.ps1 -Budget 3 -Reps 3  # cca 6 h
#   .\overnight_regression.ps1 -Reps 2            # cca 7 h
#
# Kriterium (per depo-den, prisne): linky median B <= A; skutecna cena median <= +1 %,
# max <= +2 %, zadny beh > nejlepsi A +3 %; exit 0 kde A 0; run_status.json u B;
# shodne hlavicky vystupu; cas <= budget + 60 s. Kamionove linky (profil hgv) se
# u obou stran precenuji hgv km z ORS (stejny metr).
# Vystup: data\results\_regression\<stamp>\report.md (+ results.jsonl per beh)
#         a log data\results\overnight_<stamp>.log
#
# BASELINE = DOCASNY git worktree _baseline_4f0f879 (vypis stareho commitu). Skript
# ho zalozi jen pro tento beh a na konci ho zase ODSTRANI - v projektu po nem nic
# nezustane; vysledky lezi v data\results\_regression, ne ve worktree.
param(
    [double]$Budget = 5,
    [int]$Reps = 3,
    [string[]]$Dates = @("2026-08-07", "2026-08-10", "2026-08-13", "2026-08-17"),
    [string[]]$Depots = @("CB", "MO", "HK", "PR"),
    [string]$BaselineCommit = "4f0f879",
    [switch]$KeepBaseline                          # nechat worktree i po dobehu
)
$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$env:SKIP_STARTUP_TESTS = "1"
# konzole PowerShellu 5.1 cte vystup pythonu jako cp852 -> rozsypane ceske znaky;
# prepnout na UTF-8 (soubory jsou UTF-8 vzdy, tohle je jen zobrazeni)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Set-Location $PSScriptRoot

$baseDir = "_baseline_$BaselineCommit"
if (-not (Test-Path "$baseDir\vrp_solver_lines_v6.py")) {
    Write-Host "Baseline worktree - vytvarim DOCASNE: git worktree add --detach $baseDir $BaselineCommit"
    git worktree add --detach $baseDir $BaselineCommit
}
$marker = @(
    "# NESAHAT - docasny archiv stareho solveru (commit $BaselineCommit) jen pro regresni A/B.",
    "# Neni soucast projektu, needituje se, nespousti se odsud ostry beh, necommituje se.",
    "# overnight_regression.ps1 ho po dobehnuti sam odstrani (git worktree remove).",
    "# Vysledky jsou v data\results\_regression\<stamp>. Podrobnosti: WORKFLOW.md, sekce 8."
) -join "`r`n"
Set-Content -Path "$baseDir\_NESAHAT_ARCHIV.md" -Value $marker -Encoding UTF8

$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$log = "data\results\overnight_$stamp.log"
New-Item -ItemType Directory -Force -Path "data\results" | Out-Null
("=== overnight regression {0} | baseline={1} budget={2} reps={3} dates={4} ===" -f $stamp, $BaselineCommit, $Budget, $Reps, ($Dates -join ',')) | Tee-Object -FilePath $log
("### start {0}" -f (Get-Date -Format "HH:mm")) | Tee-Object -FilePath $log -Append

python regression_ab.py --baseline-dir $baseDir --dates @Dates --depots @Depots `
    --reps $Reps --budget $Budget --extras --out "data\results\_regression\$stamp" 2>&1 |
    Tee-Object -FilePath $log -Append

("### hotovo {0} - report: data\results\_regression\{1}\report.md" -f (Get-Date -Format "HH:mm"), $stamp) | Tee-Object -FilePath $log -Append

if (-not $KeepBaseline) {
    # uklid: docasny worktree pryc (vysledky zustavaji v data\results\_regression)
    git worktree remove --force $baseDir 2>$null
    git worktree prune
    if (Test-Path $baseDir) { Remove-Item -Recurse -Force $baseDir }
    ("### baseline worktree {0} odstranen (vysledky: data\results\_regression\{1})" -f $baseDir, $stamp) | Tee-Object -FilePath $log -Append
}
("=== DONE {0} ===" -f (Get-Date -Format "HH:mm")) | Tee-Object -FilePath $log -Append
