#!/usr/bin/env python3
"""
轻小说翻译器 V1.6
主程序入口

阶段 2（Composition Root）：main.py 只负责创建 Tk root、调用
bootstrap 和启动事件循环。所有依赖组装在 bootstrap.py 中完成。
"""

import sys

from src.runtime_compatibility import ensure_supported_python

ensure_supported_python()

import tkinter as tk
from tkinter import messagebox, ttk

from src._version import display_version
from src.bootstrap import create_app_context
from src.ui.window_geometry import WindowGeometryTracker
from src.ui.windows_dpi import enable_per_monitor_dpi_awareness
from src.utils.logger import get_logger

logger = get_logger(__name__)

PACKAGED_SMOKE_TEST_ARG = "--packaged-smoke-test"


def run_packaged_smoke_test(*, verify_gui: bool = True) -> int:
    """Exercise code paths that previously failed only inside the frozen exe."""
    import chardet

    sample = "轻小说翻译器".encode("gb18030")
    result = chardet.detect(sample)
    if not result.get("encoding"):
        return 1

    if verify_gui:
        root = tk.Tk()
        try:
            root.withdraw()
            root.update_idletasks()
        finally:
            root.destroy()
    return 0


class TranslatorApp:
    def __init__(self):
        enable_per_monitor_dpi_awareness()
        self.root = tk.Tk()
        self.app_context = None
        self.main_window = None
        self._loading_frame = None
        self._main_window_geometry_tracker = None
        self.setup_app()

    def setup_app(self):
        """设置应用程序基本配置"""
        # P1-12：标题版本来自单一版本源 src/_version.py
        self.root.title(f"轻小说翻译器 V{display_version()}")
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

            self._main_window_geometry_tracker = WindowGeometryTracker(
                self.root,
                config_manager,
                "main",
                default_size=(1000, 700),
                minimum_size=(800, 600),
            )
            self._main_window_geometry_tracker.restore()
            self._main_window_geometry_tracker.bind()

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
        """应用关闭时的处理（ENG-1：分阶段保存与关闭）

        阶段：
        1. 询问未保存文档（save/discard/cancel/proceed），保留 P0-1
           ``decision`` 参数传给 ``MainWindow.close`` 让 autosave 按选择分支。
        2. 关闭主窗口资源（停止翻译、关闭引擎、flush 自动保存）。
        3. 保存配置（聚合结果）；失败时提供 重试 / 不保存退出 / 取消 选择。
        4. 销毁窗口。

        每个阶段使用独立 try/except，避免一个宽捕获覆盖全部阶段。
        """
        # 阶段 1：未保存文档守卫（P0-1：将 decision 传给 close）
        decision = "proceed"
        try:
            if self.main_window is not None:
                # P0-5：检查未保存更改
                choice = self.main_window.confirm_save_before_close()
                if choice == "cancel":
                    return  # 用户取消，不关闭
                if choice == "save" and not self.main_window.save_and_flush():
                    return  # 保存失败，不关闭，让用户重试
                # choice == "discard" 或 "proceed"：继续关闭
                decision = choice
        except Exception as e:
            logger.error("关闭前文档保存失败: %s", e)
            if not self._confirm_continue_after_error(
                "文档保存失败",
                f"关闭前文档保存流程出错：{e}\n\n是否继续关闭？",
            ):
                return

        # 阶段 2：保存配置（ENG-1：聚合结果 + 重试/不保存退出/取消）。
        # 所有仍允许用户取消关闭的操作必须发生在资源 teardown 之前；否则
        # “取消退出”会留下看似可用、实际已关闭服务的主窗口。
        if self.app_context is not None:
            tracker = getattr(self, "_main_window_geometry_tracker", None)
            if tracker is not None:
                tracker.save()
            if not self._save_config_with_retry():
                return  # 用户取消关闭

        # 阶段 3：关闭主窗口资源。配置保存已完成或用户已明确选择不保存，
        # 因此从这里开始不再提供返回应用的分支。
        if self.main_window is not None:
            try:
                self.main_window.close(decision=decision)
            except Exception as e:
                logger.error("关闭主窗口资源失败: %s", e)
                messagebox.showwarning(
                    "关闭提示",
                    f"部分资源关闭失败：{e}\n应用仍将退出。",
                    parent=self.root,
                )

        # 阶段 4：销毁窗口
        self.root.destroy()

    def _save_config_with_retry(self) -> bool:
        """ENG-1：保存配置，失败时提供 重试 / 不保存退出 / 取消 选择。

        返回 True 表示可以继续关闭，False 表示用户取消关闭。
        - 重试：再次调用 save_config()。
        - 不保存退出：跳过保存直接关闭。
        - 取消：返回 False，应用保持打开。
        """
        from src.domain.secret import ConfigSaveResult

        while True:
            try:
                result: ConfigSaveResult = self.app_context.config_manager.save_config()
            except Exception as e:
                logger.error("保存配置时抛出异常: %s", e)
                result = None

            # 成功（含 SESSION_ONLY）：SESSION_ONLY 仅提示，不阻断退出
            if result is not None and not result.failed:
                if result.session_only:
                    messagebox.showinfo(
                        "保存提示",
                        result.user_message,
                        parent=self.root,
                    )
                return True

            # 失败：询问用户
            error_msg = result.user_message if result is not None else "配置保存时发生异常"
            choice = messagebox.askyesnocancel(
                "配置保存失败",
                f"{error_msg}\n\n选择：\n"
                "- 是：重试保存\n"
                "- 否：不保存退出（配置可能不完整）\n"
                "- 取消：返回应用",
                icon=messagebox.WARNING,
                parent=self.root,
            )
            if choice is None:
                return False  # 取消关闭
            if choice:
                continue  # 重试
            return True  # 不保存退出

    def _confirm_continue_after_error(self, title: str, message: str) -> bool:
        """通用错误确认对话框：返回 True 表示继续关闭，False 表示取消。"""
        return messagebox.askyesno(
            title,
            f"{message}\n\n选择 是 继续关闭，否 返回应用。",
            icon=messagebox.WARNING,
            parent=self.root,
        )

    def run(self):
        """启动应用"""
        self.root.mainloop()


def main():
    """主函数"""
    if PACKAGED_SMOKE_TEST_ARG in sys.argv[1:]:
        sys.exit(run_packaged_smoke_test())

    try:
        app = TranslatorApp()
        app.run()
    except Exception as e:
        messagebox.showerror("启动错误", f"应用启动失败: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
