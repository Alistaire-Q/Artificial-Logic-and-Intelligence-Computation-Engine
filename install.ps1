# ALICE Windows kurulumu. Repo klasöründe çalıştır:
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Kurulum sihirbazı (ALICE-Setup.exe, installer/alice.iss) bunu -FromInstaller ile çağırır.
param([switch]$FromInstaller, [switch]$Autostart)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot  # kurulumdan çağrılınca çalışma klasörü farklı olabilir

trap {
    Write-Host ""
    Write-Host "Kurulum tamamlanamadı: $_" -ForegroundColor Red
    if ($FromInstaller) { Read-Host "Kapatmak için Enter" | Out-Null }
    exit 1
}

# Python'u bul. "python" komutu çoğu zaman Microsoft Store'un sahte kısayolu oluyor ya da winget'ten
# sonra PATH güncellenmemiş oluyor; bu yüzden önce bilinen kurulum klasörlerine ve py başlatıcısına bak.
function Test-Py([string]$exe, [string[]]$pre) {
    try {
        $out = & $exe @pre -c "import sys; print(sys.executable if sys.version_info[:2] >= (3, 10) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $path = ([string]($out | Select-Object -Last 1)).Trim()
            if ($path -and (Test-Path $path)) { return $path }
        }
    } catch {}
    return $null
}

function Find-Python {
    $known = @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )
    foreach ($k in $known) {
        if (Test-Path $k) { $r = Test-Py $k @(); if ($r) { return $r } }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("-3.11", "-3.12", "-3.10")) { $r = Test-Py "py" @($v); if ($r) { return $r } }
    }
    return (Test-Py "python" @())
}

$py = Find-Python
if (-not $py -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Python bulunamadı, Python 3.11 kuruluyor (winget)..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
    $py = Find-Python
}
if (-not $py) {
    throw "Python bulunamadı. python.org'dan Python 3.11 kur ('Add python.exe to PATH' işaretli), PowerShell'i kapatıp aç, tekrar dene."
}
Write-Host "Python: $py"

# Otomatik güncelleme git ile çalışır (ALICE kendi deposunu çeker). Git yoksa kur; kurulamazsa ALICE yine çalışır.
if (-not (Get-Command git -ErrorAction SilentlyContinue) -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Git kuruluyor (otomatik güncelleme için)..." -ForegroundColor Yellow
    winget install -e --id Git.Git --scope user --accept-package-agreements --accept-source-agreements | Out-Null
}

if (-not (Test-Path ".venv")) { & $py -m venv .venv }
if (-not (Test-Path ".\.venv\Scripts\python.exe")) { throw "Sanal ortam (.venv) oluşturulamadı." }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -e ".[computer,browser]"
if ($LASTEXITCODE -ne 0) { throw "Paket kurulumu başarısız oldu, yukarıdaki hatayı kontrol et." }

# Uygulama penceresi (pywebview). Kurulamazsa sorun değil: ALICE Edge'in uygulama moduyla açılır.
& .\.venv\Scripts\python.exe -m pip install -e ".[app]"
if ($LASTEXITCODE -ne 0) { Write-Host "pywebview kurulamadı, uygulama Edge penceresinde açılacak." -ForegroundColor Yellow }

# Çekirdek tepsi simgesi, ses (mikrofon, bas-konuş, konuşma algılama) ve kısayollar.
& .\.venv\Scripts\python.exe -m pip install -e ".[core,voice]"
if ($LASTEXITCODE -ne 0) { Write-Host "Ses paketleri kurulamadı; yazılı kullanım çalışır." -ForegroundColor Yellow }

# Yerel Whisper: "Hey Alice"i bilgisayarda algılar (her cümle buluta gitmez) ve Groq kotası bitince yedek olur.
& .\.venv\Scripts\python.exe -m pip install faster-whisper
if ($LASTEXITCODE -ne 0) { Write-Host "Yerel Whisper kurulamadı; konuşma tanıma bulutta (Groq) çalışır." -ForegroundColor Yellow }

# Chrome ya da Edge kuruluysa ALICE onları kullanır; ikisi de yoksa Playwright Chromium indir.
$chrome = Test-Path "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
$edge = Test-Path "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (-not ($chrome -or $edge)) { & .\.venv\Scripts\python.exe -m playwright install chromium }

& .\.venv\Scripts\alice.exe init
& .\.venv\Scripts\alice.exe shortcut
if ($Autostart) { & .\.venv\Scripts\alice.exe core install }

Write-Host ""
Write-Host "Kurulum tamam." -ForegroundColor Green
Write-Host "1) $env:USERPROFILE\.alice\.env dosyasına API anahtarlarını yaz (en az GEMINI_API_KEY)."
Write-Host "2) .\.venv\Scripts\alice.exe doctor"
Write-Host "3) Masaüstündeki ALICE kısayoluna çift tıkla (uygulama), ya da terminal için: .\.venv\Scripts\alice.exe"
if ($FromInstaller) {
    Write-Host "Bu pencere birkaç saniye içinde kapanacak; ALICE'i kurulum sihirbazından açabilirsin."
    Start-Sleep -Seconds 4
}
