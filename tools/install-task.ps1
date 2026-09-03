<#
    Enregistre la tache planifiee Windows qui lance le trieur une fois par jour.

    .\tools\install-task.ps1                    # simulation quotidienne a 08:00
    .\tools\install-task.ps1 -Apply             # applique reellement
    .\tools\install-task.ps1 -Apply -At 21:30   # a une autre heure
    .\tools\install-task.ps1 -Remove            # desinstalle la tache

    WSL2 s'eteint quand plus rien n'y tourne : un cron interne raterait ses
    rendez-vous. Le planificateur Windows, lui, reveille WSL a la demande.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "Trieur Gmail",
    [string]$At = "08:00",
    [string]$Distro = "Ubuntu",
    [switch]$Apply,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Tache '$TaskName' supprimee."
    exit 0
}

# Chemin du projet vu depuis WSL, deduit de l'emplacement de ce script.
$projetWin = Split-Path -Parent $PSScriptRoot
$drive = $projetWin.Substring(0, 1).ToLower()
$projetWsl = "/mnt/$drive" + $projetWin.Substring(2).Replace('\', '/')

$flag = if ($Apply) { " --apply" } else { "" }
$cmd = "cd '$projetWsl' && exec bash tools/run_daily.sh$flag"

$action = New-ScheduledTaskAction -Execute "wsl.exe" `
    -Argument "-d $Distro -e bash -lc `"$cmd`""

# StartWhenAvailable : si le PC etait eteint a l'heure dite, la passe est
# rattrapee au demarrage suivant plutot que sautee.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

$trigger = New-ScheduledTaskTrigger -Daily -At $At

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Trie la boite Gmail une fois par jour" `
    -Force | Out-Null

Write-Host "Tache '$TaskName' enregistree."
Write-Host "  quand   : tous les jours a $At (rattrapee si le PC etait eteint)"
Write-Host "  mode    : $(if ($Apply) { 'APPLIQUE les libelles' } else { 'SIMULATION (rapport seulement)' })"
Write-Host "  commande: wsl.exe -d $Distro -e bash -lc `"$cmd`""
Write-Host "  journal : $projetWin\state\logs\"
Write-Host ""
Write-Host "Tester tout de suite  : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Voir le dernier resultat : Get-ScheduledTaskInfo -TaskName '$TaskName'"
