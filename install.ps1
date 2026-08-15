[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [string]$Version = "3.5.3"
)

$ErrorActionPreference = "Stop"
$Repository = "YuanYeYouTao/Yuki-QQbot"
$BotImage = "ghcr.io/yuanyeyoutao/yuki-qqbot"

function Fail([string]$Message) {
    throw $Message
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Fail "Version must use X.Y.Z."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is not installed."
}
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "The Docker Compose CLI plugin is not available."
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker Engine is not running."
}
$Architecture = (& docker info --format '{{.Architecture}}').Trim()
$DockerOs = (& docker info --format '{{.OSType}}').Trim()
if ($DockerOs -ne 'linux') {
    Fail "Docker Desktop must be running Linux containers."
}
if ($Architecture -notin @('amd64', 'x86_64')) {
    Fail "Yuki $Version officially supports linux/amd64; Docker reports $Architecture."
}

if (-not $InstallDir) {
    if ((Test-Path -LiteralPath "docker-compose.yml") -and (Test-Path -LiteralPath ".env.example")) {
        $InstallDir = (Get-Location).Path
    } else {
        $InstallDir = Join-Path (Get-Location).Path "yuki"
    }
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
[System.IO.Directory]::CreateDirectory($InstallDir) | Out-Null
$WriteProbe = Join-Path $InstallDir (".yuki-write-test-" + [guid]::NewGuid())
try {
    [System.IO.File]::WriteAllText($WriteProbe, "write-test")
} catch {
    Fail "Installation directory is not writable."
} finally {
    if (Test-Path -LiteralPath $WriteProbe) {
        Remove-Item -LiteralPath $WriteProbe -Force
    }
}
$ComposePath = Join-Path $InstallDir "docker-compose.yml"
$EnvTemplatePath = Join-Path $InstallDir ".env.example"
$Existing = (Test-Path -LiteralPath $ComposePath) -and (Test-Path -LiteralPath $EnvTemplatePath)
if (-not $Existing -and (Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1)) {
    Fail "Installation directory is not empty and is not a Yuki deployment."
}

$Temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("yuki-install-" + [guid]::NewGuid())
[System.IO.Directory]::CreateDirectory($Temporary) | Out-Null
try {
    $Base = "https://github.com/$Repository/releases/download/v$Version"
    $ArchiveName = "yuki-$Version-deploy.zip"
    $Archive = Join-Path $Temporary $ArchiveName
    $Checksums = Join-Path $Temporary "SHA256SUMS"
    Invoke-WebRequest -Uri "$Base/$ArchiveName" -OutFile $Archive
    Invoke-WebRequest -Uri "$Base/SHA256SUMS" -OutFile $Checksums
    $ChecksumLine = Get-Content -LiteralPath $Checksums | Where-Object {
        $_ -match "^[0-9a-fA-F]{64}\s+$([regex]::Escape($ArchiveName))$"
    } | Select-Object -First 1
    if (-not $ChecksumLine) {
        Fail "Release checksum does not list $ArchiveName."
    }
    $Expected = ($ChecksumLine -split '\s+')[0].ToLowerInvariant()
    $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Expected -ne $Actual) {
        Fail "Release archive checksum mismatch."
    }
    Expand-Archive -LiteralPath $Archive -DestinationPath $Temporary
    $Source = Join-Path $Temporary "yuki-$Version-deploy"
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        Fail "Release archive layout is invalid."
    }
    if (-not $Existing) {
        Copy-Item -Path (Join-Path $Source '*') -Destination $InstallDir -Recurse -Force
        Get-ChildItem -LiteralPath $Source -Force | Where-Object Name -Like '.*' | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force
        }
    } else {
        $Stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
        $ManagedBackup = Join-Path $InstallDir ".yuki\backups\installer-$Stamp"
        foreach ($Relative in @('docker-compose.yml', '.env.example', 'install.sh', 'install.ps1', "Yuki-$Version-Upgrade.md")) {
            $SourceFile = Join-Path $Source $Relative
            if (-not (Test-Path -LiteralPath $SourceFile -PathType Leaf)) {
                Fail "Release bundle is missing $Relative."
            }
            $TargetFile = Join-Path $InstallDir $Relative
            if (Test-Path -LiteralPath $TargetFile -PathType Leaf) {
                [System.IO.Directory]::CreateDirectory($ManagedBackup) | Out-Null
                Copy-Item -LiteralPath $TargetFile -Destination (Join-Path $ManagedBackup $Relative) -Force
            }
            $TemporaryTarget = "$TargetFile.yuki-new"
            Copy-Item -LiteralPath $SourceFile -Destination $TemporaryTarget -Force
            Move-Item -LiteralPath $TemporaryTarget -Destination $TargetFile -Force
        }
        Write-Host "Updated release-managed deployment files; mutable data and configuration were preserved." -ForegroundColor Green
    }

    if (-not $Existing -and (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        if (Get-NetTCPConnection -LocalPort 6099 -State Listen -ErrorAction SilentlyContinue) {
            Fail "TCP port 6099 is already in use."
        }
    }

    $Image = "${BotImage}:$Version"
    Write-Host "Pulling $Image" -ForegroundColor Cyan
    & docker pull $Image
    if ($LASTEXITCODE -ne 0) { Fail "Unable to pull $Image." }

    & docker run --rm -it `
        --entrypoint qq-ai-bot-cli `
        --volume "${InstallDir}:/deploy" `
        --workdir /deploy `
        $Image setup --deployment-root /deploy
    if ($LASTEXITCODE -ne 0) { Fail "Guided setup did not complete." }

    $Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $PrivateTargets = @(
        @{
            Path = (Join-Path $InstallDir ".env")
            Permission = "${Identity}:(F)"
            Recurse = $false
        },
        @{
            Path = (Join-Path $InstallDir ".yuki\backups")
            Permission = "${Identity}:(OI)(CI)F"
            Recurse = $true
        }
    )
    foreach ($Target in $PrivateTargets) {
        if (Test-Path -LiteralPath $Target.Path) {
            $AclArguments = @($Target.Path, "/inheritance:r", "/grant:r", $Target.Permission)
            if ($Target.Recurse) { $AclArguments += @("/T", "/C") }
            & icacls @AclArguments *> $null
            if ($LASTEXITCODE -ne 0) { Fail "Unable to restrict configuration ACLs." }
        }
    }

    Push-Location $InstallDir
    try {
        & docker compose config --quiet
        if ($LASTEXITCODE -ne 0) { Fail "docker compose config failed." }
        & docker compose pull
        if ($LASTEXITCODE -ne 0) { Fail "docker compose pull failed." }
        $OldBot = (& docker compose ps -q bot).Trim()
        & docker compose up -d
        if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed." }

        function Wait-ForBot {
            $Deadline = [DateTime]::UtcNow.AddSeconds(180)
            while ([DateTime]::UtcNow -lt $Deadline) {
                $Container = (& docker compose ps -q bot).Trim()
                if ($Container) {
                    $Status = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $Container).Trim()
                    if ($Status -eq 'healthy') { return $true }
                    if ($Status -eq 'exited') { return $false }
                }
                Start-Sleep -Seconds 2
            }
            return $false
        }

        function Wait-ForService([string]$Service) {
            $Deadline = [DateTime]::UtcNow.AddSeconds(180)
            while ([DateTime]::UtcNow -lt $Deadline) {
                $Container = (& docker compose --profile speech ps -q $Service).Trim()
                if ($Container) {
                    $Status = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $Container).Trim()
                    if ($Status -eq 'healthy') { return $true }
                    if ($Status -eq 'exited') { return $false }
                }
                Start-Sleep -Seconds 2
            }
            return $false
        }

        if (-not (Wait-ForBot)) { Fail "Bot did not become healthy within 180 seconds." }
        $NewBot = (& docker compose ps -q bot).Trim()
        if ((Test-Path -LiteralPath "data/setup/restart-required") -and ($OldBot -ne $NewBot)) {
            Remove-Item -LiteralPath "data/setup/restart-required" -Force
        }
        $SpeechActionPath = "data/setup/speech-action"
        if (Test-Path -LiteralPath $SpeechActionPath) {
            $SpeechAction = (Get-Content -LiteralPath $SpeechActionPath -Raw).Trim()
            if ($SpeechAction -eq 'start') {
                & docker compose --profile speech up -d --no-deps genie-tts-worker
                if ($LASTEXITCODE -ne 0) { Fail "Speech Worker could not be started." }
                if (-not (Wait-ForService "genie-tts-worker")) { Fail "Speech Worker did not become healthy." }
            } elseif ($SpeechAction -eq 'stop') {
                & docker compose --profile speech stop genie-tts-worker
                if ($LASTEXITCODE -ne 0) { Fail "Speech Worker could not be stopped." }
                & docker compose --profile speech rm -f genie-tts-worker
                if ($LASTEXITCODE -ne 0) { Fail "Speech Worker container could not be removed." }
            } else {
                Fail "Unknown pending Speech action."
            }
            Remove-Item -LiteralPath $SpeechActionPath -Force
        }
        if (Test-Path -LiteralPath "data/setup/pending.json") {
            & docker compose exec -T bot qq-ai-bot-cli setup apply-pending --deployment-root /app --no-color
            if ($LASTEXITCODE -ne 0) { Fail "Plugin choices could not be applied." }
        }
        if (Test-Path -LiteralPath "data/setup/restart-required") {
            & docker compose restart bot
            if ($LASTEXITCODE -ne 0) { Fail "Bot could not be restarted after configuration." }
            if (-not (Wait-ForBot)) { Fail "Bot did not become healthy after configuration." }
            Remove-Item -LiteralPath "data/setup/restart-required" -Force
        }
        & docker compose exec -T bot qq-ai-bot-cli setup verify --deployment-root /app --no-color
        if ($LASTEXITCODE -ne 0) { Fail "Final health verification failed." }
    } finally {
        Pop-Location
    }
} finally {
    if (Test-Path -LiteralPath $Temporary) {
        Remove-Item -LiteralPath $Temporary -Recurse -Force
    }
}
