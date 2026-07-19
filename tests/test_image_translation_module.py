#!/usr/bin/env python3
"""
图片翻译模块集成测试

覆盖阶段 1 契约层和阶段 3 应用服务：
- 领域模型不可变性与状态语义
- 目标语言映射（含未知语言明确失败）
- manifest v1/v2 兼容、空结果覆盖、原子写入、指纹校验
- Provider 注册表与禁用开关
- Service 的成功、部分成功、失败和取消语义
- 配置迁移：默认 Provider 永远为 manga，AI 选择不被持久化
- 行为：Manga 失败/无文字/取消时 AI 调用次数为 0
"""

import json
from pathlib import Path

import pytest

from src.domain.errors import (
    ImageTranslationCancelled,
    ImageTranslationConfigError,
)
from src.domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationProviderId,
    ImageTranslationRequest,
    ImageTranslationResult,
)
from src.domain.translation import OperationStatus
from src.infrastructure.image_translation.fake_provider import (
    FakeImageTranslationProvider,
)
from src.infrastructure.image_translation.language_codes import (
    stage_label,
    to_manga_lang,
)
from src.infrastructure.image_translation.manifest_repository import (
    ManifestRepository,
)
from src.infrastructure.image_translation.registry import (
    ImageTranslationProviderRegistry,
)

# ── 领域模型 ──────────────────────────────────────


class TestDomainModels:
    def test_provider_id_values(self):
        assert ImageTranslationProviderId.MANGA.value == "manga"
        assert ImageTranslationProviderId.AI_VOLCENGINE.value == "ai_volcengine"

    def test_request_is_frozen(self):
        req = ImageTranslationRequest(
            mapping_dir=Path("/tmp"),
            target_language="中文",
            provider_id=ImageTranslationProviderId.MANGA,
        )
        with pytest.raises(Exception):
            req.target_language = "英文"  # type: ignore[misc]

    def test_progress_is_frozen(self):
        prog = ImageTranslationProgress(stage="检测", current=0, total=10)
        with pytest.raises(Exception):
            prog.current = 5  # type: ignore[misc]

    def test_result_structured_fields(self):
        """结果使用结构化字段区分成功、跳过和失败，不混淆语义"""
        result = ImageTranslationResult(
            status=OperationStatus.PARTIAL,
            result_map={"a.jpg": "translated_images/manga/a.png"},
            skipped_images=["b.jpg"],
            failed_images={"c.jpg": "OCR failed"},
        )
        assert result.succeeded_count == 1
        assert not result.is_empty_run

    def test_empty_run_detection(self):
        result = ImageTranslationResult(status=OperationStatus.CANCELLED)
        assert result.is_empty_run


# ── 语言映射 ──────────────────────────────────────


class TestLanguageMapping:
    @pytest.mark.parametrize(
        "project_lang,manga_code",
        [
            ("中文", "CHS"),
            ("简体中文", "CHS"),
            ("繁體中文", "CHT"),
            ("英文", "ENG"),
            ("日文", "JPN"),
            ("韩文", "KOR"),
            ("法文", "FRA"),
            ("德文", "DEU"),
            ("西班牙文", "ESP"),
            ("俄文", "RUS"),
        ],
    )
    def test_known_language_mapping(self, project_lang, manga_code):
        assert to_manga_lang(project_lang) == manga_code

    @pytest.mark.parametrize("unknown_lang", ["", "火星文", "Latin", None])
    def test_unknown_language_fails(self, unknown_lang):
        """未映射语言返回 None，执行前应报配置错误，不静默回退"""
        assert to_manga_lang(unknown_lang) is None

    def test_stage_label_mapping(self):
        assert stage_label("detection") == "检测文字"
        assert stage_label("ocr") == "识别文字"
        assert stage_label("translating") == "翻译文字"
        assert stage_label("mask-generation") == "生成去字区域"
        assert stage_label("inpainting") == "修复原图"
        assert stage_label("rendering") == "渲染译文"

    def test_stage_label_unknown_returns_original(self):
        assert stage_label("unknown_state") == "unknown_state"


# ── manifest 兼容 ──────────────────────────────────────


