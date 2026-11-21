# One-click setup for the AI Leadership Team on Windows 10/11.
# Started by double-clicking SETUP.bat. Safe to run again: it skips what is already done.
# It installs Python, Git and Ollama (free), downloads the free AI model,
# downloads this team and your knowledge base, and puts a "Leadership Team" icon on the desktop.
# It does NOT ask for or store any keys. Those are added later, by you.

$ErrorActionPreference = "Continue"
$Home_ = "C:\"
$Team = "C:\ops-agent-team"
$KB = "C:\lucent-kb"
$Model = "qwen2.5:7b"

function Say($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

if (-not (Have "winget")) {
    Write-Host "This PC has no 'winget'. Please install 'App Installer' from the Microsoft Store, then run SETUP.bat again." -ForegroundColor Red
    exit 1
}

# 1. Smaller model if the PC has less than 16 GB of memory
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
if ($ramGB -lt 15) { $Model = "qwen2.5:3b" }
Say "This PC has $ramGB GB of memory. Using the AI model $Model."

# 2. Programs
foreach ($p in @(@("python", "Python.Python.3.12"), @("git", "Git.Git"), @("ollama", "Ollama.Ollama"))) {
    if (Have $p[0]) { Say "$($p[0]) is already installed." ; continue }
    Say "Installing $($p[1]) ..."
    winget install --id $p[1] -e --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
}
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (Have "ollama") { $ollamaExe = "ollama" }

# 3. Start Ollama and download the free model
Say "Starting Ollama and downloading the free AI model (about 2 to 5 GB, can take a while) ..."
Start-Process $ollamaExe -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
Start-Sleep -Seconds 5
& $ollamaExe pull $Model

# 4. Download the team (public) and the knowledge base (private, GitHub will ask you to sign in)
if (Test-Path "$Team\.git") { Say "Updating the team code ..."; git -C $Team pull --ff-only }
else { Say "Downloading the team code ..."; git clone https://github.com/anirag1206/ops-agent-team $Team }

if (Test-Path "$KB\.git") { Say "Updating the knowledge base ..."; git -C $KB pull --ff-only }
else {
    Say "Downloading your private knowledge base. A GitHub sign-in window may open: sign in as anirag1206."
    git clone https://github.com/anirag1206/lucent-kb $KB
}

# 5. Graphify (free, open source) for the knowledge map
Say "Installing Graphify ..."
python -m pip install --quiet --upgrade graphifyy

# 6. Use the model we chose
$cfgPath = "$Team\config.json"
if (Test-Path $cfgPath) {
    (Get-Content $cfgPath -Raw) -replace '"ollama_model":\s*"[^"]*"', "`"ollama_model`": `"$Model`"" |
        Set-Content $cfgPath -Encoding UTF8
}

# 7. Desktop icon
$desk = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$desk\Leadership Team.lnk")
$lnk.TargetPath = "$Team\start_chat.bat"
$lnk.WorkingDirectory = $Team
$lnk.Save()

# 8. Quick check
Say "Checking that everything works ..."
Push-Location $Team
python run.py team
Pop-Location

Write-Host ""
Write-Host "All done. Double-click 'Leadership Team' on your desktop to talk to your team." -ForegroundColor Green
if (-not (Test-Path "$KB\README.md")) {
    Write-Host "Note: the knowledge base did not download (GitHub sign-in). Run SETUP.bat again and sign in." -ForegroundColor Yellow
}
