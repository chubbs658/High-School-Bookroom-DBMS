<#
  fix-stripped-anchor-tags.ps1

  Reverses the corruption found in assign.html, students.html, and base.html:
  a blank/whitespace-only line immediately followed by a bare `href="...` line
  (the opening `<a` on its own line got wiped out, most likely by an earlier
  PowerShell -replace regex that matched blank lines too broadly).

  This script re-inserts "<a" on that blank line, using the same indentation
  as the href line below it. It only touches lines matching that exact
  signature, so it will not affect single-line <a href="..."> tags (those
  were never corrupted) or any other blank lines in the file.

  USAGE:
    1. Back up your templates folder first (or make sure it's committed to
       git) - this edits files in place.
    2. From the project root:
         .\fix-stripped-anchor-tags.ps1
    3. Review the "Fixed:" lines it prints, then diff/spot-check a few files.

  By default this scans .\templates recursively. Change $TemplatesPath below
  if your templates live somewhere else.
#>

$TemplatesPath = ".\templates"

if (-not (Test-Path $TemplatesPath)) {
    Write-Host "Could not find '$TemplatesPath'. Edit `$TemplatesPath at the top of this script." -ForegroundColor Red
    exit 1
}

$pattern = '(?m)^[ \t]*\r?\n([ \t]*)href="'
$replacement = '$1<a' + "`r`n" + '$1href="'

$filesChanged = 0

Get-ChildItem -Path $TemplatesPath -Filter *.html -Recurse | ForEach-Object {
    $path = $_.FullName
    $original = Get-Content -Raw -LiteralPath $path

    $fixed = [regex]::Replace($original, $pattern, $replacement)

    if ($fixed -ne $original) {
        Set-Content -LiteralPath $path -Value $fixed -NoNewline
        $matchCount = [regex]::Matches($original, $pattern).Count
        Write-Host "Fixed: $path  ($matchCount tag(s) restored)" -ForegroundColor Green
        $filesChanged++
    }
}

if ($filesChanged -eq 0) {
    Write-Host "No corrupted anchor tags found. Nothing changed." -ForegroundColor Yellow
} else {
    Write-Host "`nDone. $filesChanged file(s) updated. Please review the changes before committing." -ForegroundColor Cyan
}
