# DocFlow GitHub Releases 一键发布
# 用法（在 docflow 目录）:
#   .\packaging\release.ps1                 # 构建 + 打包 + 发 Release
#   .\packaging\release.ps1 -SkipBuild      # 复用已构建的 frontend/dist
#   .\packaging\release.ps1 -DryRun         # 只构建+压 zip，不发 GitHub（本地验证）
#   .\packaging\release.ps1 -Notes "修复对账" -Prerelease
param(
    [string]$Notes = "",
    [switch]$SkipBuild,
    [switch]$Prerelease,
    [switch]$DryRun,
    [string]$Repo = ""      # owner/name，留空则从 git remote 解析
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$cfg  = Join-Path $root "backend\config.py"
$vtxt = Join-Path $PSScriptRoot "version_info.txt"
$relDir = Join-Path $root "release"

function Step($m) { Write-Host $m -ForegroundColor Cyan }

# ---------- 1. 读版本号（唯一来源：backend/config.py）----------
Step "[1/5] 读取版本号 backend/config.py"
$m = [regex]::Match((Get-Content $cfg -Raw), "(?m)^VERSION\s*=\s*['""]([^'""]+)['""]")
if (-not $m.Success) { throw "config.py 里没找到 VERSION" }
$version = $m.Groups[1].Value.Trim()
if ($version -notmatch "^\d+\.\d+\.\d+$") { throw "版本号 $version 不是 x.y.z 格式" }
$tag = "v$version"
Write-Host "    版本: $version  标签: $tag" -ForegroundColor Green

# ---------- 2. 用版本号重生成 version_info.txt，杜绝漂移 ----------
Step "[2/5] 重生成 packaging/version_info.txt（与版本号对齐）"
$p = $version.Split(".")
$tup = "($($p[0]), $($p[1]), $($p[2]), 0)"
$info = @"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=$tup,
    prodvers=$tup,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'DocFlow'),
        StringStruct('FileDescription', 'DocFlow 文流 · 智能文档整理'),
        StringStruct('FileVersion', '$version'),
        StringStruct('InternalName', 'DocFlow'),
        StringStruct('LegalCopyright', '本地优先，数据不出机器'),
        StringStruct('OriginalFilename', 'DocFlow.exe'),
        StringStruct('ProductName', 'DocFlow'),
        StringStruct('ProductVersion', '$version')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"@
[IO.File]::WriteAllText($vtxt, $info, (New-Object System.Text.UTF8Encoding($false)))

# ---------- 3. 构建前端 + PyInstaller 打包 ----------
if ($SkipPackage) {
    Step "[3/5] 跳过构建与打包，复用现有 dist\DocFlow"
} else {
    if (-not $SkipBuild) {
        Step "[3/5] 构建前端 npm ci && npm run build"
        Push-Location (Join-Path $root "frontend")
        try {
            & npm ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci 失败" }
            & npm run build
            if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
        } finally { Pop-Location }
    } else {
        Step "[3/5] 跳过前端构建，复用现有 frontend/dist"
    }
    if (-not (Test-Path (Join-Path $root "frontend\dist\index.html"))) { throw "frontend\dist\index.html 不存在，先构建前端" }

    Step "       PyInstaller 打包 docflow.spec"
    Push-Location $root
    try {
        & python -m PyInstaller (Join-Path $PSScriptRoot "docflow.spec") --noconfirm
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }
    } finally { Pop-Location }
}

$dist = Join-Path $root "dist\DocFlow"
if (-not (Test-Path (Join-Path $dist "DocFlow.exe"))) { throw "产物缺失：$dist\DocFlow.exe" }

# ---------- 4. 压 zip + 生成 latest.json ----------
Step "[4/5] 压缩桌面版 -> release\DocFlow-Desktop-$tag.zip"
if (-not (Test-Path $relDir)) { New-Item -ItemType Directory -Force -Path $relDir | Out-Null }
$zipName = "DocFlow-Desktop-$tag.zip"
$zip = Join-Path $relDir $zipName
if (Test-Path $zip) { Remove-Item $zip -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($dist, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)
$sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $zip).Length
$builtAt = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
if ($Notes -eq "") { $Notes = "DocFlow 桌面版 $tag" }

