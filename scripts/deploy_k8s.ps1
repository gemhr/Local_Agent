[CmdletBinding()]
param(
    [ValidateSet('base','local')][string]$Overlay = 'base',
    [Parameter(Mandatory=$true)][string]$Context,
    [switch]$AllowNonLocalContext,
    [string]$Image = 'localagent:stage6-wp8'
)

$ErrorActionPreference = 'Stop'
$manifest = Join-Path $PSScriptRoot "..\deploy\k8s\$Overlay"
$namespace = if ($Overlay -eq 'local') { 'localagent-stage6-wp8' } else { 'localagent' }
$kubectlArgs = @('--context', $Context)

$current = ([string](& kubectl config current-context 2>$null)).Trim()
Write-Host "Current Kubernetes context: $current"
Write-Host "Explicit deployment context: $Context"
$isClearlyLocal = (
    $Context -in @('docker-desktop', 'minikube', 'kind') -or
    $Context -match '(?i)^kind[-_.]' -or
    $Context -match '(?i)(^|[-_.])(local|test|dev)($|[-_.])'
)
if (-not $AllowNonLocalContext -and -not $isClearlyLocal) {
    throw 'Context does not look local/test. Re-run with -AllowNonLocalContext only for an explicitly authorized non-production test cluster.'
}
& kubectl @kubectlArgs cluster-info | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Kubernetes cluster is not reachable.' }

$renderedManifest = (& kubectl @kubectlArgs kustomize $manifest) -join "`n"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($renderedManifest)) { throw 'Manifest rendering failed.' }
$renderedManifest | & kubectl @kubectlArgs apply --dry-run=client -f - | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Manifest client validation failed.' }
$documents = $renderedManifest -split '(?m)^---\s*$' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

function Apply-Docs([string[]]$docs) {
    foreach ($doc in $docs) {
        if (-not [string]::IsNullOrWhiteSpace($doc)) {
            $rendered = $doc -replace 'image:\s*localagent:stage6-wp8', "image: $Image"
            $rendered | & kubectl @kubectlArgs apply -f - --server-side=false | Out-Host
            if ($LASTEXITCODE -ne 0) { throw 'Manifest apply failed.' }
        }
    }
}

# Namespace、配置和 Service 先落地；只使用 Kustomize 渲染结果，避免资源进入 default Namespace。
Apply-Docs @($documents | Where-Object { $_ -match '(?m)^kind: (Namespace|ServiceAccount|ConfigMap|Service)\s*$' })
if ($Overlay -eq 'local') {
    Apply-Docs @($documents | Where-Object { $_ -match '(?m)^kind: Secret\s*$' })
    Apply-Docs @($documents | Where-Object {
        $_ -match '(?m)^kind: Deployment\s*$' -and $_ -match '(?m)^  name: (postgres|redis|kafka)\s*$'
    })
    foreach ($dependency in @('postgres','redis','kafka')) {
        & kubectl @kubectlArgs -n $namespace rollout status "deployment/$dependency" --timeout=10m | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Local infrastructure rollout failed: $dependency" }
    }
}
if (-not (& kubectl @kubectlArgs -n $namespace get secret localagent-secrets --ignore-not-found -o name)) {
    throw "Secret localagent-secrets is missing in namespace $namespace. Create it without printing values."
}

foreach ($job in @('localagent-migrate','localagent-kafka-init')) {
    & kubectl @kubectlArgs -n $namespace delete job $job --ignore-not-found | Out-Host
}
Apply-Docs @($documents | Where-Object { $_ -match '(?m)^kind: Job\s*$' -and $_ -match '(?m)^  name: localagent-migrate\s*$' })
& kubectl @kubectlArgs -n $namespace wait --for=condition=complete --timeout=10m job/localagent-migrate | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Migration Job did not complete.' }
Apply-Docs @($documents | Where-Object { $_ -match '(?m)^kind: Job\s*$' -and $_ -match '(?m)^  name: localagent-kafka-init\s*$' })
& kubectl @kubectlArgs -n $namespace wait --for=condition=complete --timeout=10m job/localagent-kafka-init | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Kafka init Job did not complete.' }

Apply-Docs @($documents | Where-Object {
    $_ -match '(?m)^kind: Deployment\s*$' -and $_ -match '(?m)^  name: localagent-(api|outbox-publisher|evaluation-worker)\s*$'
})

foreach ($deployment in @('localagent-api','localagent-outbox-publisher','localagent-evaluation-worker')) {
    & kubectl @kubectlArgs -n $namespace rollout status "deployment/$deployment" --timeout=10m | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Rollout failed: $deployment" }
}
Write-Host 'Deployment completed. ConfigMap/Secret changes require a rollout or restart.'
