# kit bootstrap for Windows (PowerShell, no admin):
#   irm https://raw.githubusercontent.com/kevaldeepak/kit/main/bootstrap.ps1 | iex
#
# Installs uv (user-level), a private Python, downloads kit and hands over
# to `kit setup`. Safe to re-run: it updates kit in place.
#
# Env overrides: KIT_REPO (owner/name), KIT_REF (branch), KIT_SOURCE (local
# folder instead of GitHub), KIT_HOME, KIT_NONINTERACTIVE=1.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $repo = if ($env:KIT_REPO) { $env:KIT_REPO } else { 'kevaldeepak/kit' }
    $ref = if ($env:KIT_REF) { $env:KIT_REF } else { 'main' }
    $kitHome = if ($env:KIT_HOME) { $env:KIT_HOME } else { Join-Path $env:LOCALAPPDATA 'kit' }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("kit-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null

    try {
        Write-Host '[1/4] Getting uv (Python manager, user-level)'
        $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
        $uvLocal = Join-Path $HOME '.local\bin\uv.exe'
        if (-not $uv -and (Test-Path $uvLocal)) { $uv = $uvLocal }
        if (-not $uv) {
            $env:UV_NO_MODIFY_PATH = '1'
            $env:INSTALLER_NO_MODIFY_PATH = '1'
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression | Out-Null
            $uv = $uvLocal
        }
        if (-not (Test-Path $uv)) { throw 'uv install failed.' }

        Write-Host "[2/4] Setting up Python in $kitHome\venv"
        $py = Join-Path $kitHome 'venv\Scripts\python.exe'
        if (-not (Test-Path $py)) {
            & $uv venv --quiet --python 3.12 (Join-Path $kitHome 'venv')
            if ($LASTEXITCODE -ne 0) { throw 'Creating the Python environment failed.' }
        }
        & $uv pip install --quiet --python $py pyyaml
        if ($LASTEXITCODE -ne 0) { throw 'Installing pyyaml failed.' }

        if ($env:KIT_SOURCE) {
            Write-Host "[3/4] Using local copy $env:KIT_SOURCE"
            $src = (Resolve-Path $env:KIT_SOURCE).Path
            $spec = $src
        } else {
            Write-Host "[3/4] Downloading kit ($repo@$ref)"
            $zip = Join-Path $tmp 'kit.zip'
            Invoke-WebRequest -UseBasicParsing "https://codeload.github.com/$repo/zip/refs/heads/$ref" -OutFile $zip
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $src = (Get-ChildItem -Directory $tmp | Select-Object -First 1).FullName
            $spec = "github:$repo@$ref"
        }

        Write-Host '[4/4] Installing kit'
        $env:KIT_HOME = $kitHome
        $env:PYTHONPATH = $src
        $env:PYTHONUTF8 = '1'
        & $py -m kit.cli setup --source $spec --from $src
        $code = $LASTEXITCODE
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        $bin = Join-Path $kitHome 'bin'
        if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$bin;$env:Path" }
        if ($code -ne 0) { throw "kit setup exited with code $code" }
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}