$latest = [ordered]@{
    version   = $version
    tag       = $tag
    asset     = $zipName
    sha256    = $sha
    size      = $size
    built_at  = $builtAt
    notes     = $Notes
    prerelease= [bool]$Prerelease
    download  = "https://github.com/<owner>/<repo>/releases/latest"
}
$latestJson = $latest | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $relDir "latest.json"), $latestJson, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("    {0}  |  {1} MB  |  sha256 {2}" -f $zipName, [math]::Round($size/1MB,1), $sha.Substring(0,12)) -ForegroundColor Green

if ($DryRun) { Step "[5/5] DryRun：已产出 zip，未发布 GitHub"; return }

# ---------- 5. 发布到 GitHub Releases ----------
Step "[5/5] 发布到 GitHub Releases"
if (-not $Repo) {
    $url = (& git -C $root remote get-url origin 2>$null)
    if ($url) {
        $Repo = ($url -replace "^.*github\.com[:/]", "" -replace "\.git$", "")
    }
}
if ($Repo -notmatch "^[\w.-]+/[\w.-]+$") {
    Write-Host "    无法确定 GitHub 仓库（owner/name）。" -ForegroundColor Yellow
    Write-Host "    请先 git remote add origin https://github.com/<owner>/<repo>.git，" -ForegroundColor Yellow
    Write-Host "    或用参数指定：.\packaging\release.ps1 -Repo <owner>/<repo>" -ForegroundColor Yellow
    Write-Host "    zip 已就绪，可直接在 Releases 页面手动上传：$zip" -ForegroundColor Yellow
    return
}

$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if ($gh) {
    & $gh auth status
    if ($LASTEXITCODE -ne 0) { throw "gh 未登录，先运行：gh auth login" }
    $exists = & $gh release view $tag --repo $Repo 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "    Release $tag 已存在，更新资产…" -ForegroundColor Yellow
        & $gh release upload $tag $zip --clobber --repo $Repo
        & $gh release edit $tag --notes $Notes --repo $Repo
    } else {
        $flags = @()
        if ($Prerelease) { $flags += "--prerelease" }
        & $gh release create $tag $zip --repo $Repo --title "DocFlow $tag" --notes $Notes @flags
    }
    if ($LASTEXITCODE -ne 0) { throw "gh release 失败" }
    Write-Host ("    OK -> https://github.com/{0}/releases/tag/{1}" -f $Repo, $tag) -ForegroundColor Green
} else {
    # 兜底：用 GitHub API。令牌来源优先 GITHUB_TOKEN，其次复用本机 git 已缓存凭据（免装 gh）
    $tok = $env:GITHUB_TOKEN
    if (-not $tok) {
        $tok = ("protocol=https`nhost=github.com`n`n" | git credential fill 2>$null |
                Where-Object { $_ -like 'password=*' }) -replace '^password=', ''
    }
    if (-not $tok) {
        Write-Host "    未找到 gh，也没有可用 GitHub 凭据。" -ForegroundColor Yellow
        Write-Host "    任选其一：winget install GitHub.cli 后 gh auth login；或设 GITHUB_TOKEN(scope: repo)；" -ForegroundColor Yellow
        Write-Host "    或在终端先 git push 一次让凭据管理器缓存令牌，再重跑本脚本。zip 已就绪：$zip" -ForegroundColor Yellow
        return
    }
    $hdr = @{ Authorization = "Bearer $tok"; Accept = "application/vnd.github+json"; "User-Agent" = "docflow-release" }
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/tags/$tag" -Headers $hdr -ErrorAction SilentlyContinue
    if (-not $rel) {
        $body = @{ tag_name = $tag; name = "DocFlow $tag"; body = $Notes; prerelease = [bool]$Prerelease } | ConvertTo-Json
        $rel = Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$Repo/releases" -Headers $hdr -Body $body -ContentType "application/json"
    }
    # 先删同名旧资产再上传（API 上传大文件走 uploads.github.com）
    foreach ($a in $rel.assets) {
        if ($a.name -eq $zipName) { Invoke-RestMethod -Method Delete -Uri $a.upload_url -Headers $hdr | Out-Null }
    }
    $upUrl = "https://uploads.github.com/repos/$Repo/releases/$($rel.id)/assets?name=$zipName"
    Invoke-RestMethod -Method Post -Uri $upUrl -Headers $hdr -InFile $zip -ContentType "application/zip" | Out-Null
    Write-Host ("    OK -> {0}" -f $rel.html_url) -ForegroundColor Green
}

Write-Host ""
Write-Host "完成。用户下载入口：https://github.com/$Repo/releases/latest" -ForegroundColor Green