class TestManifestRepository:
    def _make_images_json(self, mapping_dir: Path) -> None:
        (mapping_dir / "images.json").write_text(
            json.dumps({"image_mappings": {"a.jpg": {"original_path": "a.jpg"}}}),
            encoding="utf-8",
        )

    def test_load_v1_bare_dict(self, tmp_path):
        """v1 裸字典：整个字典视为 result_map"""
        self._make_images_json(tmp_path)
        repo = ManifestRepository(tmp_path)
        (tmp_path / "image_translation_result.json").write_text(
            json.dumps({"a.jpg": "a_translated.png", "b.jpg": "b_translated.png"}),
            encoding="utf-8",
        )
        data = repo.load()
        assert data is not None
        assert data.schema_version == 1
        assert data.result_map["a.jpg"] == "a_translated.png"

    def test_load_v1_with_result_map_key(self, tmp_path):
        """v1 带 result_map 键"""
        self._make_images_json(tmp_path)
        repo = ManifestRepository(tmp_path)
        (tmp_path / "image_translation_result.json").write_text(
            json.dumps({"result_map": {"a.jpg": "a.png"}, "run_at": "2026-01-01"}),
            encoding="utf-8",
        )
        data = repo.load()
        assert data is not None
        assert data.result_map == {"a.jpg": "a.png"}
        assert data.run_at == "2026-01-01"

    def test_load_v2_full(self, tmp_path):
        """v2 完整结构"""
        self._make_images_json(tmp_path)
        repo = ManifestRepository(tmp_path)
        payload = {
            "schema_version": 2,
            "provider": "manga",
            "status": "partial",
            "run_id": "abc",
            "run_at": "2026-07-15",
            "source_fingerprint": "sha256-src",
            "config_fingerprint": "sha256-cfg",
            "result_count": 1,
            "result_map": {"a.jpg": "manga/a.png"},
            "skipped_images": ["b.jpg"],
            "failed_images": {"c.jpg": "OCR failed"},
        }
        (tmp_path / "image_translation_result.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        data = repo.load()
        assert data.is_v2
        assert data.provider == "manga"
        assert data.source_fingerprint == "sha256-src"
        assert data.skipped_images == ["b.jpg"]
        assert data.failed_images == {"c.jpg": "OCR failed"}

    def test_load_missing_returns_none(self, tmp_path):
        self._make_images_json(tmp_path)
        repo = ManifestRepository(tmp_path)
        assert repo.load() is None

    def test_load_result_map_backward_compat(self, tmp_path):
        """load_result_map 兼容 v1/v2，供导出器使用"""
        self._make_images_json(tmp_path)
        repo = ManifestRepository(tmp_path)
        (tmp_path / "image_translation_result.json").write_text(
            json.dumps({"result_map": {"a.jpg": "a.png"}}), encoding="utf-8"
        )
        assert repo.load_result_map() == {"a.jpg": "a.png"}

    def test_save_v2_overwrites_old(self, tmp_path):
        """每次运行即使结果为空也覆盖旧 manifest，防止导出过期图片"""
        self._make_images_json(tmp_path)
        repo = ManifestRepository(tmp_path)
        # 旧文件
        (tmp_path / "image_translation_result.json").write_text(
            json.dumps({"result_map": {"old.jpg": "old.png"}}), encoding="utf-8"
        )
        # 空结果覆盖
        result = ImageTranslationResult(
            status=OperationStatus.SUCCEEDED,
            provider_id=ImageTranslationProviderId.MANGA,
            run_id="new",
        )
        repo.save(result, source_fingerprint="fp", run_at="2026-07-15")
        data = repo.load()
        assert data.is_v2
        assert data.result_map == {}
        assert "old.jpg" not in data.result_map

    def test_save_atomic_and_includes_fingerprint(self, tmp_path):
        self._make_images_json(tmp_path)
        repo = ManifestRepository(tmp_path)
        result = ImageTranslationResult(
            status=OperationStatus.SUCCEEDED,
            result_map={"a.jpg": "manga/a.png"},
            provider_id=ImageTranslationProviderId.MANGA,
            run_id="run-1",
        )
        repo.save(
            result,
            source_fingerprint="src-fp",
            config_fingerprint="cfg-fp",
            run_at="2026-07-15T12:00:00+08:00",
        )
        data = repo.load()
        assert data.source_fingerprint == "src-fp"
        assert data.config_fingerprint == "cfg-fp"
        assert data.provider == "manga"

    def test_save_sanitizes_failed_errors(self, tmp_path):
        """失败原因做脱敏，不包含 Key/Base64/鉴权"""
        self._make_images_json(tmp_path)
        repo = ManifestRepository(tmp_path)
        result = ImageTranslationResult(
            status=OperationStatus.FAILED,
            failed_images={"a.jpg": "OCR failed with api_key=sk-secret123 and Bearer token"},
            provider_id=ImageTranslationProviderId.MANGA,
        )
        repo.save(result)
        data = repo.load()
        assert "sk-secret123" not in data.failed_images["a.jpg"]
        assert "credential" in data.failed_images["a.jpg"].lower()

    def test_save_truncates_long_errors(self, tmp_path):
        self._make_images_json(tmp_path)
        repo = ManifestRepository(tmp_path)
        long_msg = "x" * 1000
        result = ImageTranslationResult(
            status=OperationStatus.FAILED,
            failed_images={"a.jpg": long_msg},
            provider_id=ImageTranslationProviderId.MANGA,
        )
        repo.save(result)
        data = repo.load()
        assert len(data.failed_images["a.jpg"]) <= 400


# ── Provider 注册表 ──────────────────────────────────────


class TestProviderRegistry:
    def test_register_and_get(self):
        registry = ImageTranslationProviderRegistry()
        provider = FakeImageTranslationProvider(provider_id="manga")
        registry.register(provider)
        assert registry.get("manga") is provider

    def test_get_by_enum(self):
        registry = ImageTranslationProviderRegistry()
        provider = FakeImageTranslationProvider(provider_id="manga")
        registry.register(provider)
        assert registry.get(ImageTranslationProviderId.MANGA) is provider

    def test_get_unregistered_returns_none(self):
        registry = ImageTranslationProviderRegistry()
        assert registry.get("ai_volcengine") is None

    def test_manga_disabled_returns_none(self):
        """功能开关禁用时 Manga Provider 返回 None，不自动改选 AI"""
        registry = ImageTranslationProviderRegistry()
        manga = FakeImageTranslationProvider(provider_id="manga")
        ai = FakeImageTranslationProvider(provider_id="ai_volcengine")
        registry.register(manga)
        registry.register(ai)
        registry.manga_provider_available = False
        assert registry.get("manga") is None
        # AI 仍可取得
        assert registry.get("ai_volcengine") is ai

    def test_list_available_excludes_disabled_manga(self):
        registry = ImageTranslationProviderRegistry()
        registry.register(FakeImageTranslationProvider(provider_id="manga"))
        registry.register(FakeImageTranslationProvider(provider_id="ai_volcengine"))
        registry.manga_provider_available = False
        available = registry.list_available()
        assert "manga" not in available
        assert "ai_volcengine" in available


# ── 应用服务 ──────────────────────────────────────


def _make_request(tmp_path, provider_id=ImageTranslationProviderId.MANGA, **kw):
    images_json = tmp_path / "images.json"
    if not images_json.exists():
        images_json.write_text(
            json.dumps({"image_mappings": {"a.jpg": {"original_path": "a.jpg"}}}),
            encoding="utf-8",
        )
    return ImageTranslationRequest(
        mapping_dir=tmp_path,
        target_language=kw.get("target_language", "中文"),
        provider_id=provider_id,
        selected_images=kw.get("selected_images"),
        source_fingerprint=kw.get("source_fingerprint", ""),
        config_fingerprint=kw.get("config_fingerprint", ""),
    )


class TestImageTranslationService:
    def test_success_writes_manifest(self, tmp_path, tmp_config_manager):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        fake = FakeImageTranslationProvider(
            provider_id="manga",
            result_map={"a.jpg": "manga/a.png"},
        )
        registry.register(fake)
        service = ImageTranslationService(tmp_config_manager, registry)

        request = _make_request(tmp_path)
        result = service.translate(request)

        assert result.status == OperationStatus.SUCCEEDED
        assert result.result_map == {"a.jpg": "manga/a.png"}
        # manifest 已写入
        repo = ManifestRepository(tmp_path)
        data = repo.load()
        assert data.is_v2
        assert data.result_map == {"a.jpg": "manga/a.png"}

    def test_partial_success(self, tmp_path, tmp_config_manager):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        fake = FakeImageTranslationProvider(
            provider_id="manga",
            result_map={"a.jpg": "manga/a.png"},
            skipped_images=["b.jpg"],
            failed_images={"c.jpg": "OCR failed"},
            status=OperationStatus.PARTIAL,
        )
        registry.register(fake)
        service = ImageTranslationService(tmp_config_manager, registry)

        result = service.translate(_make_request(tmp_path))
        assert result.status == OperationStatus.PARTIAL
        assert len(result.skipped_images) == 1
        assert result.failed_images["c.jpg"] == "OCR failed"

    def test_all_failed(self, tmp_path, tmp_config_manager):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        fake = FakeImageTranslationProvider(
            provider_id="manga",
            failed_images={"a.jpg": "model load failed"},
            status=OperationStatus.FAILED,
        )
        registry.register(fake)
        service = ImageTranslationService(tmp_config_manager, registry)

        result = service.translate(_make_request(tmp_path))
        assert result.status == OperationStatus.FAILED
        assert result.result_map == {}

    def test_cancelled_writes_empty_manifest(self, tmp_path, tmp_config_manager):
        """取消时写入空 manifest，清除旧结果"""
        from src.application.image_translation_service import ImageTranslationService

        # 先写入旧 manifest
        repo = ManifestRepository(tmp_path)
        repo.save(
            ImageTranslationResult(
                status=OperationStatus.SUCCEEDED,
                result_map={"old.jpg": "old.png"},
                provider_id=ImageTranslationProviderId.MANGA,
            )
        )

        registry = ImageTranslationProviderRegistry()
        fake = FakeImageTranslationProvider(
            provider_id="manga", raise_on_translate=ImageTranslationCancelled()
        )
        registry.register(fake)
        service = ImageTranslationService(tmp_config_manager, registry)

        with pytest.raises(ImageTranslationCancelled):
            service.translate(_make_request(tmp_path))

        # 旧结果应被清除
        data = ManifestRepository(tmp_path).load()
        assert data.result_map == {}

    def test_validate_failure_raises_config_error(self, tmp_path, tmp_config_manager):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        fake = FakeImageTranslationProvider(
            provider_id="manga",
            validation_errors=["Manga 引擎未安装"],
        )
        registry.register(fake)
        service = ImageTranslationService(tmp_config_manager, registry)

        with pytest.raises(ImageTranslationConfigError):
            service.translate(_make_request(tmp_path))
        # 未调用 translate
        assert not fake.translate_called

    def test_missing_mapping_dir(self, tmp_path, tmp_config_manager):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        registry.register(FakeImageTranslationProvider(provider_id="manga"))
        service = ImageTranslationService(tmp_config_manager, registry)

        request = ImageTranslationRequest(
            mapping_dir=tmp_path / "nonexistent",
            target_language="中文",
            provider_id=ImageTranslationProviderId.MANGA,
        )
        with pytest.raises(ImageTranslationConfigError):
            service.translate(request)

    def test_missing_images_json(self, tmp_path, tmp_config_manager):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        registry.register(FakeImageTranslationProvider(provider_id="manga"))
        service = ImageTranslationService(tmp_config_manager, registry)

        request = ImageTranslationRequest(
            mapping_dir=tmp_path,
            target_language="中文",
            provider_id=ImageTranslationProviderId.MANGA,
        )
        with pytest.raises(ImageTranslationConfigError):
            service.translate(request)

    def test_progress_callback_invoked(self, tmp_path, tmp_config_manager):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        fake = FakeImageTranslationProvider(
            provider_id="manga",
            result_map={"a.jpg": "a.png"},
        )
        registry.register(fake)
        service = ImageTranslationService(tmp_config_manager, registry)

        events = []
        service.translate(_make_request(tmp_path), on_progress=events.append)
        assert len(events) > 0
        assert isinstance(events[0], ImageTranslationProgress)

    def test_provider_closed_on_demand(self, tmp_path, tmp_config_manager):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        fake = FakeImageTranslationProvider(provider_id="manga")
        registry.register(fake)
        service = ImageTranslationService(tmp_config_manager, registry)

        service.close_provider(ImageTranslationProviderId.MANGA)
        assert fake.close_called

    def test_cancel_idempotent(self, tmp_path, tmp_config_manager):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        fake = FakeImageTranslationProvider(provider_id="manga")
        registry.register(fake)
        service = ImageTranslationService(tmp_config_manager, registry)

        service.cancel(ImageTranslationProviderId.MANGA)
        service.cancel(ImageTranslationProviderId.MANGA)
        assert fake.cancel_called


# ── 配置迁移 ──────────────────────────────────────


class TestConfigMigration:
    def test_default_provider_always_manga(self, tmp_config_manager):
        """新安装和旧配置升级后，默认 Provider 均为 manga"""
        config = tmp_config_manager.get_image_translation_config()
        assert config["default_provider"] == "manga"

    def test_default_volc_api_config_is_available(self, tmp_config_manager):
        config = tmp_config_manager.get_image_translation_config()["ai_volcengine"]

        assert config["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
        assert config["model"] == "doubao-seedream-5-0-pro-260628"

    def test_missing_section_auto_fill(self, tmp_config_manager):
        """配置缺少 image_translation 时自动补齐"""
        # 写入一个没有 image_translation 段的配置
        app_config = tmp_config_manager.get_app_config()
        app_config.pop("image_translation", None)
        tmp_config_manager.save_app_config(app_config)
        # 重新加载
        tmp_config_manager.app_config = tmp_config_manager.load_app_config()
        config = tmp_config_manager.get_image_translation_config()
        assert config["default_provider"] == "manga"
        assert "manga" in config
        assert "ai_volcengine" in config

    def test_default_provider_not_overridden_by_ai(self, tmp_config_manager):
        """用户单次选择 AI 不写回 default_provider"""
        # 模拟旧配置被篡改为 AI
        app_config = tmp_config_manager.get_app_config()
        app_config["image_translation"] = {
            "default_provider": "ai_volcengine",  # 被篡改
            "manga": {"quality_preset": "high_quality"},
        }
        tmp_config_manager.save_app_config(app_config)
        tmp_config_manager.app_config = tmp_config_manager.load_app_config()
        # default_provider 应被强制重置为 manga
        assert tmp_config_manager.get_image_translation_config()["default_provider"] == "manga"
        # 用户自定义的 manga 段字段保留
        assert (
            tmp_config_manager.get_image_translation_config()["manga"]["quality_preset"]
            == "high_quality"
        )

    def test_preserves_volc_key(self, tmp_config_manager):
        """保留旧 image_gen_provider 和密钥环中的 volc:ark_api_key"""
        tmp_config_manager.save_volc_key("test-volc-key-123")
        app_config = tmp_config_manager.get_app_config()
        # image_gen_provider 保留
        assert app_config.get("image_gen_provider") == "volcengine"
        # volc key 仍可读取
        assert tmp_config_manager.get_volc_key() == "test-volc-key-123"

    def test_get_default_provider_method(self, tmp_config_manager):
        assert tmp_config_manager.get_default_image_translation_provider() == "manga"


# ── 行为测试：Manga 失败不调用 AI ──────────────────────────────────────


class TestNoAutoFallback:
    """验收标准 #5：Manga 无文字、失败、取消和缺模型场景的 AI 调用次数均为 0"""

    def _setup_service(self, tmp_config_manager, manga_fake, ai_fake):
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        registry.register(manga_fake)
        registry.register(ai_fake)
        return ImageTranslationService(tmp_config_manager, registry)

    def test_manga_failure_does_not_call_ai(self, tmp_path, tmp_config_manager):
        manga = FakeImageTranslationProvider(
            provider_id="manga",
            failed_images={"a.jpg": "model load failed"},
            status=OperationStatus.FAILED,
        )
        ai = FakeImageTranslationProvider(provider_id="ai_volcengine")
        service = self._setup_service(tmp_config_manager, manga, ai)

        service.translate(_make_request(tmp_path))
        assert not ai.translate_called

    def test_manga_no_text_does_not_call_ai(self, tmp_path, tmp_config_manager):
        """Manga 处理后无可翻译文字，不调用 AI"""
        manga = FakeImageTranslationProvider(
            provider_id="manga",
            skipped_images=["a.jpg"],
            status=OperationStatus.SUCCEEDED,
        )
        ai = FakeImageTranslationProvider(provider_id="ai_volcengine")
        service = self._setup_service(tmp_config_manager, manga, ai)

        service.translate(_make_request(tmp_path))
        assert not ai.translate_called

    def test_manga_cancelled_does_not_call_ai(self, tmp_path, tmp_config_manager):
        manga = FakeImageTranslationProvider(
            provider_id="manga",
            raise_on_translate=ImageTranslationCancelled(),
        )
        ai = FakeImageTranslationProvider(provider_id="ai_volcengine")
        service = self._setup_service(tmp_config_manager, manga, ai)

        with pytest.raises(ImageTranslationCancelled):
            service.translate(_make_request(tmp_path))
        assert not ai.translate_called

    def test_manga_missing_does_not_call_ai(self, tmp_path, tmp_config_manager):
        """Manga 模块未注册（缺模型）时，不自动调用 AI"""
        from src.application.image_translation_service import ImageTranslationService

        registry = ImageTranslationProviderRegistry()
        ai = FakeImageTranslationProvider(provider_id="ai_volcengine")
        registry.register(ai)
        # Manga 未注册
        service = ImageTranslationService(tmp_config_manager, registry)

        with pytest.raises(ImageTranslationConfigError):
            service.translate(_make_request(tmp_path))
        assert not ai.translate_called

    def test_ai_only_via_explicit_provider_id(self, tmp_path, tmp_config_manager):
        """只有显式 AI 请求才会调用 AI Provider"""
        manga = FakeImageTranslationProvider(provider_id="manga", result_map={})
        ai = FakeImageTranslationProvider(
            provider_id="ai_volcengine", result_map={"a.jpg": "ai/a.png"}
        )
        service = self._setup_service(tmp_config_manager, manga, ai)

        # 显式请求 AI
        result = service.translate(
            _make_request(tmp_path, provider_id=ImageTranslationProviderId.AI_VOLCENGINE)
        )
        assert ai.translate_called
        assert not manga.translate_called
        assert result.provider_id == ImageTranslationProviderId.AI_VOLCENGINE


# ── 阶段 4：UI 接线（默认 Manga / AI 显式入口） ──────────────


class _StubConfigManager:
    """轻量 config_manager 替身，仅实现 handler 用到的方法。"""

    def __init__(self, *, volc_key="", api_configured=True):
        self._volc_key = volc_key
        self._api_configured = api_configured

    def get_volc_key(self):
        return self._volc_key

    def is_api_configured(self):
        return self._api_configured

    def get_app_config(self):
        return {"target_language": "中文"}

    def get_image_translation_config(self):
        return {
            "default_provider": "manga",
            "manga": {
                "quality_preset": "standard",
                "device": "auto",
                "model_dir": "",
                "batch_size": 1,
            },
            "ai_volcengine": {"provider": "volcengine"},
        }


def _build_handler(mapping_dir, *, volc_key="", api_configured=True):
    """绕过 __init__ 构造一个 ImageTranslationHandler，避免创建 Tk。"""
    from src.domain.edition import EditionCapabilities
    from src.ui.image_translation_handler import ImageTranslationHandler

    handler = ImageTranslationHandler.__new__(ImageTranslationHandler)
    handler.config_manager = _StubConfigManager(volc_key=volc_key, api_configured=api_configured)
    handler.status_updater = lambda _msg: None
    handler.image_progress_updater = lambda _msg: None
    handler.get_mapping_dir = lambda: mapping_dir
    handler.open_settings = lambda: None
    handler._app_paths = None
    handler._font_path = None
    handler._service = None
    handler._worker_thread = None
    # P0-2：绕过 __init__ 的测试替身需手动注入 Full edition 能力，
    # 否则 start_image_translation 会因属性缺失而抛 AttributeError。
    handler._edition_capabilities = EditionCapabilities.full()
    # _safe_after 直接执行回调，避免依赖 Tk after
    handler._safe_after = lambda func: func()
    handler.root = type(
        "_StubRoot",
        (),
        {"after": lambda self, _delay, func: func()},
    )()
    return handler


def _write_images_json(mapping_dir, *, with_images=True):
    """写入 images.json，模拟 EPUB 已导入。"""
    if with_images:
        data = {"image_mappings": {"a.jpg": {"original_path": "a.jpg"}}}
    else:
        data = {"image_mappings": {}}
    (mapping_dir / "images.json").write_text(json.dumps(data), encoding="utf-8")


class TestPhase4HandlerWiring:
    """阶段 4 验收：UI 入口分派正确，AI 调用次数受控。"""

    def test_start_image_translation_dispatches_manga(self, tmp_path, monkeypatch):
        """主按钮入口直接分派 Manga Provider，不弹选择对话框。"""
        _write_images_json(tmp_path)
        handler = _build_handler(tmp_path)

        # 拦截 migrate_legacy_images（inline import）
        import sys
        import types

        fake_module = types.ModuleType("src.infrastructure.image_asset_store")
        fake_module.migrate_legacy_images = lambda _mapping_dir: None
        monkeypatch.setitem(sys.modules, "src.infrastructure.image_asset_store", fake_module)

        captured = []
        handler._start_translation = lambda pid: captured.append(pid)

        handler.start_image_translation()

        assert captured == [ImageTranslationProviderId.MANGA]

    def test_manga_entry_works_without_volc_key(self, tmp_path, monkeypatch):
        """无火山 Key 时 Manga 入口仍可启动，证明默认模块不依赖 AI。"""
        _write_images_json(tmp_path)
        # 显式不配置火山 Key
        handler = _build_handler(tmp_path, volc_key="", api_configured=True)

        import sys
        import types

        fake_module = types.ModuleType("src.infrastructure.image_asset_store")
        fake_module.migrate_legacy_images = lambda _mapping_dir: None
        monkeypatch.setitem(sys.modules, "src.infrastructure.image_asset_store", fake_module)

        captured = []
        handler._start_translation = lambda pid: captured.append(pid)

        handler.start_image_translation()

        assert captured == [ImageTranslationProviderId.MANGA]

    def test_ai_entry_dispatches_ai_after_confirmation(self, tmp_path, monkeypatch):
        """AI 入口在用户二次确认后分派 AI Provider。"""
        _write_images_json(tmp_path)
        handler = _build_handler(tmp_path, volc_key="volc-test-key")

        # 模拟用户点击「确认」
        import src.ui.image_translation_handler as handler_mod

        monkeypatch.setattr(handler_mod.messagebox, "askyesno", lambda *a, **k: True)

        captured = []
        handler._start_translation = lambda pid: captured.append(pid)

        handler.start_ai_image_translation()

        assert captured == [ImageTranslationProviderId.AI_VOLCENGINE]

    def test_ai_entry_aborts_without_volc_key(self, tmp_path, monkeypatch):
        """无火山 Key 时 AI 入口打开设置且不启动任何翻译。"""
        _write_images_json(tmp_path)
        handler = _build_handler(tmp_path, volc_key="")

        # 拦截 showwarning 弹窗，避免阻塞测试
        import src.ui.image_translation_handler as handler_mod

        monkeypatch.setattr(handler_mod.messagebox, "showwarning", lambda *a, **k: None)

        settings_called = []
        handler.open_settings = lambda: settings_called.append(True)

        captured = []
        handler._start_translation = lambda pid: captured.append(pid)

        handler.start_ai_image_translation()

        # 不分派任何 Provider
        assert captured == []
        # 调用了 open_settings 引导用户配置
        assert settings_called == [True]

    def test_ai_entry_aborts_when_user_declines_confirmation(self, tmp_path, monkeypatch):
        """用户在二次确认对话框点击「否」时不启动 AI。"""
        _write_images_json(tmp_path)
        handler = _build_handler(tmp_path, volc_key="volc-test-key")

        import src.ui.image_translation_handler as handler_mod

        monkeypatch.setattr(handler_mod.messagebox, "askyesno", lambda *a, **k: False)

        captured = []
        handler._start_translation = lambda pid: captured.append(pid)

        handler.start_ai_image_translation()

        assert captured == []

    def test_manga_validate_failure_in_worker_does_not_call_ai(self, tmp_path, monkeypatch):
        """Manga 校验失败时 worker 不调用 AI Provider（不自动切换）。

        通过替换 _get_service 返回一个 Manga 校验失败 + AI 跟踪调用的 Service，
        直接执行 worker 主体（不另起线程）。
        """
        _write_images_json(tmp_path)
        handler = _build_handler(tmp_path, volc_key="volc-test-key")

        # 拦截 messagebox 弹窗，避免阻塞测试
        import src.ui.image_translation_handler as handler_mod

        monkeypatch.setattr(handler_mod.messagebox, "showerror", lambda *a, **k: None)
        monkeypatch.setattr(handler_mod.messagebox, "showinfo", lambda *a, **k: None)
        monkeypatch.setattr(handler_mod.messagebox, "showwarning", lambda *a, **k: None)

        # 构造一个 Service：Manga validate 失败，AI 可被调用以便跟踪
        registry = ImageTranslationProviderRegistry()
        manga_fake = FakeImageTranslationProvider(
            provider_id="manga",
            validation_errors=["Manga engine not installed"],
        )
        ai_fake = FakeImageTranslationProvider(provider_id="ai_volcengine")
        registry.register(manga_fake)
        registry.register(ai_fake)

        from src.application.image_translation_service import ImageTranslationService

        service = ImageTranslationService(handler.config_manager, registry)
        handler._service = service

        # 直接调用 worker（同步执行，不开线程）
        handler._translation_worker(ImageTranslationProviderId.MANGA)

        # 关键断言：Manga 校验失败后 AI Provider 从未被调用
        assert not ai_fake.translate_called


class TestConcurrentWindowQueueWiring:
    """阶段 4 验收：队列图片翻译默认走 Manga，移除火山 Key 硬编码。"""

    def test_translate_all_images_requires_api_config_not_volc_key(self, tmp_path, monkeypatch):
        """队列图片翻译入口只检查 API 配置（Manga external_llm 用），不检查火山 Key。"""
        from src.domain.edition import EditionCapabilities
        from src.ui.concurrent_window import ConcurrentWindow

        # 绕过 __init__
        win = ConcurrentWindow.__new__(ConcurrentWindow)
        win.config_manager = _StubConfigManager(volc_key="", api_configured=True)
        win.app_paths = None
        win.edition_capabilities = EditionCapabilities.full()

        # manager.get_all_tasks() 返回空列表 → 进入「没有已完成的EPUB任务」分支
        win.manager = type(
            "_StubManager",
            (),
            {"get_all_tasks": lambda self: []},
        )()
        win.win = type(
            "_StubWin",
            (),
            {
                "winfo_exists": lambda self: True,
                "title": lambda self, _t: None,
            },
        )()
        # P1-8：新增的图片翻译 worker 状态机属性（绕过 __init__ 时需手动设置）
        win._image_translate_busy = False
        win._image_translate_run_id = 0

        # 捕获 message：期望「没有已完成的EPUB任务」（API 已配置 + 无火山 Key 也能进入）
        shown = []
        import src.ui.concurrent_window as cw_mod

        monkeypatch.setattr(cw_mod.messagebox, "showinfo", lambda *a, **k: shown.append(a))
        monkeypatch.setattr(cw_mod.messagebox, "showwarning", lambda *a, **k: shown.append(a))

        win._translate_all_images()

        # 第一个消息是「提示」分支（没有已完成任务），证明入口放行（未因缺火山 Key 中止）
        assert shown, "应当弹出一个消息"
        assert "没有已完成的EPUB任务" in shown[0][1]
