#!/usr/bin/env python3
"""
pytest 全局配置与共享 fixtures

为领域层、EPUB 处理器、配置管理器、密钥存储等提供可独立测试的隔离环境。
所有 fixture 均不依赖 Tkinter，可纯命令行运行。
"""

import os
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

# 将项目根加入 sys.path，便于直接 import src.*
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── 临时目录 fixtures ────────────────────────────────


@pytest.fixture()
def tmp_app_paths(tmp_path):
    """构造一个完全隔离的 AppPaths，指向 tmp_path 下的子目录。"""
    from src.app_paths import AppPaths

    return AppPaths.create(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        workspace_dir=tmp_path / "workspace",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture()
def tmp_config_manager(tmp_app_paths, reset_secure_storage_state):
    """基于临时 AppPaths 的 ConfigManager。

    依赖 reset_secure_storage_state 以确保 _keyring_available 缓存和环境变量
    在每个测试前都被重置，避免跨测试污染（R2-BUG-002 测试稳定性）。
    """
    from src.config.config_manager import ConfigManager
    from src.domain.secret import StorageStatus

    class InMemorySecretStore:
        """Prevent tests from reading or overwriting the user's OS keyring."""

        def __init__(self):
            self.values = {}

        def store(self, identifier, key):
            self.values[identifier] = key
            return StorageStatus.PERSISTED

        def retrieve(self, identifier):
            return self.values.get(identifier, "")

        def delete(self, identifier):
            self.values.pop(identifier, None)
            return True

    return ConfigManager(
        app_paths=tmp_app_paths,
        secret_store=InMemorySecretStore(),
    )


# ── 密钥环隔离 fixtures ──────────────────────────────


@pytest.fixture()
def reset_secure_storage_state():
    """每个测试前后重置 secure_storage 的全局缓存状态。

    R2-BUG-002：避免 _keyring_available 缓存跨测试污染。
    """
    from src.utils import secure_storage

    saved_available = secure_storage._keyring_available
    saved_env = dict(os.environ)
    secure_storage._keyring_available = None
    try:
        yield secure_storage
    finally:
        secure_storage._keyring_available = saved_available
        # 清理本测试新增的环境变量
        for key in list(os.environ.keys()):
            if key.startswith("AI_TRANSLATOR_KEY_") and key not in saved_env:
                os.environ.pop(key, None)
        # 恢复被修改的环境变量
        os.environ.update(saved_env)


# ── EPUB fixture 构造 ────────────────────────────────


def _build_epub_chapter(title: str, paragraphs: List[str], lang: str = "ja") -> bytes:
    """构造一个最小化的 EPUB 章节 HTML。"""
    paras = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="{lang}">
<head><title>{title}</title><meta charset="utf-8"/></head>
<body>
<h1>{title}</h1>
{paras}
</body>
</html>""".encode()


def _make_minimal_epub(
    output_path: Path,
    *,
    chapters: List[Tuple[str, List[str]]],
    use_string_spine: bool = False,
    images: List[Tuple[str, bytes]] = None,
    nested_dirs: bool = False,
) -> Path:
    """构造一个最小可解析的 EPUB 文件。

    Args:
        output_path: 输出 .epub 路径
        chapters: [(title, [paragraphs]), ...]
        use_string_spine: True 时让 book.spine 为字符串形式（触发 R2-BUG-001）
        images: [(image_path, image_bytes), ...]
        nested_dirs: True 时把章节放到 part1/ part2/ 子目录（触发 R2-BUG-005）
    """
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("test-epub-id")
    book.set_title("测试 EPUB")
    book.set_language("ja")

    spine_items = []
    for idx, (title, paragraphs) in enumerate(chapters, start=1):
        if nested_dirs:
            file_name = f"part{(idx % 2) + 1}/chapter{idx}.xhtml"
        else:
            file_name = f"Text/chapter{idx}.xhtml"

        chapter = epub.EpubHtml(
            title=title,
            file_name=file_name,
            lang="ja",
        )
        chapter.content = _build_epub_chapter(title, paragraphs)
        book.add_item(chapter)
        spine_items.append(chapter)

    # 设置 spine：字符串形式或对象形式
    if use_string_spine:
        # R2-BUG-001 场景：spine 条目为字符串 idref
        book.spine = [(item.get_id(), "yes") for item in spine_items]
    else:
        book.spine = spine_items

    # 添加图片
    if images:
        for img_path, img_bytes in images:
            img_item = epub.EpubImage(
                uid=f"img_{Path(img_path).stem}",
                file_name=img_path,
                media_type="image/png",
                content=img_bytes,
            )
            book.add_item(img_item)

    # 必备项
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # TOC
    book.toc = tuple(spine_items)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(output_path), book)
    return output_path


@pytest.fixture()
def make_epub(tmp_path):
    """返回一个工厂函数，用于按需构造 EPUB fixture。"""
    counter = {"n": 0}

    def _factory(**kwargs) -> Path:
        counter["n"] += 1
        path = tmp_path / f"test_epub_{counter['n']}.epub"
        return _make_minimal_epub(path, **kwargs)

    return _factory


@pytest.fixture()
def simple_epub(make_epub) -> Path:
    """一个最简单的 EPUB：两个章节，对象 spine。"""
    return make_epub(
        chapters=[
            ("第一章", ["これはテストです。", "二行目の内容です。"]),
            ("第二章", ["三章目の内容です。", "終わり。"]),
        ],
    )


@pytest.fixture()
def string_spine_epub(make_epub) -> Path:
    """R2-BUG-001：spine 条目为字符串 idref 形式。"""
    return make_epub(
        chapters=[
            ("第一章", ["文字列 spine のテスト。"]),
            ("第二章", ["二つ目の章です。"]),
        ],
        use_string_spine=True,
    )


@pytest.fixture()
def nested_dirs_epub(make_epub) -> Path:
    """R2-BUG-005：同名章节位于不同子目录。"""
    return make_epub(
        chapters=[
            ("第一章", ["part1 の内容です。"]),
            ("第二章", ["part2 の内容です。"]),
        ],
        nested_dirs=True,
    )


@pytest.fixture()
def image_epub(make_epub) -> Path:
    """包含图片的 EPUB（R2-BUG-016/017/018）。"""
    # 1x1 PNG
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa3\x1f\x16\xb7\x00\x00\x00\x00IEND\xaeB`\x82"
    return make_epub(
        chapters=[("第一章", ["画像テスト。"])],
        images=[("Images/cover.png", png_bytes), ("Images/inner.png", png_bytes)],
    )
