@echo off
chcp 65001 > NUL
cd /d "%~dp0"
echo ============================================
echo   Crexi 账号登录窗口（只需做一次）
echo ============================================
echo.
echo 接下来会弹出一个 Chrome 窗口，请：
echo   1. 在窗口里登录你的 Crexi 账号（手机/邮箱验证照常完成）
echo   2. 看到右上角是自己的账号后，直接关闭这个 Chrome 窗口
echo   3. 登录状态会自动保存，之后核查任务自动使用，不用再登录
echo.
echo （登录过程中不要关这个黑色小窗口，关 Chrome 窗口后它会自己退出）
echo.
.venv\Scripts\python.exe _crexi_login.py
echo.
echo 登录窗口已关闭。
pause
