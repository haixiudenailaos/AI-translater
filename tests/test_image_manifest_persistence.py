#!/usr/bin/env python3
"""
P1-1 回归测试：图片翻译 manifest 持久化失败不得被误报为成功

覆盖验收标准：
- 任意 manifest 写入异常都不会被记录后静默忽略。
- Provider 执行成功但结果保存失败时，不得返回 SUCCEEDED。
- UI 成功状态与磁盘持久化状态一致（通过抛出 ImageManifestPersistenceError 实现）。
- 写入失败后旧文件保持完整，且用户可以重试（save_manifest 公开入口）。
- 取消路径 manifest 失败时仍抛出 ImageTranslationCancelled（取消语义优先）。
"""

from pathlib import Path

import pytest

from src.application.image_translation_service import ImageTranslationService
from src.domain.errors import (
    ImageManifestPersistenceError,
    ImageTranslationCancelled,
)
from src.domain.image_translation import (
    ImageTranslationProviderId,
    ImageTranslationRequest,
    ImageTranslationResult,
)
from src.domain.translation import OperationStatus
from src.infrastructure.image_translation.fake_provider import (
    FakeImageTranslationProvider,
)
from src.infrastructure.image_translation.manifest_repository import (
    ManifestRepository,
)
from src.infrastructure.image_translation.registry import (
    ImageTranslationProviderRegistry,
)

# ── 测试替身 ──────────────────────────────────────


class FailingManifestRepository:
    """模拟写入失败的 manifest 仓储。

    save / save_empty 抛出 OSError，模拟磁盘满 / 权限错误 / 原子替换失败。
    load 委托给真实 ManifestRepository，用于验证旧文件未被破坏。
    """

    def __init__(self, mapping_dir: Path):
        self.mapping_dir = Path(mapping_dir)
        self._real = ManifestRepository(self.mapping_dir)
        self.save_calls = 0
        self.save_empty_calls = 0

    def save(self, result, *, source_fingerprint="", config_fingerprint="", run_at=""):
        self.save_calls += 1
        raise OSError("模拟磁盘满：无法写入 manifest")

    def save_empty(self, *, run_at=""):
        self.save_empty_calls += 1
        raise OSError("模拟权限错误：无法写入空 manifest")

    def load(self):
        return self._real.load()


class CountingManifestRepository(FailingManifestRepository):
    """第一次 save 失败，第二次成功（模拟重试恢复）。"""

    def __init__(self, mapping_dir: Path):
        super().__init__(mapping_dir)
        self._fail_remaining = 1

    def save(self, result, *, source_fingerprint="", config_fingerprint="", run_at=""):
        self.save_calls += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise OSError("模拟首次写入失败")
        # 重试成功：委托真实实现
        self._real.save(
            result,
            source_fingerprint=source_fingerprint,
            config_fingerprint=config_fingerprint,
            run_at=run_at,
        )


# ── fixtures ──────────────────────────────────────


def _make_request(tmp_path: Path) -> ImageTranslationRequest:
    """创建有效请求（mapping_dir 含 images.json）。"""
    (tmp_path / "images.json").write_text("{}", encoding="utf-8")
    return ImageTranslationRequest(
        mapping_dir=tmp_path,
        target_language="中文",
        provider_id=ImageTranslationProviderId.MANGA,
    )


@pytest.fixture
def tmp_config_manager(tmp_path):
    """最小配置管理器替身。"""

    class _CM:
        def get_app_config(self):
            return {"image_translation": {}}

    return _CM()


# ── 测试用例 ──────────────────────────────────────


