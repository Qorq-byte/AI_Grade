@echo off
chcp 65001 >nul
echo ========================================
echo   AI绩效评价报告生成系统 - 后端启动
echo ========================================
echo.

REM 检查虚拟环境
if not exist ".venv\Scripts\activate.bat" (
    echo [错误] 未找到虚拟环境，请先运行: python -m venv .venv
    pause
    exit /b 1
)

REM 激活虚拟环境
call .venv\Scripts\activate.bat

REM 检查依赖
echo [1/3] 检查依赖包...
pip list | findstr fastapi >nul
if errorlevel 1 (
    echo [提示] 正在安装依赖包...
    pip install -r requirements.txt
)

REM 检查.env文件
echo [2/3] 检查环境配置...
if not exist ".env" (
    echo [警告] 未找到.env文件，复制.env.example作为模板
    copy .env.example .env
    echo [提示] 请编辑.env文件，配置API密钥等信息
)

REM 启动服务
echo [3/3] 启动后端服务...
echo.
python -m backend.main

pause
