# ALICE Windows kurulumu. Repo klasöründe çalıştır:
#   powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "Python bulunamadı. python.org'dan 3.11+ kur ve 'Add to PATH' kutusunu işaretle." }
$ver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$ver -lt [version]"3.10") { throw "Python $ver çok eski, 3.10+ gerekli." }

if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -e ".[all]"

# Chrome ya da Edge kuruluysa ALICE onları kullanır; ikisi de yoksa Playwright Chromium indir.
$chrome = Test-Path "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
$edge = Test-Path "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (-not ($chrome -or $edge)) { & .\.venv\Scripts\python.exe -m playwright install chromium }

& .\.venv\Scripts\alice.exe init

Write-Host ""
Write-Host "Kurulum tamam." -ForegroundColor Green
Write-Host "1) $env:USERPROFILE\.alice\.env dosyasına API anahtarlarını yaz (en az GEMINI_API_KEY)."
Write-Host "2) .\.venv\Scripts\alice.exe doctor"
Write-Host "3) .\.venv\Scripts\alice.exe"
