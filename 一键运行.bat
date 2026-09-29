@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
title B站抽奖 + 关注管理

where python >nul 2>nul
if errorlevel 1 goto no_python
if not exist "config.json" goto no_config

:menu
cls
echo ==================================================
echo            B 站抽奖 + 关注管理
echo ==================================================
echo.
echo    9     打开网页版 - 表格 + 逐条勾选【推荐】
echo.
echo   -- 参与抽奖（命令行）--
echo    1     试跑 - 只看会参与哪些，一个都不会动
echo          第一次用、或者改过配置，都先跑这个
echo    2     正式参与 - 跑之前还会让你确认一次
echo.
echo   -- 管理关注（命令行）--
echo    3     看关注分类报告
echo    4     深度检查 - 找出注销号、僵尸号，认出内容分区
echo    5     取关某一类 - 会先列出来让你确认
echo.
echo   -- 其他 --
echo    c     自检 - 看 Cookie 还灵不灵、B站接口有没有下线
echo    6     安装 / 更新依赖
echo    7     打开日志文件夹
echo    8     打开配置文件
echo    0     退出
echo.
echo ==================================================
set "pick="
set /p "pick=请输入数字后按回车: "

if "%pick%"=="9" goto webui
if "%pick%"=="1" goto run_test
if "%pick%"=="2" goto run_real
if "%pick%"=="3" goto follow_report
if "%pick%"=="4" goto follow_deep
if "%pick%"=="5" goto follow_unfollow
if /i "%pick%"=="c" goto selfcheck
if "%pick%"=="6" goto install
if "%pick%"=="7" goto open_logs
if "%pick%"=="8" goto open_config
if "%pick%"=="0" exit /b 0
goto menu

:webui
cls
echo [网页版] 浏览器会自动打开。想停就回到这个窗口按 Ctrl+C。
echo 界面只监听本机 127.0.0.1，局域网里的其他设备访问不到。
echo.
python web.py
goto done

:run_test
cls
echo [试跑模式] 不会真的关注/转发/评论，看完可以放心。
echo.
python main.py --test
goto done

:run_real
cls
echo [正式模式] 下面列出的抽奖会被真的参与。
echo 每个动作之间会随机等 20~45 秒，所以跑得慢是正常的，让它自己跑完。
echo.
python main.py
goto done

:follow_report
cls
echo [关注分类] 第一次跑要先抓一遍关注列表，几千个关注大约要两三分钟。
echo.
python follow.py
goto done

:follow_deep
cls
echo [深度检查] 逐个查关注的 UP：是不是注销了、多久没更新了、主要做什么内容。
echo 每个要三次请求，查 200 个大约 8 分钟。查过的会记住，下次接着查。
echo.
set "num="
set /p "num=这次查多少个？(直接回车 = 200) "
if "%num%"=="" set "num=200"
python follow.py --deep %num%
goto done

:follow_unfollow
cls
echo [批量取关] 先看一眼有哪些类别（选 3 能看到完整报告和人数）：
echo.
echo    账号属性: 抽奖  未分组  互关  单向关注  特别关注
echo              机构号  个人认证  无认证  关注超一年  最近30天关注
echo    账号状态: 已注销  长期不更新
echo    内容分区: 游戏区  动画区  科技区  生活区 ... 二创  分区未知
echo              也可以细到 "游戏区/手机游戏"  "内容:原神"
echo    ^(内容分区要先用选项 4 做过深度检查才会出现^)
echo.
echo 想逐个挑着取关的话，用选项 9 的网页版更方便。
echo.
set "cat="
set /p "cat=要取关哪一类？(直接回车 = 取消) "
if "%cat%"=="" goto menu
set "num="
set /p "num=最多取关几个？(直接回车 = 不限) "
set "lim="
if not "%num%"=="" set "lim=--limit %num%"
echo.
set "yn="
set /p "yn=先演练不动手吗？(y = 演练, 直接回车 = 真的取关) "
if /i "%yn%"=="y" (
    python follow.py --unfollow "%cat%" %lim% --dry-run
) else (
    python follow.py --unfollow "%cat%" %lim%
)
goto done

:selfcheck
cls
echo [自检] 只读，不会关注/转发/评论任何东西。
echo B站下线接口很勤，跑不出抽奖时先来这里看一眼是不是接口挂了。
echo.
python main.py --check
echo.
echo 顺便跑一遍离线逻辑自检...
echo.
python tests.py
goto done

:install
cls
echo 正在安装依赖...
echo.
python -m pip install -r requirements.txt
goto done

:open_logs
if not exist "logs" mkdir "logs"
start "" explorer "%cd%\logs"
goto menu

:open_config
start "" notepad "%cd%\config.json"
goto menu

:done
echo.
echo ==================================================
echo 跑完了。按任意键回到菜单。
pause >nul
goto menu

:no_python
echo.
echo   × 没找到 Python
echo.
echo   请去 https://www.python.org/downloads/ 下载安装，
echo   安装时务必勾上 "Add Python to PATH" 这个选项，
echo   装完之后重新打开这个 bat。
echo.
pause
exit /b 1

:no_config
echo.
echo   × 还没有 config.json
echo.
echo   请把 config.example.json 复制一份、改名成 config.json，
echo   然后把你的 B 站 Cookie 填进去。
echo   具体步骤见 README.md 的「第 3 步」。
echo.
echo   按任意键打开当前文件夹...
pause >nul
start "" explorer "%cd%"
exit /b 1
