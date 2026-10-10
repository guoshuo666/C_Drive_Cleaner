@echo off
chcp 936 >nul
setlocal EnableExtensions
title C盘空间可视化清理助手

rem ============================================================
rem  C盘空间可视化清理助手 · 一键启动脚本
rem  作用：1) 启动 app.py 后台服务  2) 延迟 2 秒后自动打开浏览器
rem  安全：本脚本不删除任何文件，只负责启动服务与打开网页
rem  说明：内部调试用  start.bat selftest   只做检测，不启动服务/不开浏览器
rem ============================================================

cd /d "%~dp0"
set "PORT=5000"
set "PYCMD="

echo ============================================================
echo    C盘空间可视化清理助手 · 正在启动
echo ============================================================
echo.

rem ---------- 1. 找一个真正可用的 Python ----------
call :TRY_PY python
if defined PYCMD goto :PY_OK
call :TRY_PY py -3
if defined PYCMD goto :PY_OK
if exist "E:\python314\python.exe" call :TRY_PY "E:\python314\python.exe"
if defined PYCMD goto :PY_OK
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" call :TRY_PY "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PYCMD goto :PY_OK
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" call :TRY_PY "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PYCMD goto :PY_OK

echo [错误] 没有找到可用的 Python 解释器。
echo.
echo        请先安装 Python 3.8 或更高版本（https://www.python.org/downloads/），
echo        安装时务必勾选 "Add Python to PATH"，然后重新双击本文件。
echo.
pause
exit /b 1

:PY_OK
echo [1/4] 已找到 Python : %PYCMD%

if not exist "%~dp0app.py" (
  echo.
  echo [错误] 当前目录下找不到 app.py / index.html，
  echo        请把本脚本和它们放在同一个文件夹里。
  echo        当前目录：%~dp0
  echo.
  pause
  exit /b 1
)

rem ---------- 2. 找一个空闲端口（默认 5000） ----------
set /a TRY=5000
:PORTPICK
call :PORT_FREE %TRY%
if not errorlevel 1 goto :GOTPORT
echo       端口 %TRY% 被其它程序占用，尝试下一个端口 ...
set /a TRY+=1
if %TRY% GTR 5019 goto :PORTFAIL
goto :PORTPICK

:PORTFAIL
echo [错误] 5000-5019 端口全部被占用，无法启动服务。
echo        请关闭占用这些端口的程序后重试（可用 netstat -ano 查看）。
echo.
pause
exit /b 1

:GOTPORT
set "PORT=%TRY%"
echo [2/4] 将使用端口 : %PORT%

rem 内部调试开关：只做检测，绝不启动服务、绝不打开浏览器
if /i "%~1"=="selftest" goto :DO_SELFTEST

rem 5000 被占用时，先看看占用它的会不会就是本工具自己
if "%PORT%"=="5000" goto :START_SERVER
call :LOOK_FOR_RUNNING
if errorlevel 1 goto :OPEN
echo       端口 5000 被占用（可能是旧版本服务或其它程序），本工具改用 %PORT%。

:START_SERVER
rem ---------- 3. 启动后台服务 ----------
echo [3/4] 正在启动后台服务 ...
start "C盘空间扫描服务 - 关闭此窗口即停止服务" %PYCMD% "%~dp0app.py" --port %PORT%

rem ---------- 4. 延迟 2 秒，等页面就绪后打开浏览器 ----------
echo [4/4] 等待服务就绪 ...
ping -n 3 127.0.0.1 >nul

set /a WAIT=0
:WAITLOOP
call :PING_APP %PORT%
if not errorlevel 1 goto :OPEN
set /a WAIT+=1
if %WAIT% GEQ 15 goto :WAITFAIL
ping -n 2 127.0.0.1 >nul
goto :WAITLOOP

:WAITFAIL
echo.
echo [警告] 服务没有在 %PORT% 端口正常响应。
echo        请查看标题为 "C盘空间扫描服务" 的窗口里的提示信息。
echo        常见原因：Python 版本过低、杀毒软件拦截、或目录中缺少 index.html。
echo.
pause
exit /b 1

:OPEN
echo.
echo 正在用系统默认浏览器打开：http://127.0.0.1:%PORT%
start "" "http://127.0.0.1:%PORT%"
echo.
echo ============================================================
echo    界面地址   : http://127.0.0.1:%PORT%
echo    后台服务   : "C盘空间扫描服务" 窗口（关闭该窗口即停止服务）
echo    扫描报告   : C_Drive_Scan_Report.txt（就在本工具目录里）
echo ============================================================
echo.
echo  本工具只做安全扫描，绝不删除任何文件，请根据结果自行手动清理。
echo.
echo  按任意键关闭本窗口（后台服务会继续在另一个窗口运行）。
pause >nul
exit /b 0


rem ============================================================
rem  子过程区
rem ============================================================

rem 尝试用给定命令启动 Python；成功则写入 PYCMD
:TRY_PY
%* -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYCMD=%*"
exit /b 0

rem 端口是否空闲：空闲返回 0，被占用返回 1
:PORT_FREE
powershell -NoProfile -ExecutionPolicy Bypass -Command "try{$l=New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Parse('127.0.0.1'),%~1);$l.Start();$l.Stop();exit 0}catch{exit 1}" >nul 2>&1
if errorlevel 1 (
  rem PowerShell 不可用时退回 netstat 判断
  netstat -ano | findstr /R /C:":%~1 " >nul 2>&1
  if errorlevel 1 exit /b 0
  exit /b 1
)
exit /b 0

rem 本工具【当前版本】的服务是否已在指定端口运行：是则返回 0（旧版本不算，避免新旧混用）
:PING_APP
powershell -NoProfile -ExecutionPolicy Bypass -Command "try{$r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%~1/api/ping';if($r.StatusCode -eq 200 -and $r.Content -match 'C_Drive_Scan_Tool' -and $r.Content -match 'drilldown'){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

rem 在 5000 ~ (TRY-1) 之间寻找本工具已在运行的服务；找到返回 1 并把 PORT 设为该端口
:LOOK_FOR_RUNNING
set /a CHK=5000
:LOOK_LOOP
call :PING_APP %CHK%
if not errorlevel 1 (
  set "PORT=%CHK%"
  echo       检测到本工具的服务已经在运行，直接复用。
  exit /b 1
)
set /a CHK+=1
if %CHK% LSS %TRY% goto :LOOK_LOOP
exit /b 0

:DO_SELFTEST
echo.
echo [selftest] Python 命令 : %PYCMD%
echo [selftest] 选用端口    : %PORT%
echo [selftest] 脚本目录    : %~dp0
echo [selftest] 检测完成，未启动服务、未打开浏览器。
exit /b 0
