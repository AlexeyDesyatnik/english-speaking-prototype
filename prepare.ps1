<#
  Собирает портативный комплект для бенчмарка:
    runtime\  - Python 3.12 (embeddable) + пакеты (kokoro-onnx, piper-tts, onnxruntime)
    models\   - модели Kokoro и голоса Piper
  Запускать на компьютере С ИНТЕРНЕТОМ (дома), потом вся папка копируется на флешку.

  Запуск: prepare.bat   (или powershell -ExecutionPolicy Bypass -File prepare.ps1)
  Ключи:
    -SkipFp32   не качать полную модель Kokoro (325 МБ), только сжатую int8
#>
param([switch]$SkipFp32)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # иначе Invoke-WebRequest в PS 5.1 качает в разы медленнее
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root    = $PSScriptRoot
$Runtime = Join-Path $Root 'runtime'
$Models  = Join-Path $Root 'models'
$Cache   = Join-Path $Root '.cache'
$Py      = Join-Path $Runtime 'python.exe'

$PyVer  = '3.12.10'
$HfHosts = @('https://huggingface.co', 'https://hf-mirror.com')   # второй - зеркало на случай проблем с HF
$KokoroRepo = 'onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main'
$PiperRepo  = 'rhasspy/piper-voices/resolve/main'

$KokoroVoices = @('af_heart','af_bella','af_nicole','af_sarah','am_michael','am_adam',
                  'bf_emma','bf_isabella','bm_george','bm_lewis')
$PiperVoices  = @('en/en_US/lessac/medium/en_US-lessac-medium',
                  'en/en_US/lessac/high/en_US-lessac-high',
                  'en/en_US/ryan/medium/en_US-ryan-medium',
                  'en/en_GB/cori/medium/en_GB-cori-medium')

$HasCurl = [bool](Get-Command curl.exe -ErrorAction SilentlyContinue)

function Step($text) { Write-Host ""; Write-Host "=== $text" -ForegroundColor Cyan }

function Get-File([string[]]$Urls, [string]$Dest) {
    if (Test-Path $Dest) { Write-Host "  уже есть: $(Split-Path $Dest -Leaf)"; return }
    New-Item -ItemType Directory -Force -Path (Split-Path $Dest) | Out-Null
    $tmp = "$Dest.part"
    foreach ($u in $Urls) {
        Write-Host "  качаю $u"
        try {
            if ($HasCurl) {
                & curl.exe -L --fail --retry 3 --retry-delay 3 -C - -o $tmp $u
                if ($LASTEXITCODE -ne 0) { throw "curl завершился с кодом $LASTEXITCODE" }
            } else {
                Invoke-WebRequest -Uri $u -OutFile $tmp -UseBasicParsing
            }
            Move-Item -Force $tmp $Dest
            return
        } catch {
            Write-Warning "  не получилось: $_"
            if (Test-Path $tmp) { Remove-Item -Force $tmp }
        }
    }
    throw "Не удалось скачать $(Split-Path $Dest -Leaf). Проверь интернет (или включи VPN) и запусти скрипт ещё раз - уже скачанное повторно качаться не будет."
}

function Get-Hf([string]$RepoPath, [string]$Dest) {
    Get-File -Urls ($HfHosts | ForEach-Object { "$_/$RepoPath" }) -Dest $Dest
}

function Invoke-Py {
    & $Py @args
    if ($LASTEXITCODE -ne 0) { throw "python $($args -join ' ') завершился с кодом $LASTEXITCODE" }
}

# ------------------------------------------------------------------ Python
Step "Python $PyVer (embeddable)"
if (-not (Test-Path $Py)) {
    $zip = Join-Path $Cache "python-$PyVer-embed-amd64.zip"
    Get-File -Urls @("https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip") -Dest $zip
    Expand-Archive -Path $zip -DestinationPath $Runtime -Force
}
# Включаем site, чтобы работал pip и site-packages
$pth = Get-ChildItem $Runtime -Filter 'python*._pth' | Select-Object -First 1
$pthText = Get-Content $pth.FullName
if ($pthText -contains '#import site') {
    ($pthText -replace '^#import site$', 'import site') | Set-Content -Encoding Ascii $pth.FullName
}
Write-Host "  OK: $Py"

# ------------------------------------------------------------------ pip и пакеты
Step "pip и пакеты"
# В PS 5.1 перенаправленный stderr при ErrorActionPreference=Stop превращается в исключение
$ErrorActionPreference = 'Continue'
& $Py -m pip --version 2>&1 | Out-Null
$hasPip = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $hasPip) {
    $getpip = Join-Path $Cache 'get-pip.py'
    Get-File -Urls @('https://bootstrap.pypa.io/get-pip.py') -Dest $getpip
    Invoke-Py $getpip --no-warn-script-location
}
Invoke-Py -m pip install --no-warn-script-location --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')

# ------------------------------------------------------------------ библиотеки Visual C++
# onnxruntime и espeak-ng требуют msvcp140*.dll. На школьных ноутбуках
# Visual C++ Redistributable может не стоять, поэтому кладём DLL рядом с python.exe.
Step "Библиотеки Visual C++"
$sys32 = if ([Environment]::Is64BitProcess) { "$env:windir\System32" } else { "$env:windir\Sysnative" }
$missing = @()
foreach ($dll in 'msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll') {
    $src = Join-Path $sys32 $dll
    if (Test-Path $src) { Copy-Item -Force $src $Runtime; Write-Host "  скопирован $dll" }
    elseif (-not (Test-Path (Join-Path $Runtime $dll))) { $missing += $dll }
}
if ($missing) {
    Write-Warning "  На этом компьютере нет $($missing -join ', '). Качаю установщик vc_redist.x64.exe -"
    Write-Warning "  если на ноутбуке бенчмарк не запустится, установи его там."
    Get-File -Urls @('https://aka.ms/vs/17/release/vc_redist.x64.exe') -Dest (Join-Path $Root 'vc_redist.x64.exe')
}

# ------------------------------------------------------------------ модели Kokoro
Step "Модели Kokoro"
$k = Join-Path $Models 'kokoro'
if (-not $SkipFp32) { Get-Hf "$KokoroRepo/onnx/model.onnx" (Join-Path $k 'kokoro-fp32.onnx') }
Get-Hf "$KokoroRepo/onnx/model_quantized.onnx" (Join-Path $k 'kokoro-int8.onnx')
foreach ($v in $KokoroVoices) { Get-Hf "$KokoroRepo/voices/$v.bin" (Join-Path $k "voices\$v.bin") }
Invoke-Py (Join-Path $Root 'tools\pack_kokoro_voices.py') (Join-Path $k 'voices') (Join-Path $k 'voices.npz')

# ------------------------------------------------------------------ голоса Piper
Step "Голоса Piper"
$p = Join-Path $Models 'piper'
foreach ($v in $PiperVoices) {
    $name = Split-Path $v -Leaf
    Get-Hf "$PiperRepo/$v.onnx" (Join-Path $p "$name.onnx")
    Get-Hf "$PiperRepo/$v.onnx.json" (Join-Path $p "$name.onnx.json")
}

# ------------------------------------------------------------------ проверка
Step "Проверка"
$env:PYTHONUTF8 = '1'
Invoke-Py (Join-Path $Root 'bench\bench.py') --selftest

$sizeMb = [math]::Round(((Get-ChildItem $Runtime, $Models -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB)
Write-Host ""
Write-Host "Готово. Комплект занимает ~$sizeMb МБ." -ForegroundColor Green
Write-Host "Скопируй папку на флешку (папки .git и .cache не нужны) и запусти run_bench.bat на каждом ноутбуке."
