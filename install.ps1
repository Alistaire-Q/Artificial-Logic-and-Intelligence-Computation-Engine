# ALICE Windows kurulumu. Repo klasöründe çalıştır:
#   powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"

# Windows'taki "python" Microsoft Store kısayolu gerçek Python değil, gerçekten çalışıyor mu diye bak.
$ver = $null
try { $ver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null } catch {}
if (-not $ver) {
    throw "Python bulunamadı. 'winget install Python.Python.3.12' çalıştır (ya da python.org'dan kur, 'Add to PATH' işaretli), PowerShell'i kapatıp aç, tekrar dene."
}
if ([version]$ver -lt [version]"3.10") { throw "Python $ver çok eski, 3.10+ gerekli." }

if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -e ".[computer,browser]"
if ($LASTEXITCODE -ne 0) { throw "Paket kurulumu başarısız oldu, yukarıdaki hatayı kontrol et." }

# Uygulama penceresi (pywebview). Kurulamazsa sorun değil: ALICE Edge'in uygulama moduyla açılır.
& .\.venv\Scripts\python.exe -m pip install -e ".[app]"
if ($LASTEXITCODE -ne 0) { Write-Host "pywebview kurulamadı, uygulama Edge penceresinde açılacak." -ForegroundColor Yellow }

# Chrome ya da Edge kuruluysa ALICE onları kullanır; ikisi de yoksa Playwright Chromium indir.
$chrome = Test-Path "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
$edge = Test-Path "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (-not ($chrome -or $edge)) { & .\.venv\Scripts\python.exe -m playwright install chromium }

& .\.venv\Scripts\alice.exe init
& .\.venv\Scripts\alice.exe shortcut

Write-Host ""
Write-Host "Kurulum tamam." -ForegroundColor Green
Write-Host "1) $env:USERPROFILE\.alice\.env dosyasına API anahtarlarını yaz (en az GEMINI_API_KEY)."
Write-Host "2) .\.venv\Scripts\alice.exe doctor"
Write-Host "3) Masaüstündeki ALICE kısayoluna çift tıkla (uygulama), ya da terminal için: .\.venv\Scripts\alice.exe"