class TestManifestPersistenceFailure:
    """P1-1：manifest 写入失败不得被误报为成功"""

    def test_provider_success_but_manifest_failure_raises(self, tmp_path, tmp_config_manager):
        """Provider 成功但 manifest 保存失败时，抛出 ImageManifestPersistenceError，
        不返回 SUCCEEDED 结果。"""
        registry = ImageTranslationProviderRegistry()
        registry.register(
            FakeImageTranslationProvider(
                provider_id="manga",
                result_map={"a.jpg": "manga/a.png"},
            )
        )
        service = ImageTranslationService(
            tmp_config_manager,
            registry,
            manifest_repository_factory=FailingManifestRepository,
        )

        with pytest.raises(ImageManifestPersistenceError) as exc_info:
            service.translate(_make_request(tmp_path))

        # 异常携带 Provider 的成功结果，供 UI 展示并支持重试
        assert exc_info.value.partial_result is not None
        assert exc_info.value.partial_result.status == OperationStatus.SUCCEEDED
        assert exc_info.value.partial_result.result_map == {"a.jpg": "manga/a.png"}
        assert exc_info.value.mapping_dir == tmp_path

    def test_old_manifest_preserved_on_write_failure(self, tmp_path, tmp_config_manager):
        """写入失败时旧 manifest 保持完整（不被破坏、不被清空）。"""
        # 先写入旧 manifest
        repo = ManifestRepository(tmp_path)
        old_result = ImageTranslationResult(
            status=OperationStatus.SUCCEEDED,
            result_map={"old.jpg": "manga/old.png"},
            provider_id=ImageTranslationProviderId.MANGA,
        )
        repo.save(old_result, run_at="2026-01-01T00:00:00+08:00")

        registry = ImageTranslationProviderRegistry()
        registry.register(
            FakeImageTranslationProvider(
                provider_id="manga",
                result_map={"new.jpg": "manga/new.png"},
            )
        )
        service = ImageTranslationService(
            tmp_config_manager,
            registry,
            manifest_repository_factory=FailingManifestRepository,
        )

        with pytest.raises(ImageManifestPersistenceError):
            service.translate(_make_request(tmp_path))

        # 旧 manifest 应保持完整，未被新结果覆盖或破坏
        data = repo.load()
        assert data is not None
        assert data.result_map == {"old.jpg": "manga/old.png"}
        assert data.run_at == "2026-01-01T00:00:00+08:00"

    def test_retry_save_manifest_succeeds(self, tmp_path, tmp_config_manager):
        """持久化失败后，用户可通过 save_manifest 重试保存。"""
        registry = ImageTranslationProviderRegistry()
        registry.register(
            FakeImageTranslationProvider(
                provider_id="manga",
                result_map={"a.jpg": "manga/a.png"},
            )
        )

        # 使用缓存工厂，保证 translate 和 save_manifest 使用同一 repository 实例，
        # 使 CountingManifestRepository 的"首次失败、重试成功"语义生效。
        cached_repo = CountingManifestRepository(tmp_path)
        service = ImageTranslationService(
            tmp_config_manager,
            registry,
            manifest_repository_factory=lambda mapping_dir: cached_repo,
        )

        request = _make_request(tmp_path)
        # 首次翻译：manifest 写入失败
        with pytest.raises(ImageManifestPersistenceError) as exc_info:
            service.translate(request)
        partial_result = exc_info.value.partial_result
        assert partial_result.result_map == {"a.jpg": "manga/a.png"}

        # 重试保存：CountingManifestRepository 第二次 save 成功
        service.save_manifest(tmp_path, partial_result, request)
        # 验证 manifest 已正确写入
        data = ManifestRepository(tmp_path).load()
        assert data is not None
        assert data.result_map == {"a.jpg": "manga/a.png"}
        assert data.is_v2

    def test_cancelled_path_manifest_failure_still_raises_cancelled(
        self, tmp_path, tmp_config_manager
    ):
        """取消时 manifest 写入失败，仍抛出 ImageTranslationCancelled（取消语义优先）。

        P1-1 要求 manifest 异常不被吞掉，但取消语义优先于持久化语义：
        用户已主动取消，应收到取消反馈，而非持久化错误。
        manifest 失败仅记录日志。
        """
        registry = ImageTranslationProviderRegistry()
        registry.register(
            FakeImageTranslationProvider(
                provider_id="manga", raise_on_translate=ImageTranslationCancelled()
            )
        )
        service = ImageTranslationService(
            tmp_config_manager,
            registry,
            manifest_repository_factory=FailingManifestRepository,
        )

        with pytest.raises(ImageTranslationCancelled):
            service.translate(_make_request(tmp_path))

    def test_failed_path_manifest_failure_raises_persistence_error(
        self, tmp_path, tmp_config_manager
    ):
        """Provider 抛异常失败时，manifest 写入也失败，抛出持久化异常。"""
        registry = ImageTranslationProviderRegistry()
        registry.register(
            FakeImageTranslationProvider(
                provider_id="manga",
                raise_on_translate=RuntimeError("模型加载失败"),
            )
        )
        service = ImageTranslationService(
            tmp_config_manager,
            registry,
            manifest_repository_factory=FailingManifestRepository,
        )

        # 失败路径：先尝试写失败结果 manifest，再抛持久化异常
        with pytest.raises(ImageManifestPersistenceError):
            service.translate(_make_request(tmp_path))

    def test_injected_registry_protocol_no_global_state(self, tmp_path, tmp_config_manager):
        """P2-2：Service 使用注入的 registry，不依赖全局 get_registry()。

        两个 Service 实例使用不同 registry，互不影响。
        """
        registry1 = ImageTranslationProviderRegistry()
        registry1.register(FakeImageTranslationProvider(provider_id="manga", result_map={"a": "b"}))
        registry2 = ImageTranslationProviderRegistry()  # 空 registry

        service1 = ImageTranslationService(tmp_config_manager, registry1)
        service2 = ImageTranslationService(tmp_config_manager, registry2)

        # service1 能找到 manga provider
        assert service1.registry is registry1
        assert service1.registry.is_registered("manga")

        # service2 的 registry 为空，不共享 service1 的注册状态
        assert service2.registry is registry2
        assert not service2.registry.is_registered("manga")

    def test_save_manifest_failure_message_no_credential_leak(self, tmp_path, tmp_config_manager):
        """持久化异常消息不泄露凭据（含 api_key 的原始错误应被脱敏）。"""
        registry = ImageTranslationProviderRegistry()
        registry.register(
            FakeImageTranslationProvider(
                provider_id="manga",
                result_map={"a.jpg": "manga/a.png"},
            )
        )

        class CredentialLeakingRepository(FailingManifestRepository):
            def save(self, result, **kwargs):
                raise OSError("写入失败：api_key=sk-secret-12345")

        service = ImageTranslationService(
            tmp_config_manager,
            registry,
            manifest_repository_factory=CredentialLeakingRepository,
        )

        with pytest.raises(ImageManifestPersistenceError) as exc_info:
            service.translate(_make_request(tmp_path))

        # 异常消息本身不应包含凭据（ImageManifestPersistenceError 使用固定消息）
        msg = str(exc_info.value)
        assert "sk-secret-12345" not in msg
        assert "api_key" not in msg.lower()
