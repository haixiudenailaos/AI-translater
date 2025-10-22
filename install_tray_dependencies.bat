@echo off
echo ================================================
echo 安装批量翻译系统托盘功能依赖库
echo ================================================
echo.

echo 正在安装 pystray 和 Pillow...
pip install pystray Pillow

echo.
echo ================================================
echo 安装完成！
echo ================================================
echo.
echo 现在可以使用以下功能：
echo - 最小化批量翻译窗口到系统托盘
echo - 托盘图标显示和菜单控制
echo - 后台运行翻译任务
echo.
echo 运行测试: python test_system_tray.py
echo.
pause
