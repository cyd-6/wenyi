param(
    [ValidateSet('list', 'export')][string]$Command = 'list',
    [string]$Output,
    [string[]]$Project,
    [string]$ComposeFile = 'docker-compose.yml'
)
$ErrorActionPreference = 'Stop'
function Invoke-Docker { & docker @args; if ($LASTEXITCODE -ne 0) { throw "Docker command failed ($LASTEXITCODE)" } }
$helper = Join-Path $PSScriptRoot 'wenyi-transfer.pyz'
if (-not (Test-Path -LiteralPath $helper)) { throw 'Missing wenyi-transfer.pyz; use the migration tools from the release ZIP.' }
if ($Command -eq 'export') {
    if (-not $Output -or -not $Project) { throw 'export requires -Output FILE.wenyi.zip -Project id1,id2' }
    if (Test-Path -LiteralPath $Output) { throw 'Output already exists; choose a new filename.' }
}
$remote = '/tmp/wenyi-transfer-' + [Guid]::NewGuid().ToString('N')
$compose = @('compose', '-f', $ComposeFile)
Invoke-Docker @compose exec -T api mkdir $remote
try {
    Invoke-Docker @compose cp $helper "api:$remote/helper.pyz"
    if ($Command -eq 'list') { Invoke-Docker @compose exec -T api python "$remote/helper.pyz" list }
    else {
        $selected = @(); foreach ($id in $Project) { $selected += @('--project', $id) }
        Invoke-Docker @compose exec -T api python "$remote/helper.pyz" export --output "$remote/projects.wenyi.zip" @selected
        Invoke-Docker @compose cp "api:$remote/projects.wenyi.zip" $Output
    }
} finally { & docker @compose exec -T api rm -rf -- $remote }
