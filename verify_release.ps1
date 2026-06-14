param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [string]$ReleaseRoot
)

$ErrorActionPreference = "Stop"

$releaseRootPath = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$appDirectory = Join-Path $releaseRootPath "Twitch Stream Bot"
$zipPath = Join-Path $appDirectory "Twitch Stream Bot $Version.zip"
$installerPath = Join-Path $releaseRootPath "Twitch Stream Bot Setup $Version.exe"
$appUpdatePath = Join-Path $releaseRootPath "Twitch Stream Bot App Update $Version.exe"
$minimumMediaBinarySize = 5MB
$minimumZipSize = 50MB
$minimumInstallerSize = 50MB
$minimumAppUpdateSize = 5MB

function Assert-FileSize([string]$Path, [long]$MinimumBytes, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Description is missing: $Path"
    }
    $size = (Get-Item -LiteralPath $Path).Length
    if ($size -lt $MinimumBytes) {
        throw "$Description is unexpectedly small ($size bytes): $Path"
    }
}

function Assert-ExecutableRuns([string]$Path, [string]$Description) {
    $process = Start-Process `
        -FilePath $Path `
        -ArgumentList "-version" `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "$Description failed to execute with exit code $($process.ExitCode): $Path"
    }
}

Assert-FileSize $zipPath $minimumZipSize "Portable ZIP"
Assert-FileSize $installerPath $minimumInstallerSize "Windows installer"
Assert-FileSize $appUpdatePath $minimumAppUpdateSize "App-only update"

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    $entryMap = @{}
    foreach ($entry in $archive.Entries) {
        $entryMap[$entry.FullName.Replace("/", "\")] = $entry
    }

    foreach ($name in @(
        "Twitch Stream Bot.exe",
        "_internal\ffmpeg.exe",
        "_internal\ffprobe.exe",
        "_internal\base_library.zip"
    )) {
        if (-not $entryMap.ContainsKey($name)) {
            throw "Portable ZIP is missing required file: $name"
        }
    }

    foreach ($name in @("_internal\ffmpeg.exe", "_internal\ffprobe.exe")) {
        if ($entryMap[$name].Length -lt $minimumMediaBinarySize) {
            throw "Portable ZIP contains a media launcher shim instead of a standalone binary: $name"
        }
    }

    $forbidden = @(
        $archive.Entries.FullName | Where-Object {
            $_ -match '(^|/)(config\.json|bot_state.*\.json|sr_queue.*\.json|\.env|temp_sr)(/|$)' -or
            $_ -match '\.(db|sqlite|sqlite3|mp3|m4a|flac|wav|ogg|mp4|mkv|webm|avi|mov)$'
        }
    )
    if ($forbidden.Count -gt 0) {
        throw "Portable ZIP contains runtime, credential, or media files: $($forbidden -join ', ')"
    }
} finally {
    $archive.Dispose()
}

$testRoot = Join-Path $env:TEMP "Twitch-Stream-Bot-release-verification-$PID"
$installDirectory = Join-Path $testRoot "Program"
$cleanUpdateDirectory = Join-Path $testRoot "CleanUpdate"
$testLocalAppData = Join-Path $testRoot "LocalAppData"
$originalLocalAppData = $env:LOCALAPPDATA

try {
    New-Item -ItemType Directory -Path $installDirectory, $cleanUpdateDirectory, $testLocalAppData -Force | Out-Null
    $env:LOCALAPPDATA = $testLocalAppData

    $rejectedUpdate = Start-Process `
        -FilePath $appUpdatePath `
        -ArgumentList @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/DIR=`"$cleanUpdateDirectory`""
        ) `
        -Wait `
        -PassThru
    if ($rejectedUpdate.ExitCode -eq 0) {
        throw "App-only update unexpectedly succeeded without an existing full installation."
    }

    $installer = Start-Process `
        -FilePath $installerPath `
        -ArgumentList @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/DIR=`"$installDirectory`""
        ) `
        -Wait `
        -PassThru
    if ($installer.ExitCode -ne 0) {
        throw "Silent installer returned exit code $($installer.ExitCode)."
    }

    $installedApp = Join-Path $installDirectory "Twitch Stream Bot.exe"
    $installedFfmpeg = Join-Path $installDirectory "_internal\ffmpeg.exe"
    $installedFfprobe = Join-Path $installDirectory "_internal\ffprobe.exe"
    foreach ($binary in @($installedFfmpeg, $installedFfprobe)) {
        Assert-FileSize $binary $minimumMediaBinarySize "Installed media binary"
        Assert-ExecutableRuns $binary "Installed media binary"
    }

    if (-not (Test-Path -LiteralPath $installedApp)) {
        throw "Installed application executable is missing."
    }

    $ffmpegHashBefore = (Get-FileHash -LiteralPath $installedFfmpeg -Algorithm SHA256).Hash
    $ffprobeHashBefore = (Get-FileHash -LiteralPath $installedFfprobe -Algorithm SHA256).Hash
    $appUpdate = Start-Process `
        -FilePath $appUpdatePath `
        -ArgumentList @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/DIR=`"$installDirectory`""
        ) `
        -Wait `
        -PassThru
    if ($appUpdate.ExitCode -ne 0) {
        throw "Silent app-only update returned exit code $($appUpdate.ExitCode)."
    }

    $ffmpegHashAfter = (Get-FileHash -LiteralPath $installedFfmpeg -Algorithm SHA256).Hash
    $ffprobeHashAfter = (Get-FileHash -LiteralPath $installedFfprobe -Algorithm SHA256).Hash
    if ($ffmpegHashBefore -ne $ffmpegHashAfter) {
        throw "App-only update modified the installed FFmpeg binary."
    }
    if ($ffprobeHashBefore -ne $ffprobeHashAfter) {
        throw "App-only update modified the installed FFprobe binary."
    }

    $appProcess = Start-Process -FilePath $installedApp -PassThru
    Start-Sleep -Seconds 6
    $appProcess.Refresh()
    if ($appProcess.HasExited) {
        throw "Installed application exited during startup verification."
    }
    Stop-Process -Id $appProcess.Id -Force

    $uninstallerPath = Join-Path $installDirectory "unins000.exe"
    if (-not (Test-Path -LiteralPath $uninstallerPath)) {
        throw "Uninstaller was not created."
    }
    $uninstaller = Start-Process `
        -FilePath $uninstallerPath `
        -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
        -Wait `
        -PassThru
    if ($uninstaller.ExitCode -ne 0) {
        throw "Silent uninstaller returned exit code $($uninstaller.ExitCode)."
    }
} finally {
    $env:LOCALAPPDATA = $originalLocalAppData
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Release verification passed for version $Version."
