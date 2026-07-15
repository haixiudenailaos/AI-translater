#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻小说翻译器V1.6
主程序入口
"""

import tkinter as tk
from tkinter import ttk, messagebox
import sys

from src.utils.logger import setup_logging, get_logger

logger = get_logger(__name__)


class TranslatorApp:
    def __init__(self, app_paths=None):
        self.app_paths = app_paths
        self.root = tk.Tk()
        self.config_manager = None
        self.main_window = None
        self._loading_frame = None
        self.setup_app()

    def setup_app(self):
        """设置应用程序基本配置"""
        self.root.title("轻小说翻译器V1.6")
        self.root.geometry("1000x700")
        self.root.minsize(800, 600)

        # 设置关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Paint a minimal first frame before loading the application graph.
        self._loading_frame = ttk.Frame(self.root, padding=24)
        self._loading_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        ttk.Label(self._loading_frame, text="正在启动...").pack(pady=(0, 12))
        loading_progress = ttk.Progressbar(
            self._loading_frame, mode="indeterminate", length=220
        )
        loading_progress.pack()
        loading_progress.start(12)
        self.root.after_idle(self._initialize_main_window)

    def _initialize_main_window(self):
        """Load non-essential modules after Tk has painted the first frame."""
        try:
            if self.app_paths is None:
                from src.app_paths import AppPaths

                self.app_paths = AppPaths.create()
            setup_logging(log_dir=self.app_paths.log_dir)

            try:
                icon_path = self.app_paths.resource_dir / "assets" / "icon.ico"
                if icon_path.exists():
                    self.root.iconbitmap(str(icon_path))
            except Exception:
                pass

            try:
                self.app_paths.migrate_legacy_config()
            except Exception as exc:
                logger.warning("旧配置迁移失败: %s", exc)

            from src.config.config_manager import ConfigManager
            from src.ui.main_window import MainWindow

            self.config_manager = ConfigManager(app_paths=self.app_paths)
            self.main_window = MainWindow(
                self.root,
                self.config_manager,
                app_paths=self.app_paths,
            )
            if self._loading_frame is not None:
                self._loading_frame.destroy()
                self._loading_frame = None
        except Exception as exc:
            logger.exception("应用初始化失败")
            messagebox.showerror("启动错误", f"应用启动失败: {exc}")
            self.root.destroy()

    def on_closing(self):
        """应用关闭时的处理"""
        try:
            # BUG-005：先停止翻译、关闭所有引擎，再保存配置和销毁窗口
            if self.main_window is not None:
                self.main_window.close()
            # 保存配置
            if self.config_manager is not None:
                self.config_manager.save_config()
            self.root.destroy()
        except Exception as e:
            logger.error("关闭应用时出错: %s", e)
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
