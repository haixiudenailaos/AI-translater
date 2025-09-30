#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量级Python翻译工具
主程序入口
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import json
import os
from pathlib import Path
import sys

# 导入自定义模块
from src.ui.main_window import MainWindow
from src.config.config_manager import ConfigManager

class TranslatorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.config_manager = ConfigManager()
        self.setup_app()
        
    def setup_app(self):
        """设置应用程序基本配置"""
        self.root.title("轻量级Python翻译工具 v1.0")
        self.root.geometry("1000x700")
        self.root.minsize(800, 600)
        
        # 设置应用图标（如果存在）
        try:
            if os.path.exists("assets/icon.ico"):
                self.root.iconbitmap("assets/icon.ico")
        except:
            pass
            
        # 创建主窗口
        self.main_window = MainWindow(self.root, self.config_manager)
        
        # 设置关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
    def on_closing(self):
        """应用关闭时的处理"""
        try:
            # 保存配置
            self.config_manager.save_config()
            self.root.destroy()
        except Exception as e:
            print(f"关闭应用时出错: {e}")
            self.root.destroy()
            
    def run(self):
        """启动应用"""
        self.root.mainloop()

def main():
    """主函数"""
    try:
        app = TranslatorApp()
        app.run()
    except Exception as e:
        messagebox.showerror("启动错误", f"应用启动失败: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()