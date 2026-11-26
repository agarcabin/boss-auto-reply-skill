param([Parameter(Mandatory=$true)][string]$RequestFile)
$ErrorActionPreference='Stop'
$r=Get-Content -LiteralPath $RequestFile -Raw -Encoding utf8 | ConvertFrom-Json
if(!(Test-Path -LiteralPath $r.pythonw) -or !(Test-Path -LiteralPath $r.controller)){throw 'mapping_launcher_paths_missing'}
$arguments='"'+$r.controller+'" --inventory "'+$r.inventory+'"'
# Explorer's same-user ShellExecute keeps the interactive window independent of the tool job.
$shell=New-Object -ComObject Shell.Application
$shell.ShellExecute($r.pythonw,$arguments,[IO.Path]::GetDirectoryName($r.controller),'open',0)
