param(
    [string]$ManifestPath = (Join-Path $PSScriptRoot "ffmpeg-dependency.json"),
    [string]$CacheRoot = (Join-Path $PSScriptRoot ".build_dependencies")
)

$ErrorActionPreference = "Stop"

function Assert-OutputContains(
    [string]$Output,
    [string[]]$RequiredValues,
    [string]$Description
) {
    foreach ($value in $RequiredValues) {
        if ($Output -notmatch [regex]::Escape($value)) {
            throw "$Description is missing required capability: $value"
        }
    }
}

if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "FFmpeg dependency manifest is missing: $ManifestPath"
}

$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
foreach ($property in @("version", "variant", "archive", "url", "sha256")) {
    if (-not $manifest.$property) {
        throw "FFmpeg dependency manifest is missing '$property'."
    }
}

New-Item -ItemType Directory -Path $CacheRoot -Force | Out-Null
$archivePath = Join-Path $CacheRoot $manifest.archive
$temporaryArchive = "$archivePath.download"
$expectedHash = $manifest.sha256.ToLowerInvariant()

if (Test-Path -LiteralPath $archivePath) {
    $cachedHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($cachedHash -ne $expectedHash) {
        Remove-Item -LiteralPath $archivePath -Force
    }
}

if (-not (Test-Path -LiteralPath $archivePath)) {
    Write-Host "Downloading pinned FFmpeg $($manifest.version) $($manifest.variant)..."
    if (Test-Path -LiteralPath $temporaryArchive) {
        Remove-Item -LiteralPath $temporaryArchive -Force
    }
    Invoke-WebRequest -Uri $manifest.url -OutFile $temporaryArchive -UseBasicParsing
    $downloadHash = (Get-FileHash -LiteralPath $temporaryArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($downloadHash -ne $expectedHash) {
        Remove-Item -LiteralPath $temporaryArchive -Force
        throw "FFmpeg archive checksum mismatch. Expected $expectedHash but received $downloadHash."
    }
    Move-Item -LiteralPath $temporaryArchive -Destination $archivePath
}

$archiveBaseName = [IO.Path]::GetFileNameWithoutExtension($manifest.archive)
$extractedRoot = Join-Path $CacheRoot $archiveBaseName
$ffmpegPath = Join-Path $extractedRoot "bin\ffmpeg.exe"
$ffprobePath = Join-Path $extractedRoot "bin\ffprobe.exe"

if (-not (Test-Path -LiteralPath $ffmpegPath) -or -not (Test-Path -LiteralPath $ffprobePath)) {
    if (Test-Path -LiteralPath $extractedRoot) {
        Remove-Item -LiteralPath $extractedRoot -Recurse -Force
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $CacheRoot -Force
}

foreach ($binary in @($ffmpegPath, $ffprobePath)) {
    if (-not (Test-Path -LiteralPath $binary)) {
        throw "Pinned FFmpeg archive is missing required binary: $binary"
    }
    if ((Get-Item -LiteralPath $binary).Length -lt 5MB) {
        throw "Pinned FFmpeg binary is unexpectedly small: $binary"
    }
}

$ffmpegVersion = (& $ffmpegPath -hide_banner -version 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "Pinned FFmpeg failed to execute."
}
Assert-OutputContains `
    $ffmpegVersion `
    @("ffmpeg version $($manifest.version)-$($manifest.variant)") `
    "Pinned FFmpeg"

$ffprobeVersion = (& $ffprobePath -hide_banner -version 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "Pinned FFprobe failed to execute."
}
Assert-OutputContains `
    $ffprobeVersion `
    @("ffprobe version $($manifest.version)-$($manifest.variant)") `
    "Pinned FFprobe"

$encoders = (& $ffmpegPath -hide_banner -encoders 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect pinned FFmpeg encoders."
}
Assert-OutputContains `
    $encoders `
    @("libx264", "libx265", "libvpx-vp9", "libaom-av1", "aac", "libmp3lame", "libopus", "libvorbis") `
    "Pinned FFmpeg"

Write-Host "Pinned FFmpeg dependency verified: $($manifest.version) $($manifest.variant)"
[PSCustomObject]@{
    Version = $manifest.version
    Variant = $manifest.variant
    FFmpeg = $ffmpegPath
    FFprobe = $ffprobePath
}
