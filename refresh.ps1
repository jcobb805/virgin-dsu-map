# Weekly refresh for the Virgin DSU Screener.
# Runs fetch.py, then pops a Windows toast if new virgin-DSU permits appeared.
# Scheduled task: "Virgin DSU Weekly Refresh" (Mondays 7:00 AM, runs when available).

$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $base 'refresh.log'

"=== refresh $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" | Add-Content $log
try {
    $out = & python (Join-Path $base 'fetch.py') 2>&1 | Out-String
    $out | Add-Content $log
} catch {
    "FETCH FAILED: $_" | Add-Content $log
    exit 1
}

# Push refreshed data to GitHub Pages (live dashboard)
try {
    Set-Location $base
    git add data.js history.json | Out-Null
    $st = git status --porcelain data.js history.json
    if ($st) {
        git commit -m "Weekly refresh $(Get-Date -Format 'yyyy-MM-dd')" | Out-Null
        git push | Out-Null
        "Pushed refresh to GitHub" | Add-Content $log
    } else {
        "No data changes to push" | Add-Content $log
    }
} catch {
    "Git push failed (non-fatal): $_" | Add-Content $log
}

# Read last run stats and notify
try {
    $hist = Get-Content (Join-Path $base 'history.json') -Raw | ConvertFrom-Json
    $run = $hist.runs[-1]
    $newP = [int]$run.newPermits
    $newU = [int]$run.newUnits
    $tot = [int]$run.virginUnits

    if ($newP -gt 0) {
        $title = "Virgin DSU: $newP new permit$(if($newP -ne 1){'s'})"
        $body = "$newU new unit$(if($newU -ne 1){'s'}) with no production history. $tot virgin units total. Open the dashboard for details."
    } else {
        $title = "Virgin DSU: no new activity"
        $body = "$tot virgin units tracked. Refresh complete."
    }

    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $xml = @"
<toast><visual><binding template="ToastGeneric">
<text>$title</text><text>$body</text>
</binding></visual></toast>
"@
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(
        'Virgin DSU Screener').Show($toast)
    "Toast shown: $title" | Add-Content $log
} catch {
    "Toast failed (non-fatal): $_" | Add-Content $log
}
