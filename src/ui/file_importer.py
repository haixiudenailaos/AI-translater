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
from ..application.translation_document import TranslationDocument
from ..core.epub_processor import EpubImportCancelled
from ..domain.edition import EditionCapabilities, detect_edition_capabilities
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
        confirm_replace_session: Callable[[], str] | None = None,
        confirm_stop_active_translation: Callable[[], bool] | None = None,
        document: TranslationDocument | None = None,
        edition_capabilities: EditionCapabilities | None = None,
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
            confirm_replace_session: P0-3：替换当前会话前由主窗口提供的
                Save/Discard/Cancel 守卫回调，返回 "save"/"discard"/"cancel"/"proceed"。
                ``None`` 时跳过守卫（保留旧行为，仅用于早期测试替身）。
            confirm_stop_active_translation: P0-2：当前翻译运行时的会话替换
                守卫。确认后负责停止旧请求并清理运行状态，返回是否可以替换。
            document: 可选的应用级文档模型。传入后，导入会话与主窗口共享
                同一个 ``TranslationDocument``，避免模型和会话各自维护副本。
            edition_capabilities: Text/Full 版本能力契约。Text 版本不提示 Manga 处理。
        """
        self.root = root
        self.config_manager = config_manager
        self.file_handler = file_handler
        self.epub_processor = epub_processor
        self.table_loader = table_loader
        self.status_updater = status_updater
        self.image_translation_starter = image_translation_starter
        self._confirm_replace_session = confirm_replace_session
        self._confirm_stop_active_translation = confirm_stop_active_translation
        self._document = document
        self.edition_capabilities = (
            edition_capabilities
            if edition_capabilities is not None
            else detect_edition_capabilities()
        )

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
        self._txt_import_busy = False
        self._txt_import_generation = 0
        self._disabled_control_states: list[tuple[object, str]] = []

    def close(self) -> None:
        """P1-1：关闭 UI 回调事件泵，释放 ``after`` 调度。

        应在 ``MainWindow.on_closing`` 中调用，确保退出时取消 pump 调度。
        幂等。
        """
        self._txt_import_generation += 1
        self._txt_import_busy = False
        pump = self._ui_pump
        self._ui_pump = None
        if pump is not None:
            pump.close()

    def replace_document(self, document: TranslationDocument) -> None:
        """Attach a shared document model (primarily for composition-root setup)."""
        self._document = document

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

    @property
    def is_dirty(self) -> bool:
        """P0-3：当前会话是否有未保存更改。

        委派给 MainWindow 提供的 ``is_dirty_callback``（构造后绑定），
        默认返回 ``False`` 以兼容早期测试替身。
        """
        callback = getattr(self, "is_dirty_callback", None)
        if callback is None:
            return False
        return bool(callback())

    def update_session_target_path(self, target_path: Path) -> None:
        """P0-3：写回 Save As 产生的新目标路径。

        在 Tk 主线程调用。保留当前会话的 session_id、kind、source、mapping、
        document、dirty 状态，仅替换 target_path。这样后续 Ctrl+S 会直接
        写入新目标，不再每次弹出 Save As。
        """
        old = self._session
        if old.target_path == target_path:
            return
        # 使用 dataclasses.replace 保留所有其他字段；DocumentSession 是 slots dataclass。
        from dataclasses import replace

        self._session = replace(old, target_path=Path(target_path))

    def mark_clean(self) -> None:
        """Mark the current session clean after a successful explicit save."""
        self._session.mark_clean()

    def _guard_replace_session(self) -> bool:
        """P0-3：替换会话前请求 Save/Discard/Cancel 守卫。

        返回 ``True`` 表示可以继续替换，``False`` 表示用户取消。
        当 current session 没有未保存更改时直接放行。
        """
        if self.is_dirty:
            guard = self._confirm_replace_session
            if guard is not None:
                choice = guard()
                if choice == "cancel":
                    return False
                if choice == "save" and self.is_dirty:
                    # 守卫回调应在返回前完成保存；保留此检查保护旧调用方。
                    self.status_updater("已取消导入（保存未完成）")
                    return False

        # 先完成可取消的保存决策，避免用户取消导入时翻译却已被停止。
        stop_guard = self._confirm_stop_active_translation
        return stop_guard is None or stop_guard()

    def _safe_after(self, target_window, func):
        """P1-1：通过 UI 回调邮箱提交回调，不在工作线程调用 Tk API。

        ``target_window`` 参数保留以兼容旧调用方，但不再从中调用
        ``winfo_exists()``（跨线程 Tk 调用）。回调执行时若目标窗口已销毁，
        pump 的异常处理器会捕获并记录 ``TclError``，不会中断后续回调。
        """
        self._ui_mailbox.submit(func)

    def import_file(self, file_path=None):
        """导入文件并自动检测/创建对应译文文件，加载译文并定位到末尾

        P0-3：当前会话有未保存更改时，先弹 Save/Discard/Cancel 守卫。
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
                # P0-3：替换会话前请求 Save/Discard/Cancel 守卫。
                # 用户取消时不进入文件读取，避免未保存数据被丢弃。
                if not self._guard_replace_session():
                    return
                # 记录原文路径
                src_path = Path(file_path)

                # 分支处理：EPUB走映射导入流程；文本文件走原有流程
                if src_path.suffix.lower() == ".epub":
                    # 创建解析提示弹窗并在新线程中执行解析
                    self._import_epub_with_progress(src_path)
                    return

                self._import_txt_in_background(src_path)

        except Exception as e:
            messagebox.showerror("导入错误", f"文件导入失败: {str(e)}")

    def _import_txt_in_background(self, src_path: Path) -> None:
        """Read and validate a TXT session off the Tk thread.

        The worker only performs disk I/O and constructs ``ImportResult``.
        The mailbox callback commits the session on the Tk thread, preserving
        the existing all-or-nothing session replacement contract.
        """
        if self._txt_import_busy:
            messagebox.showinfo("导入进行中", "当前文本文件仍在读取，请稍候。")
            return

        self._txt_import_busy = True
        self._txt_import_generation += 1
        generation = self._txt_import_generation
        self._disable_ui_controls()
        self.status_updater(f"正在读取文本文件: {src_path.name}")

        def worker() -> None:
            result = self._build_txt_import_result(src_path)
            self._ui_mailbox.submit(
                lambda: self._finish_txt_import(generation, src_path, result)
            )

        threading.Thread(
            target=worker,
            name="txt-import",
            daemon=True,
        ).start()

    def _finish_txt_import(
        self,
        generation: int,
        src_path: Path,
        result: ImportResult,
    ) -> None:
        """Commit a completed TXT import on the Tk thread."""
        if generation != self._txt_import_generation:
            return
        self._txt_import_busy = False
        self._enable_ui_controls()
        if not result.succeeded or result.session is None:
            messagebox.showerror("导入错误", result.user_message)
            return

        self._commit_session(result.session)
        target_path = result.session.target_path
        self.status_updater(f"已导入文件: {src_path.name}（译文文件：{target_path.name}）")

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

    def _build_epub_import_result(
        self,
        src_path: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> ImportResult:
        """构建 EPUB 导入结果（在工作线程内调用，无 UI 副作用）。

        P0-3/P0-6：使用局部变量，失败时旧 session 完全不变。
        成功时返回带新 ``DocumentSession`` 的 ``ImportResult``，
        由主线程 ``_commit_session`` 原子替换。
        """
        try:
            if cancel_requested is not None and cancel_requested():
                return ImportResult.cancelled()
            # 生成/刷新 mapping
            mapping_info = self.epub_processor.import_epub(
                str(src_path),
                cancel_requested=cancel_requested,
                progress_callback=progress_callback,
            )
            if cancel_requested is not None and cancel_requested():
                return ImportResult.cancelled()
            new_mapping_dir = Path(mapping_info["mapping_dir"])

            # 加载原文和译文列表
            originals, translations = self.epub_processor.load_content_mapping(str(new_mapping_dir))
            if cancel_requested is not None and cancel_requested():
                return ImportResult.cancelled()

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
        except EpubImportCancelled:
            return ImportResult.cancelled()
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
        # P0-3：导入结果先构建了独立模型；生产主窗口传入应用级模型时，
        # 将内容复制到该模型后把会话指向同一实例。这样 controller、autosave、
        # FileImporter 和 UI 共享唯一的 TranslationDocument。
        if self._document is not None and session.document is not self._document:
            self._document.replace(
                session.document.source_lines(),
                session.document.target_lines(),
            )
            from dataclasses import replace

            session = replace(session, document=self._document)

        self._session = session
        self.current_mapping_keys = []
        self.table_loader(
            session.document.source_lines(),
            session.document.target_lines(),
        )

    def _import_epub_with_progress(self, src_path):
        """在弹窗中执行EPUB解析，解析期间禁止操作界面

        P1-9：模态解析框改为可取消——``WM_DELETE_WINDOW`` 与取消按钮均
        设置 ``cancel_event``，worker 在解析阶段间隙检查并退出；``finally``
        无论如何都会释放 grab 并恢复主界面控件，避免解析卡死时 UI 永久禁用。
        """
        # 创建模态对话框
        progress_dialog = tk.Toplevel(self.root)
        progress_dialog.title("正在解析EPUB")
        progress_dialog.transient(self.root)
        progress_dialog.grab_set()  # 模态窗口
        progress_dialog.resizable(False, False)

        # P1-9：取消令牌——worker 在阶段间隙检查，关闭窗口或点取消按钮即设置
        cancel_event = threading.Event()

        # P1-9：WM_DELETE_WINDOW 不再硬禁止，改为确认后请求取消
        def _on_close_request():
            if not cancel_event.is_set():
                cancel_event.set()
                try:
                    status_label.config(text="正在取消，请稍候...")
                    cancel_btn.config(state=tk.DISABLED, text="正在取消...")
                except Exception:
                    pass

        progress_dialog.protocol("WM_DELETE_WINDOW", _on_close_request)

        # 计算居中位置
        window_width = 400
        window_height = 180
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

        # P1-9：取消按钮——用户可随时请求取消
        cancel_btn = ttk.Button(container, text="取消", command=_on_close_request)
        cancel_btn.pack(pady=(10, 0))

        # Preserve the real state of main-window controls. The progress dialog
        # remains interactive so its cancel command is always available.
        self._disable_ui_controls(exclude_widget=progress_dialog)

        def epub_import_worker():
            """完全重构：EPUB解析工作线程，简化为纯行号对齐

            P0-3/P0-6：通过 ``_build_epub_import_result`` 构建结构化结果，
            只有成功时才在主线程 ``_commit_session`` 原子替换会话状态。
            失败时旧 session 完全不变，且错误信息会通过
            ``ImportResult.user_message`` 向用户报告路径、原因。

            P1-9：在解析阶段间隙检查 ``cancel_event``，用户取消时不再提交
            session 并走 finally 释放 grab/恢复控件。
            """
            try:
                result = self._build_epub_import_result(
                    src_path,
                    cancel_requested=cancel_event.is_set,
                    progress_callback=lambda message: self._safe_after(
                        progress_dialog, lambda: status_label.config(text=message)
                    ),
                )
            except Exception as e:
                result = ImportResult.failure(ImportFailure.EPUB_PARSE_FAILED, str(e))
            finally:
                # P1-9：无论成功、失败还是取消，都在主线程释放 grab + 恢复控件
                self._safe_after(
                    progress_dialog,
                    lambda: self._finish_epub_import(
                        progress_dialog,
                        result,
                        src_path,
                        cancelled=cancel_event.is_set(),
                    ),
                )

        # 在新线程中执行解析
        import_thread = threading.Thread(target=epub_import_worker, daemon=True)
        import_thread.start()

    def _disable_ui_controls(self, *, exclude_widget=None):
        """Temporarily disable controls while preserving their exact prior state."""
        self._disabled_control_states = []
        try:
            for widget in self.root.winfo_children():
                self._disable_widget_recursive(widget, exclude_widget=exclude_widget)
        except Exception:
            self._disabled_control_states = []

    def _enable_ui_controls(self):
        """Restore the state captured by ``_disable_ui_controls``."""
        states, self._disabled_control_states = self._disabled_control_states, []
        for widget, state in states:
            try:
                widget.config(state=state)
            except Exception:
                continue

    def _disable_widget_recursive(self, widget, *, exclude_widget=None):
        """Capture and disable command widgets, excluding the progress dialog tree."""
        if widget is exclude_widget:
            return
        try:
            if isinstance(widget, (ttk.Button, tk.Button, ttk.Combobox)):
                self._disabled_control_states.append((widget, str(widget.cget("state"))))
                widget.config(state=tk.DISABLED)
            for child in widget.winfo_children():
                self._disable_widget_recursive(child, exclude_widget=exclude_widget)
        except Exception:
            pass

    def _finish_epub_import(self, dialog, result: ImportResult, src_path: Path, *, cancelled: bool):
        """Finish an EPUB import on the Tk thread after its final safe point."""
        try:
            dialog.destroy()
        except Exception:
            pass

        # 恢复UI控件
        self._enable_ui_controls()

        if cancelled or result.cancelled_by_user:
            self.status_updater(f"已取消导入 EPUB：{src_path.name}")
            return

        if not result.succeeded or result.session is None:
            messagebox.showerror("导入错误", f"EPUB映射导入失败：\n\n{result.user_message}")
            return

        self._commit_session(result.session)
        new_mapping_dir = result.session.mapping_dir
        try:
            mapping_display = new_mapping_dir.relative_to(src_path.parent)
        except ValueError:
            try:
                mapping_display = new_mapping_dir.relative_to(Path.cwd())
            except ValueError:
                mapping_display = new_mapping_dir
        self.status_updater(f"已导入EPUB: {src_path.name}（映射目录：{mapping_display}）")

        # EPUB导入成功后，检查是否有图片且启用了图片文字识别
        self._check_epub_images_for_text()

    def _check_epub_images_for_text(self):
        """EPUB 导入后询问是否使用默认图片翻译模块处理。

        接入 Manga 模块后：不再检查火山 Key，直接走默认 Manga 模块。
        """
        capabilities = getattr(self, "edition_capabilities", None)
        if capabilities is None:
            capabilities = detect_edition_capabilities()
        if not capabilities.manga_enabled:
            return

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

        P0-3：当前会话有未保存更改时，先弹 Save/Discard/Cancel 守卫。
        剪贴板导入通过创建新的 CLIPBOARD session 原子替换状态，
        避免新项目译文写入旧文件或旧 EPUB 映射。
        """
        try:
            # P0-3：替换会话前请求 Save/Discard/Cancel 守卫。
            if not self._guard_replace_session():
                return
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
