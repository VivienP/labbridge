[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$repositoryPrefix = $repository.TrimEnd('\') + '\'
$temporaryParent = Join-Path ([System.IO.Path]::GetTempPath()) 'labbridge-cv-passport-demo'
$copy = Join-Path $temporaryParent ([guid]::NewGuid().ToString('N'))
$project = 'labbridge-cv-passport-clean-' + [guid]::NewGuid().ToString('N').Substring(0, 12)

New-Item -ItemType Directory -Path $copy -Force | Out-Null
$copy = (Resolve-Path -LiteralPath $copy).Path

try {
    $files = @(git -C $repository ls-files --cached --others --exclude-standard)
    if ($LASTEXITCODE -ne 0 -or $files.Count -eq 0) {
        throw 'git ls-files returned no clean-copy inputs'
    }
    foreach ($relative in $files) {
        $source = (Resolve-Path -LiteralPath (Join-Path $repository $relative)).Path
        if (-not $source.StartsWith(
                $repositoryPrefix,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Refusing out-of-repository source: $source"
        }
        $destination = Join-Path $copy $relative
        $destinationParent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination
    }

    Push-Location $copy
    try {
        $env:LABBRIDGE_COMPOSE_PROJECT = $project
        $env:LABBRIDGE_DEMO_CONTAINER = "$project-app-1"
        docker compose --profile demo up -d --build --wait
        if ($LASTEXITCODE -ne 0) { throw 'the exact demo command failed' }

        $root = Invoke-WebRequest -UseBasicParsing http://localhost:8000/
        if ($root.StatusCode -ne 200 -or $root.Content -notmatch 'LabBridge.+CV Passport') {
            throw 'the production page did not answer at http://localhost:8000/'
        }

        Push-Location (Join-Path $copy 'frontend')
        try {
            npm ci
            if ($LASTEXITCODE -ne 0) { throw 'npm ci failed in the clean copy' }
            npm run e2e
            if ($LASTEXITCODE -ne 0) { throw 'the clean-copy browser flow failed' }
        }
        finally {
            Pop-Location
        }
    }
    finally {
        docker compose --profile demo down --volumes --remove-orphans
        Pop-Location
    }
}
finally {
    if (Test-Path -LiteralPath $copy) {
        $resolvedCopy = (Resolve-Path -LiteralPath $copy).Path
        $resolvedParent = (Resolve-Path -LiteralPath $temporaryParent).Path.TrimEnd('\') + '\'
        if (-not $resolvedCopy.StartsWith(
                $resolvedParent,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Refusing out-of-temporary cleanup target: $resolvedCopy"
        }
        Remove-Item -LiteralPath $resolvedCopy -Recurse -Force
    }
    Remove-Item Env:LABBRIDGE_COMPOSE_PROJECT -ErrorAction SilentlyContinue
    Remove-Item Env:LABBRIDGE_DEMO_CONTAINER -ErrorAction SilentlyContinue
}

Write-Output "Clean-copy CV Passport demo verified with project $project"
