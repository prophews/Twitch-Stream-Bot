$ErrorActionPreference = "Stop"

$root = (Resolve-Path $PSScriptRoot).Path
$buildPath = Join-Path $root "build_v2"
$stagingRoot = Join-Path $root "dist_v2_build"
$appName = "Twitch Stream Bot"
$stagedApp = Join-Path $stagingRoot $appName
$publicRoot = Join-Path $root "dist_public"
$publicApp = Join-Path $publicRoot $appName
$backupApp = "$publicApp.previous"
$versionMatch = Select-String -Path (Join-Path $root "settings.py") -Pattern 'APP_VERSION = "([^"]+)"'
if (-not $versionMatch) {
    throw "Could not read APP_VERSION from settings.py."
}
$appVersion = $versionMatch.Matches[0].Groups[1].Value
$zipName = "Twitch Stream Bot $appVersion.zip"

function Remove-SafeDirectory([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (-not $resolved.StartsWith($root + [IO.Path]::DirectorySeparatorChar)) {
        throw "Refusing to remove a path outside the project: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Move-DirectoryWithRetry([string]$Source, [string]$Destination) {
    foreach ($attempt in 1..5) {
        try {
            Move-Item -LiteralPath $Source -Destination $Destination -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq 5) {
                throw
            }
            Start-Sleep -Seconds 2
        }
    }
}

Write-Host "Installing build dependencies..."
python -m pip install --break-system-packages -r (Join-Path $root "requirements.txt") pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
$ffprobe = (Get-Command ffprobe.exe -ErrorAction Stop).Source

Remove-SafeDirectory $buildPath
Remove-SafeDirectory $stagingRoot
New-Item -ItemType Directory -Path $buildPath -Force | Out-Null

Write-Host "Building $appName..."
& pyinstaller `
    --noconfirm `
    --onedir `
    --windowed `
    --specpath $buildPath `
    --distpath $stagingRoot `
    --workpath $buildPath `
    --name $appName `
    --hidden-import cogs `
    --hidden-import cogs.sr_cog `
    --add-data "$root\player.html;." `
    --add-binary "$ffmpeg;." `
    --add-binary "$ffprobe;." `
    (Join-Path $root "run_gui.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed."
}

New-Item -ItemType Directory -Path $publicRoot -Force | Out-Null
Remove-SafeDirectory $backupApp
if (Test-Path -LiteralPath $publicApp) {
    Move-DirectoryWithRetry $publicApp $backupApp
}
try {
    Move-DirectoryWithRetry $stagedApp $publicApp
} catch {
    if (Test-Path -LiteralPath $backupApp) {
        Move-DirectoryWithRetry $backupApp $publicApp
    }
    throw
}
Remove-SafeDirectory $backupApp

Add-Type -AssemblyName System.IO.Compression.FileSystem
$temporaryZip = Join-Path $publicRoot "$zipName.new"
$finalZip = Join-Path $publicApp $zipName
if (Test-Path -LiteralPath $temporaryZip) {
    Remove-Item -LiteralPath $temporaryZip -Force
}
[IO.Compression.ZipFile]::CreateFromDirectory(
    $publicApp,
    $temporaryZip,
    [IO.Compression.CompressionLevel]::Optimal,
    $false
)
Move-Item -LiteralPath $temporaryZip -Destination $finalZip

$archive = [IO.Compression.ZipFile]::OpenRead($finalZip)
try {
    $sensitive = @(
        $archive.Entries.FullName | Where-Object {
            $_ -match '(^|/)(config|bot_state|queue_state|settings|loyalty)(\.|/|$)|\.env$|\.sqlite'
        }
    )
    if ($sensitive.Count -gt 0) {
        throw "Release ZIP contains runtime or sensitive files: $($sensitive -join ', ')"
    }
} finally {
    $archive.Dispose()
}

$isccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)
$iscc = $isccCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if ($iscc) {
    Write-Host "Building Windows installer..."
    & $iscc "/DMyAppVersion=$appVersion" (Join-Path $root "installer.iss")
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed."
    }
} else {
    Write-Warning "Inno Setup was not found. ZIP built, but installer was skipped."
}

Remove-SafeDirectory $buildPath
Remove-SafeDirectory $stagingRoot
Write-Host "Release ready: $finalZip"
