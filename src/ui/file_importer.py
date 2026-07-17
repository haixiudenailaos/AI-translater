#!/usr/bin/env python3
"""
文件导入控制器模块
负责处理文件导入、EPUB解析、剪贴板导入等功能
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, List

from ..application.document_session import (
    DocumentSession,
    ImportFailure,
    ImportResult,
    SessionKind,
)
from .ui_callback_mailbox import TkUICallbackPump, UICallbackMailbox


class FileImporter:
    """文件导入控制器

    P0-3/P0-6：会话状态以 ``DocumentSession`` 为唯一真相来源。导入流程
    先构建 ``ImportResult``（区分源读取失败、目标读取失败、目标不可写、
    mapping 失败），仅在成功时在 Tk 主线程整体替换 ``self._session``；
    失败时旧 session 完全不变。

    ``current_source_path`` / ``current_target_path`` / ``current_mapping_dir``
    保留为只读属性，向旧调用方提供向后兼容视图，外部代码无需改动。
    """

    def __init__(
        self,
        root: tk.Tk,
        config_manager,
        file_handler,
        epub_processor,
        table_loader: Callable,
        status_updater: Callable,
        image_translation_starter: Callable,
    ):
        """
        初始化文件导入控制器

        Args:
            root: Tkinter根窗口
            config_manager: 配置管理器
            file_handler: 文件处理器
            epub_processor: EPUB处理器
            table_loader: 加载数据到表格的回调函数
            status_updater: 更新状态栏的回调函数
            image_translation_starter: 启动图片翻译的回调函数
        """
        self.root = root
        self.config_manager = config_manager
        self.file_handler = file_handler
        self.epub_processor = epub_processor
        self.table_loader = table_loader
        self.status_updater = status_updater
        self.image_translation_starter = image_translation_starter

        # P0-3/P0-6：会话状态唯一真相来源，所有路径字段通过属性委派
        self._session: DocumentSession = DocumentSession.empty()
        # EPUB 运行时缓存键列表（不参与 session 原子性，单独维护）
        self.current_mapping_keys: List[str] = []

        # P1-1：UI 回调邮箱 + Tk 主线程事件泵
        # 工作线程只调用 ``_ui_mailbox.submit(func)``，不再直接调用 ``root.after()``
        # 或 ``winfo_exists()``（跨线程 Tk 调用）。
        # 仅当 root 支持 Tk ``after`` 时启动 pump（测试替身可能不支持）。
        self._ui_mailbox = UICallbackMailbox()
        self._ui_pump: TkUICallbackPump | None = None
        if hasattr(root, "after"):
            self._ui_pump = TkUICallbackPump(root, self._ui_mailbox)
            self._ui_pump.start()

    def close(self) -> None:
        """P1-1：关闭 UI 回调事件泵，释放 ``after`` 调度。

        应在 ``MainWindow.on_closing`` 中调用，确保退出时取消 pump 调度。
        幂等。
        """
        pump = self._ui_pump
        self._ui_pump = None
        if pump is not None:
            pump.close()

    # ── 向后兼容的会话只读视图 ───────────────────────────────

    @property
    def current_source_path(self) -> Path | None:
        """当前会话的原文文件路径（向后兼容属性）。"""
        return self._session.source_path

    @property
    def current_target_path(self) -> Path | None:
        """当前会话的译文文件路径（向后兼容属性）。"""
        return self._session.target_path

    @property
    def current_mapping_dir(self) -> Path | None:
        """当前会话的 EPUB 映射目录（向后兼容属性）。"""
        return self._session.mapping_dir

    @property
    def session(self) -> DocumentSession:
        """当前文档会话（只读访问，替换请使用 ``_commit_session``）。"""
        return self._session

    def _safe_after(self, target_window, func):
        """P1-1：通过 UI 回调邮箱提交回调，不在工作线程调用 Tk API。

        ``target_window`` 参数保留以兼容旧调用方，但不再从中调用
        ``winfo_exists()``（跨线程 Tk 调用）。回调执行时若目标窗口已销毁，
        pump 的异常处理器会捕获并记录 ``TclError``，不会中断后续回调。
        """
        self._ui_mailbox.submit(func)

    def import_file(self, file_path=None):
        """导入文件并自动检测/创建对应译文文件，加载译文并定位到末尾

        P0-6：导入错误不再被静默吞掉。源文件读取失败、译文文件读取失败、
        译文文件创建失败都会向用户报告错误，不会用空列表继续执行。
        内部通过 ``_build_txt_import_result`` 构建结构化 ``ImportResult``，
        仅在成功时调用 ``_commit_session`` 原子替换会话状态。
        """
        try:
            if file_path is None:
                file_path = filedialog.askopenfilename(
                    title="选择要翻译的文件",
                    filetypes=[
                        ("所有文件", "*.*"),
                        ("文本文件", "*.txt"),
                        ("EPUB电子书", "*.epub"),
                    ],
                )

            if file_path:
                # 记录原文路径
                src_path = Path(file_path)

                # 分支处理：EPUB走映射导入流程；文本文件走原有流程
                if src_path.suffix.lower() == ".epub":
                    # 创建解析提示弹窗并在新线程中执行解析
                    self._import_epub_with_progress(src_path)
                    return

                # P0-6：构建结构化 ImportResult，失败时旧 session 不变
                result = self._build_txt_import_result(src_path)
                if not result.succeeded:
                    messagebox.showerror("导入错误", result.user_message)
                    return

                # P0-3：原子提交新 session（仅成功路径）
                self._commit_session(result.session)
                tgt_path = result.session.target_path
                self.status_updater(f"已导入文件: {src_path.name}（译文文件：{tgt_path.name}）")

        except Exception as e:
            messagebox.showerror("导入错误", f"文件导入失败: {str(e)}")

    def _build_txt_import_result(self, src_path: Path) -> ImportResult:
        """构建文本文件导入结果（无 UI 副作用，可单测）。

        P0-6：明确区分源读取失败、目标读取失败、目标不可写三类失败。
        任何必要输入失败都不提交新 session。
        """
        # P0-6：源文件读取失败
        try:
            content = self.file_handler.read_file(str(src_path))
        except Exception as exc:
            return ImportResult.failure(
                ImportFailure.SOURCE_READ_FAILED,
                str(exc),
                failed_path=str(src_path),
            )
        source_lines = content.splitlines()

        # 派生译文路径：同目录、同名加"_译文.txt"
        tgt_path = src_path.with_name(f"{src_path.stem}_译文.txt")

        target_lines: List[str] = []
        if tgt_path.exists():
            try:
                tgt_content = self.file_handler.read_file(str(tgt_path))
                target_lines = tgt_content.splitlines()
            except Exception as exc:
                # P0-6：译文读取失败不继续用空列表，避免覆盖已有译文
                return ImportResult.failure(
                    ImportFailure.TARGET_READ_FAILED,
                    f"{exc}\n\n请检查文件权限或编码，或删除该文件后重新导入。",
                    failed_path=str(tgt_path),
                )
        else:
            # 不存在则创建空文件，确保后续实时保存有目标
            try:
                self.file_handler.write_file(str(tgt_path), "")
            except Exception as exc:
                # P0-6：创建失败必须报告，不继续执行
                return ImportResult.failure(
                    ImportFailure.TARGET_NOT_WRITABLE,
                    f"{exc}\n\n请检查目录权限或磁盘空间。",
                    failed_path=str(tgt_path),
                )

        # 全部成功，构建新 session
        session = DocumentSession.create(
            SessionKind.TXT,
            source_path=src_path,
            target_path=tgt_path,
            source_lines=source_lines,
            target_lines=target_lines,
        )
        return ImportResult.success(session)

    def _build_epub_import_result(self, src_path: Path) -> ImportResult:
        """构建 EPUB 导入结果（在工作线程内调用，无 UI 副作用）。

        P0-3/P0-6：使用局部变量，失败时旧 session 完全不变。
        成功时返回带新 ``DocumentSession`` 的 ``ImportResult``，
        由主线程 ``_commit_session`` 原子替换。
        """
        try:
            # 生成/刷新 mapping
            mapping_info = self.epub_processor.import_epub(str(src_path))
            new_mapping_dir = Path(mapping_info["mapping_dir"])

            # 加载原文和译文列表
            originals, translations = self.epub_processor.load_content_mapping(str(new_mapping_dir))

            # 派生译文路径（EPUB 模式下保存到 mapping 目录）
            tgt_path = new_mapping_dir / f"{src_path.stem}_译文.txt"

            # P0-6：目标目录创建失败必须报告
            try:
                tgt_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                return ImportResult.failure(
                    ImportFailure.TARGET_NOT_WRITABLE,
                    f"无法创建映射目录：{exc}",
                    failed_path=str(tgt_path.parent),
                )

            # P0-6：目标文件创建失败必须报告
            if not tgt_path.exists():
                try:
                    with open(tgt_path, "w", encoding="utf-8") as f:
                        f.write("")
                except Exception as exc:
                    return ImportResult.failure(
                        ImportFailure.TARGET_NOT_WRITABLE,
                        f"无法创建译文文件：{exc}",
                        failed_path=str(tgt_path),
                    )

            session = DocumentSession.create(
                SessionKind.EPUB,
                source_path=src_path,
                target_path=tgt_path,
                mapping_dir=new_mapping_dir,
                source_lines=originals,
                target_lines=translations,
            )
            return ImportResult.success(session)
        except Exception as exc:
            return ImportResult.failure(
                ImportFailure.EPUB_PARSE_FAILED,
                str(exc),
                failed_path=str(src_path),
            )

    def _commit_session(self, session: DocumentSession) -> None:
        """P0-3：原子提交新会话状态（必须在 Tk 主线程调用）。

        替换 ``self._session`` 并触发 ``table_loader`` 渲染。
        旧 session 的所有路径状态被整体丢弃，不会出现
        "新 source + 旧 target/mapping" 的混合状态。
        """
        self._session = session
        self.current_mapping_keys = []
        self.table_loader(
            session.document.source_lines(),
            session.document.target_lines(),
        )

    def _import_epub_with_progress(self, src_path):
        """在弹窗中执行EPUB解析，解析期间禁止操作界面

        P1-3：解析窗口注册 ``WM_DELETE_WINDOW`` 阻止用户在解析期间关闭，
        避免主界面控件永久禁用。解析完成后由 ``_finish_epub_import``
        统一关闭弹窗并幂等地恢复主界面控件。
        """
        # 创建模态对话框
        progress_dialog = tk.Toplevel(self.root)
        progress_dialog.title("正在解析EPUB")
        progress_dialog.transient(self.root)
        progress_dialog.grab_set()  # 模态窗口
        progress_dialog.resizable(False, False)

        # P1-3：禁止用户在解析期间关闭弹窗，避免主界面控件永久禁用
        def _forbid_close():
            messagebox.showinfo(
                "请稍候",
                "EPUB 正在解析中，请等待解析完成后关闭。",
                parent=progress_dialog,
            )

        progress_dialog.protocol("WM_DELETE_WINDOW", _forbid_close)

        # 计算居中位置
        window_width = 400
        window_height = 150
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        progress_dialog.geometry(f"{window_width}x{window_height}+{x}+{y}")

        # 创建界面元素
        container = ttk.Frame(progress_dialog, padding=20)
        container.pack(fill=tk.BOTH, expand=True)

        # 图标和提示文字
        icon_label = ttk.Label(container, text="📖", font=("微软雅黑", 32))
        icon_label.pack(pady=(0, 10))

        message_label = ttk.Label(
            container, text="正在解析EPUB文件，请稍候...", font=("微软雅黑", 11)
        )
        message_label.pack(pady=(0, 15))

        # 进度条
        progress_bar = ttk.Progressbar(container, mode="indeterminate", length=300)
        progress_bar.pack(pady=(0, 10))
        progress_bar.start(10)  # 启动动画

        # 状态标签
        status_label = ttk.Label(
            container, text="正在提取文档结构...", font=("微软雅黑", 9), foreground="#666"
        )
        status_label.pack()

        # 禁用主窗口按钮
        self._disable_ui_controls()

        # 存储结果和错误信息
        result_container = {"success": False, "error": None, "result": None}

        def epub_import_worker():
            """完全重构：EPUB解析工作线程，简化为纯行号对齐

            P0-3/P0-6：通过 ``_build_epub_import_result`` 构建结构化结果，
            只有成功时才在主线程 ``_commit_session`` 原子替换会话状态。
            失败时旧 session 完全不变，且错误信息会通过
            ``ImportResult.user_message`` 向用户报告路径、原因。
            """
            try:
                # 更新状态
                self._safe_after(
                    progress_dialog, lambda: status_label.config(text="正在解析EPUB结构...")
                )

                result = self._build_epub_import_result(src_path)

                self._safe_after(
                    progress_dialog, lambda: status_label.config(text="正在加载内容映射...")
                )

                if result.succeeded:
                    self._safe_after(
                        progress_dialog, lambda: status_label.config(text="正在填充原文区域...")
                    )

                    # 在主线程中原子提交 session 并更新状态
                    def update_ui():
                        # P0-3：原子提交新 session（替换 self._session + table_loader）
                        self._commit_session(result.session)
                        new_mapping_dir = result.session.mapping_dir

                        # 显示映射目录名称
                        try:
                            mapping_display = new_mapping_dir.relative_to(src_path.parent)
                        except ValueError:
                            try:
                                mapping_display = new_mapping_dir.relative_to(Path.cwd())
                            except ValueError:
                                mapping_display = new_mapping_dir

                        self.status_updater(
                            f"已导入EPUB: {src_path.name}（映射目录：{mapping_display}）"
                        )

                    self._safe_after(progress_dialog, update_ui)
                    result_container["success"] = True
                else:
                    result_container["error"] = result.user_message
            except Exception as e:
                result_container["error"] = str(e)
            finally:
                # 关闭弹窗并恢复UI
                self._safe_after(
                    progress_dialog,
                    lambda: self._finish_epub_import(
                        progress_dialog, result_container, src_path.name
                    ),
                )

        # 在新线程中执行解析
        import_thread = threading.Thread(target=epub_import_worker, daemon=True)
        import_thread.start()

    def _disable_ui_controls(self):
        """禁用主界面控件"""
        try:
            # 禁用工具栏按钮（通过遍历所有子控件）
            for widget in self.root.winfo_children():
                self._disable_widget_recursive(widget)
        except Exception:
            pass

    def _enable_ui_controls(self):
        """启用主界面控件"""
        try:
            for widget in self.root.winfo_children():
                self._enable_widget_recursive(widget)
        except Exception:
            pass

    def _disable_widget_recursive(self, widget):
        """递归禁用控件（不禁用Text控件，避免影响数据填充）"""
        try:
            if isinstance(widget, (ttk.Button, tk.Button, ttk.Combobox)):
                widget.config(state=tk.DISABLED)
            # 注意：不禁用Text控件，以便后台线程能填充数据
            for child in widget.winfo_children():
                self._disable_widget_recursive(child)
        except Exception:
            pass

    def _enable_widget_recursive(self, widget):
        """递归启用控件"""
        try:
            if isinstance(widget, (ttk.Button, tk.Button)):
                # 根据翻译状态决定按钮是否启用
                widget.config(state=tk.NORMAL)
            elif isinstance(widget, tk.Text):
                widget.config(state=tk.NORMAL)
            elif isinstance(widget, ttk.Combobox):
                widget.config(state="readonly")
            for child in widget.winfo_children():
                self._enable_widget_recursive(child)
        except Exception:
            pass

    def _finish_epub_import(self, dialog, result, filename):
        """完成EPUB导入，关闭弹窗并显示结果"""
        try:
            dialog.destroy()
        except Exception:
            pass

        # 恢复UI控件
        self._enable_ui_controls()

        # 显示结果（仅在失败时显示错误）
        if not result["success"]:
            messagebox.showerror("导入错误", f"EPUB映射导入失败：\n\n{result['error']}")
            return

        # EPUB导入成功后，检查是否有图片且启用了图片文字识别
        self._check_epub_images_for_text()

    def _check_epub_images_for_text(self):
        """EPUB 导入后询问是否使用默认图片翻译模块处理。

        接入 Manga 模块后：不再检查火山 Key，直接走默认 Manga 模块。
        """
        if not self.current_mapping_dir:
            return

        # 检查images.json是否有图片
        images_file = self.current_mapping_dir / "images.json"
        if not images_file.exists():
            return

        # PERF-004：迁移旧格式 Base64 图片到二进制资源文件
        from ..infrastructure.image_asset_store import migrate_legacy_images

        try:
            migrate_legacy_images(self.current_mapping_dir)
        except Exception:
            # 迁移失败不阻断流程，旧格式仍可回退使用
            pass

        import json

        try:
            images_data = json.loads(images_file.read_text(encoding="utf-8"))
            if not images_data.get("image_mappings"):
                return
        except Exception:
            return

        img_count = len(images_data["image_mappings"])

        # 弹出确认对话框：走默认 Manga 模块，不检查火山 Key
        do_translate = messagebox.askyesno(
            "图片翻译",
            f"检测到 EPUB 包含 {img_count} 张图片，"
            "是否使用默认图片翻译模块处理？\n\n"
            "（可稍后通过「项目 - 图片翻译」手动触发）",
        )

        if do_translate:
            self.image_translation_starter()

    def import_clipboard(self):
        """导入剪贴板内容

        P0-3/P0-6：剪贴板导入通过创建新的 CLIPBOARD session 原子替换状态，
        避免新项目译文写入旧文件或旧 EPUB 映射。
        """
        try:
            clipboard_content = self.root.clipboard_get()
            if clipboard_content.strip():
                source_lines = clipboard_content.splitlines()
                # P0-3：原子提交新的剪贴板 session（清除所有旧路径）
                session = DocumentSession.create(
                    SessionKind.CLIPBOARD,
                    source_lines=source_lines,
                    target_lines=[],
                )
                self._commit_session(session)
                self.status_updater("已导入剪贴板内容")
            else:
                messagebox.showwarning("导入警告", "剪贴板内容为空")

        except tk.TclError:
            messagebox.showerror("导入错误", "无法获取剪贴板内容")
