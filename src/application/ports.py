#!/usr/bin/env python3
"""
Application 层端口（协议）定义

端口表达业务需要，而不是包装第三方库全部 API。应用服务只依赖这些协议，
不依赖 httpx.Client、SiliconFlowAPI 或 tkinter。

阶段 3（API 生命周期）交付：
- TranslationProvider：翻译提供商协议，AI-B 的 TaskQueueService 只依赖此协议。
- UiScheduler：UI 调度器协议，Application 层不直接调用 root.after()。
- ProjectRepository：翻译项目仓库协议（UXF-004），支持重启恢复长任务。

UXF-002（稀疏行翻译）：TranslationProvider.translate_batch 已接受任意行序列，
稀疏行索引映射由 SparseLineTranslator 应用服务处理，所有调用方共享同一套逻辑。
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationRequest,
    ImageTranslationResult,
)
from ..domain.project import TranslationProject
from ..domain.translation import (
    TranslationOptions,
    TranslationProgress,
    TranslationResult,
)


@runtime_checkable
class TranslationProvider(Protocol):
    """翻译提供商协议

    现有 API 类（SiliconFlowAPI / DeepseekAPI）通过适配器满足此协议。
    应用服务（TaskQueueService / TranslationService）只依赖此协议，
    不依赖具体提供商实现，便于测试时注入替身。

    生命周期：
    - translate_batch：同步执行一批翻译，返回结构化结果。
    - cancel：取消进行中的翻译请求，幂等可多次调用。
    - close：释放 HTTP 客户端等资源，幂等可多次调用。

    UXF-002：translate_batch 接受任意行序列（不限于连续范围），
    调用方可传入稀疏行（如仅索引 3/7/12 的原文），结果按传入顺序返回。
    索引到全局位置的映射由 SparseLineTranslator 应用服务处理。
    """

    def translate_batch(
        self,
        lines: Sequence[str],
        options: TranslationOptions,
        on_progress: Callable[[TranslationProgress], None] | None = None,
    ) -> TranslationResult:
        """翻译一批原文行

        Args:
            lines: 原文行序列（非空字符串，可为稀疏选取的行）
            options: 翻译选项（目标语言、模型、批次大小等）
            on_progress: 进度回调（可选），在每批完成时调用

        Returns:
            TranslationResult：包含状态、译文、失败索引和错误信息。
            failed_indices 是相对于传入 lines 的偏移（0-based），
            调用方负责映射回全局索引。

        Raises:
            TranslationCancelled: 用户取消时抛出（不返回 CANCELLED 状态结果）
            TranslationRequestError: 请求失败时抛出
        """
        ...

    def cancel(self) -> None:
        """取消所有进行中的翻译请求

        幂等：多次调用安全。取消后客户端应被关闭并置空，
        下次 translate_batch 时自动重建。
        """
        ...

    def close(self) -> None:
        """释放资源（HTTP 客户端、心跳线程、线程池等）

        幂等：多次调用安全。关闭后不应再调用 translate_batch。
        """
        ...


@runtime_checkable
class UiScheduler(Protocol):
    """UI 调度器协议

    Application 层不应直接调用 root.after()。Presentation 层提供此实现，
    把回调调度到 Tk 主线程执行。

    Tk 实现只需封装 root.after(0, callback)。
    """

    def submit(self, callback: Callable[[], None]) -> None:
        """把回调提交到 UI 线程执行

        Args:
            callback: 无参回调，在 UI 线程中执行
        """
        ...


@runtime_checkable
class EpubProjectRepository(Protocol):
    """EPUB 项目仓库协议（阶段 4 引入）

    读写 EPUB 项目的映射格式，原子保存，格式版本管理。
    """

    def create_or_load(self, source_path: Path) -> object:
        """创建或加载 EPUB 项目

        Args:
            source_path: 源 EPUB 文件路径

        Returns:
            EpubProject 实例（阶段 4 定义）
        """
        ...

    def save_segments(self, project: object) -> None:
        """保存段落映射"""
        ...

    def save_image_map(self, project_id: str, image_map: dict[str, str]) -> None:
        """保存图片映射"""
        ...


@runtime_checkable
class ProjectRepository(Protocol):
    """翻译项目仓库协议（UXF-004）

    持久化 TranslationProject 状态，支持应用重启后恢复长任务。
    实现见 infrastructure/project_repository.py。

    AI-B 的 TaskQueueService 和 TranslationService 依赖此协议，
    测试时可注入内存替身。
    """

    def create(
        self,
        *,
        source_path: str,
        source_fingerprint: str,
        file_type: str,
        mapping_dir: str,
        original_lines: Sequence[str],
        model_snapshot: dict | None = None,
    ) -> TranslationProject:
        """创建新翻译项目（同 ID 已存在则返回已有项目）

        Args:
            source_path: 源文件路径
            source_fingerprint: 源文件内容指纹（SHA-256）
            file_type: 文件类型（"txt" / "epub" / "clipboard"）
            mapping_dir: EPUB 映射目录（TXT 可为空）
            original_lines: 原文行列表
            model_snapshot: 模型与配置快照

        Returns:
            TranslationProject 实例（未保存状态）
        """
        ...

    def load(self, project_id: str) -> TranslationProject | None:
        """按项目 ID 加载项目，不存在返回 None"""
        ...

    def save(self, project: TranslationProject) -> None:
        """原子保存项目状态（UXF-003：失败抛异常，不静默吞掉）

        保存失败时抛出异常，由上层捕获并提示"重试保存"。
        """
        ...

    def create_checkpoint(self, project: TranslationProject, label: str = "") -> str:
        """创建检查点（UXF-001：覆盖前创建，允许撤销）

        Returns:
            检查点文件名
        """
        ...

    def list_recent(self, limit: int = 20) -> list[dict]:
        """列出最近打开的项目摘要，按最后打开时间倒序"""
        ...

    def delete(self, project_id: str) -> bool:
        """删除项目及其检查点"""
        ...


@runtime_checkable
class ImageTranslationProvider(Protocol):
    """图片翻译 Provider 协议

    现有 Manga 引擎和火山图生图均通过适配器满足此协议。应用服务
    ImageTranslationService 只依赖此协议，不依赖具体引擎或 tkinter。

    约束：
    - Provider 不直接访问 Tk 控件，也不显示 messagebox。
    - Provider 不自行决定切换另一个 Provider。
    - Provider 返回结构化部分成功结果，不用空字典混淆「无需翻译」和「全部失败」。
    - cancel() 和 close() 必须幂等，可安全多次调用。
    - 错误信息不得包含 API Key、完整 Base64、请求头或鉴权响应原文。
    """

    provider_id: str

    def validate(self, request: ImageTranslationRequest) -> list[str]:
        """执行前校验请求

        Returns:
            错误消息列表；非空表示请求不满足前置条件（如缺模型、缺 Key、
            未知语言）。Service 据此提示用户，不调用 translate。
        """
        ...

    def translate(
        self,
        request: ImageTranslationRequest,
        on_progress: Callable[[ImageTranslationProgress], None] | None = None,
    ) -> ImageTranslationResult:
        """执行图片翻译

        Args:
            request: 图片翻译请求
            on_progress: 进度回调（可选）

        Returns:
            结构化结果，包含成功映射、跳过和失败列表。
            取消时抛出 ImageTranslationCancelled。
        """
        ...

    def cancel(self) -> None:
        """取消进行中的翻译，幂等"""
        ...

    def close(self) -> None:
        """释放资源（模型、event loop、HTTP 客户端），幂等"""
        ...


@runtime_checkable
class ImageManifestRepository(Protocol):
    """图片翻译 manifest 仓储协议（P1-1 / P2-2）

    Application 层只依赖此协议，不依赖具体的 ManifestRepository 实现。
    bootstrap.py 注入工厂，每次请求按 mapping_dir 创建实例。

    约束：
    - save / save_empty 失败时抛出异常，不静默吞掉（P1-1）。
    - 原子写入保证旧文件在写入失败时保持完整。
    - load 兼容 v1/v2 格式。
    """

    def save(
        self,
        result: ImageTranslationResult,
        *,
        source_fingerprint: str = "",
        config_fingerprint: str = "",
        run_at: str = "",
    ) -> None:
        """以 v2 格式原子写入 manifest，失败时抛出异常"""
        ...

    def save_empty(self, *, run_at: str = "") -> None:
        """写入空结果 manifest（取消或全部失败前的清理）"""
        ...

    def load(self) -> object | None:
        """读取 manifest，不存在返回 None"""
        ...


@runtime_checkable
class ImageProviderRegistry(Protocol):
    """图片翻译 Provider 注册表协议（P2-2）

    Application 层只依赖此协议，不依赖全局 get_registry() 单例。
    bootstrap.py 注入具体注册表，多个窗口或任务之间不共享状态。

    约束：
    - 按 provider_id 取得 Provider，不实现自动 fallback。
    - Manga Provider 在禁用开关关闭时返回 None，调用方展示错误。
    """

    def register(self, provider: object) -> None:
        """注册一个 Provider"""
        ...

    def get(self, provider_id: object) -> object | None:
        """按 provider_id 取得 Provider，不存在返回 None"""
        ...

    def is_registered(self, provider_id: object) -> bool:
        """判断 provider_id 是否已注册且可用"""
        ...


@runtime_checkable
class SecretStore(Protocol):
    """密钥存储协议（P1-2）

    Application 层只依赖此协议，不依赖 keyring 或环境变量具体实现。
    bootstrap.py 注入具体实现（KeyringSecretStore 或测试替身）。

    约束：
    - store 返回 StorageStatus，区分 PERSISTED / SESSION_ONLY / FAILED。
    - 不在日志或异常中写入密钥明文。
    - 空密钥视为删除。
    """

    def store(self, identifier: str, key: str) -> object:
        """存储密钥，返回 StorageStatus

        Args:
            identifier: 密钥标识符，如 "provider:siliconflow"
            key: 密钥明文；空字符串视为删除

        Returns:
            StorageStatus.PERSISTED / SESSION_ONLY / FAILED
        """
        ...

    def retrieve(self, identifier: str) -> str:
        """读取密钥，不存在返回空字符串"""
        ...

    def delete(self, identifier: str) -> bool:
        """删除密钥，幂等"""
        ...
