# AI绩效评价报告生成系统 - 后端启动脚本

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  AI绩效评价报告生成系统 - 后端启动" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 检查虚拟环境
if (-Not (Test-Path ".venv\Scripts\Activate.ps1")) {
    Write-Host "[错误] 未找到虚拟环境，请先运行: python -m venv .venv" -ForegroundColor Red
    pause
    exit 1
}

# 激活虚拟环境
& .\.venv\Scripts\Activate.ps1

# 检查依赖
Write-Host "[1/3] 检查依赖包..." -ForegroundColor Yellow
$fastapiInstalled = pip list | Select-String "fastapi"
if (-Not $fastapiInstalled) {
    Write-Host "[提示] 正在安装依赖包..." -ForegroundColor Yellow
    pip install -r requirements.txt
}

# 检查.env文件
Write-Host "[2/3] 检查环境配置..." -ForegroundColor Yellow
if (-Not (Test-Path ".env")) {
    Write-Host "[警告] 未找到.env文件，复制.env.example作为模板" -ForegroundColor Yellow
    Copy-Item .env.example .env
    Write-Host "[提示] 请编辑.env文件，配置API密钥等信息" -ForegroundColor Yellow
}

# 启动服务
Write-Host "[3/3] 启动后端服务..." -ForegroundColor Green
Write-Host ""
python -m backend.main

pause
