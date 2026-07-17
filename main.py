#!/usr/bin/env python3
"""
轻小说翻译器V1.6
主程序入口

阶段 2（Composition Root）：main.py 只负责创建 Tk root、调用
bootstrap 和启动事件循环。所有依赖组装在 bootstrap.py 中完成。
"""

import sys
import tkinter as tk
from tkinter import messagebox, ttk

from src.bootstrap import create_app_context
from src.utils.logger import get_logger

logger = get_logger(__name__)


class TranslatorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.app_context = None
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
        loading_progress = ttk.Progressbar(self._loading_frame, mode="indeterminate", length=220)
        loading_progress.pack()
        loading_progress.start(12)
        self.root.after_idle(self._initialize_main_window)

    def _initialize_main_window(self):
        """Load non-essential modules after Tk has painted the first frame."""
        try:
            # 阶段 2：通过 Composition Root 组装所有依赖
            self.app_context = create_app_context()
            app_paths = self.app_context.app_paths
            config_manager = self.app_context.config_manager

            try:
                icon_path = app_paths.resource_dir / "assets" / "icon.ico"
                if icon_path.exists():
                    self.root.iconbitmap(str(icon_path))
            except (OSError, tk.TclError):
                pass  # 最佳努力：图标加载失败不影响启动

            from src.ui.main_window import MainWindow

            self.main_window = MainWindow(
                self.root,
                config_manager,
                app_paths=app_paths,
            )
            if self._loading_frame is not None:
                self._loading_frame.destroy()
                self._loading_frame = None
        except Exception as exc:
            logger.exception("应用初始化失败")
            messagebox.showerror("启动错误", f"应用启动失败: {exc}")
            self.root.destroy()

    def on_closing(self):
        """应用关闭时的处理

        P0-5：关闭前检查未保存更改，提供 Save/Discard/Cancel 选择。
        保存失败时不退出，保留窗口让用户重试。
        """
        try:
            if self.main_window is not None:
                # P0-5：检查未保存更改
                choice = self.main_window.confirm_save_before_close()
                if choice == "cancel":
                    return  # 用户取消，不关闭
                if choice == "save" and not self.main_window.save_and_flush():
                    return  # 保存失败，不关闭，让用户重试
                # choice == "discard" 或 "proceed"：继续关闭

            # BUG-005：先停止翻译、关闭所有引擎，再保存配置和销毁窗口
            if self.main_window is not None:
                self.main_window.close()
            # 保存配置
            if self.app_context is not None:
                self.app_context.config_manager.save_config()
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
