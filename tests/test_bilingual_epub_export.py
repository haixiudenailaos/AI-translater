#!/usr/bin/env python3
"""双语 EPUB 导出集成测试：导入 → 快照 → 写出 → 重读校验。"""

from __future__ import annotations

import json
import queue
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from ebooklib import epub

from src.application.export_job import ExportProgress, ExportResult
from src.core.epub_processor import EPUBProcessor
from src.domain.epub_export import (
    BilingualExportReport,
    BilingualRenderPlan,
    EpubDocumentSnapshot,
    EpubExportOptions,
    EpubTextMode,
)
from src.domain.errors import BilingualEpubExportError
from src.infrastructure.bilingual_renderer import (
    HEADING_TARGET_CLASS,
    PAIR_ATTRIBUTE,
    SOURCE_CLASS,
    TARGET_CLASS,
)
from src.infrastructure.bilingual_validation import validate_bilingual_epub
from src.infrastructure.exporter import compute_file_hash, export_epub
from src.infrastructure.mapping_repository import load_content_mapping
from src.ui import translation_controller as controller_module
from src.ui.main_window import MainWindow
from src.ui.translation_controller import TranslationController

XML_NS = "{http://www.w3.org/XML/1998/namespace}"


def _chapter_html(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" lang="ja">'
        "<head><title>chapter</title></head>"
        f"<body>{body}</body></html>"
    ).encode()


def _write_epub(
    path: Path,
    chapters: list[tuple[str, str]],
    *,
    styles: dict[str, bytes] | None = None,
    images: dict[str, bytes] | None = None,
) -> Path:
    book = epub.EpubBook()
    book.set_identifier("bilingual-export-test")
    book.set_title("双语导出测试")
    book.set_language("ja")
    items = []
    for index, (file_part, body) in enumerate(chapters, start=1):
        chapter = epub.EpubHtml(
            title=f"chapter{index}", file_name=f"Text/{file_part}.xhtml", lang="ja"
        )
        chapter.content = _chapter_html(body)
        book.add_item(chapter)
        items.append(chapter)
    for index, (file_name, content) in enumerate((styles or {}).items()):
        book.add_item(
            epub.EpubItem(
                uid=f"existing-style-{index}",
                file_name=file_name,
                media_type="text/css",
                content=content,
            )
        )
    for index, (file_name, content) in enumerate((images or {}).items()):
        book.add_item(
            epub.EpubImage(
                uid=f"existing-image-{index}",
                file_name=file_name,
                media_type="image/png",
                content=content,
            )
        )
    book.spine = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = tuple(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(path), book)
    return path


@pytest.fixture()
def source_epub(tmp_path) -> Path:
    return _write_epub(
        tmp_path / "source.epub",
        [
            (
                "chapter1",
                '<h1 id="h1">第一話</h1>'
                '<p id="p1">彼は<strong>静かに</strong>笑った。</p>'
                '<p id="p2">同じ原文</p>'
                '<p id="p3">同じ原文</p>'
                "<ul><li>項目一</li></ul>"
                "<blockquote>引用文</blockquote>",
            ),
            (
                "chapter2",
                '<img src="../Images/pic.png"/>' '<p class="hidden" id="running">Running title</p>',
            ),
        ],
    )


@pytest.fixture()
def imported(source_epub, tmp_app_paths):
    processor = EPUBProcessor(app_paths=tmp_app_paths)
    result = processor.import_epub(str(source_epub))
    mapping_dir = Path(result["mapping_dir"])
    originals, _ = load_content_mapping(str(mapping_dir))
    return processor, mapping_dir, originals


def _snapshot(source_epub: Path, mapping_dir: Path, originals: list[str], targets: list[str]):
    return EpubDocumentSnapshot(
        session_id="session-1",
        document_version=3,
        source_path=source_epub,
        mapping_dir=mapping_dir,
        source_lines=tuple(originals),
        target_lines=tuple(targets),
    )


def _translated_targets(originals: list[str], *, empty_index: int | None = None) -> list[str]:
    targets = [f"译文{i + 1}" for i in range(len(originals))]
    if empty_index is not None:
        targets[empty_index] = ""
    return targets


def _document(archive: ZipFile, fragment: str) -> ElementTree.Element:
    for name in archive.namelist():
        if name.endswith(fragment):
            return ElementTree.fromstring(archive.read(name))
    raise AssertionError(f"输出中缺少章节 {fragment}")


def _class_tokens(element: ElementTree.Element) -> set[str]:
    return set(str(element.get("class") or "").split())


def _targets(root: ElementTree.Element) -> list[ElementTree.Element]:
    return [element for element in root.iter() if TARGET_CLASS in _class_tokens(element)]


def _bilingual_options() -> EpubExportOptions:
    return EpubExportOptions(text_mode=EpubTextMode.BILINGUAL, target_language_tag="zh")


def test_bilingual_export_pairs_segments_and_preserves_original(source_epub, imported, tmp_path):
    _, mapping_dir, originals = imported
    empty_index = originals.index("同じ原文")  # 第一条重复原文保持未译
    targets = _translated_targets(originals, empty_index=empty_index)
    hidden_index = originals.index("Running title")
    snapshot = _snapshot(source_epub, mapping_dir, originals, targets)
    reports: list[BilingualExportReport] = []
    output = tmp_path / "bilingual.epub"
    mapping_before = (mapping_dir / "content_mapping.json").read_bytes()
    manifest_before = (mapping_dir / "mapping_manifest.json").read_bytes()

    export_epub(
        str(mapping_dir),
        str(output),
        options=_bilingual_options(),
        snapshot=snapshot,
        on_report=reports.append,
    )

    assert (mapping_dir / "content_mapping.json").read_bytes() == mapping_before
    assert (mapping_dir / "mapping_manifest.json").read_bytes() == manifest_before

    assert len(reports) == 1
    report = reports[0]
    assert report.total_segments == len(originals)
    assert report.untranslated_segments == 1
    assert report.preserved_segments == 1
    assert report.paired_segments == len(originals) - 2

    with ZipFile(output) as archive:
        names = archive.namelist()
        assert "EPUB/Styles/ait-bilingual.css" in names
        chapter = _document(archive, "Text/chapter1.xhtml")

        p1 = chapter.find(".//{*}p[@id='p1']")
        assert p1 is not None
        assert p1.find(".//{*}strong") is not None  # 原文内联结构保留
        assert "彼は" in "".join(p1.itertext())
        target_after_p1 = [
            node for node in chapter.iter() if node.get(PAIR_ATTRIBUTE) == "line_000002"
        ]
        assert len(target_after_p1) == 2  # 原节点标记 + 译文容器

        target_texts = {
            node.get(PAIR_ATTRIBUTE): "".join(node.itertext()).strip() for node in _targets(chapter)
        }
        assert "译文2" in target_texts.values()
        assert f"line_{hidden_index + 1:06d}" not in target_texts
        assert "译文3" not in target_texts.values()  # 第一条重复原文未译
        assert "译文4" in target_texts.values()  # 第二条重复原文按行号正确配对

        image_chapter = _document(archive, "Text/chapter2.xhtml")
        assert not _targets(image_chapter)
        running = image_chapter.find(".//{*}p[@id='running']")
        assert running is not None and "".join(running.itertext()).strip() == "Running title"

        link = chapter.find(".//{*}link")
        assert link is not None and link.get("href") == "../Styles/ait-bilingual.css"

        opf = ElementTree.fromstring(archive.read("EPUB/content.opf"))
        languages = [
            (element.text or "").strip()
            for element in opf.iter()
            if element.tag.rsplit("}", 1)[-1] == "language"
        ]
        assert "ja" in languages  # 原有 package language 保留
        assert "zh" in languages  # 已知目标语言去重追加

        target = _targets(chapter)[0]
        assert target.get("lang") == "zh"
        assert target.get(f"{XML_NS}lang") == "zh"

        heading_target = [
            node for node in _targets(chapter) if HEADING_TARGET_CLASS in _class_tokens(node)
        ]
        assert heading_target and heading_target[0].tag.endswith("p")

        sources = [node for node in chapter.iter() if SOURCE_CLASS in _class_tokens(node)]
        assert len(sources) == len(_targets(chapter))


def test_repeated_export_does_not_accumulate_targets(source_epub, imported, tmp_path):
    _, mapping_dir, originals = imported
    snapshot = _snapshot(source_epub, mapping_dir, originals, _translated_targets(originals))
    source_hash = compute_file_hash(source_epub)

    first = tmp_path / "first.epub"
    second = tmp_path / "second.epub"
    export_epub(str(mapping_dir), str(first), options=_bilingual_options(), snapshot=snapshot)
    export_epub(str(mapping_dir), str(second), options=_bilingual_options(), snapshot=snapshot)

    with ZipFile(first) as archive:
        first_count = len(_targets(_document(archive, "Text/chapter1.xhtml")))
    with ZipFile(second) as archive:
        second_count = len(_targets(_document(archive, "Text/chapter2.xhtml")))
        second_chapter1 = len(_targets(_document(archive, "Text/chapter1.xhtml")))

    assert second_chapter1 == first_count
    assert second_count == 0
    assert compute_file_hash(source_epub) == source_hash


def test_bilingual_export_rejects_edited_source_text(source_epub, imported, tmp_path):
    _, mapping_dir, originals = imported
    edited = list(originals)
    edited[0] = "界面里编辑过的原文"
    snapshot = _snapshot(source_epub, mapping_dir, edited, _translated_targets(originals))

    with pytest.raises(BilingualEpubExportError, match="原文已编辑"):
        export_epub(
            str(mapping_dir),
            str(tmp_path / "out.epub"),
            options=_bilingual_options(),
            snapshot=snapshot,
        )


def test_bilingual_export_requires_a_snapshot(imported, tmp_path):
    _, mapping_dir, _originals = imported

    with pytest.raises(BilingualEpubExportError, match="缺少文档快照"):
        export_epub(
            str(mapping_dir),
            str(tmp_path / "out.epub"),
            options=_bilingual_options(),
        )


def test_bilingual_export_rejects_snapshot_in_translated_mode(imported, tmp_path, source_epub):
    _, mapping_dir, originals = imported
    snapshot = _snapshot(source_epub, mapping_dir, originals, _translated_targets(originals))

    with pytest.raises(BilingualEpubExportError, match="仅用于双语"):
        export_epub(str(mapping_dir), str(tmp_path / "out.epub"), snapshot=snapshot)


def test_source_with_existing_markers_is_rejected(tmp_path, tmp_app_paths):
    marked = _write_epub(
        tmp_path / "marked.epub",
        [("chapter1", '<p class="ait-bilingual-source">旧的双语导出</p>')],
    )
    processor = EPUBProcessor(app_paths=tmp_app_paths)
    mapping_dir = Path(processor.import_epub(str(marked))["mapping_dir"])
    originals, _ = load_content_mapping(str(mapping_dir))
    snapshot = _snapshot(marked, mapping_dir, originals, _translated_targets(originals))

    with pytest.raises(BilingualEpubExportError, match="已包含双语导出标记"):
        export_epub(
            str(mapping_dir),
            str(tmp_path / "out.epub"),
            options=_bilingual_options(),
            snapshot=snapshot,
        )


def test_plain_export_keeps_existing_replacement_behavior(imported, tmp_path):
    processor, mapping_dir, originals = imported
    targets = _translated_targets(originals)
    processor.save_translations(str(mapping_dir), targets)

    output = tmp_path / "plain.epub"
    export_epub(str(mapping_dir), str(output))

    with ZipFile(output) as archive:
        assert not any("ait-bilingual" in name for name in archive.namelist())
        chapter = _document(archive, "Text/chapter1.xhtml")
        assert not _targets(chapter)
        text = "".join(chapter.itertext())
        assert "译文2" in text
        assert "彼は" not in text


def test_legacy_mapping_without_locators_can_export_bilingual(source_epub, imported, tmp_path):
    _, mapping_dir, originals = imported
    payload = json.loads((mapping_dir / "content_mapping.json").read_text(encoding="utf-8"))
    legacy_records = {}
    for key, record in payload["content_mappings"].items():
        legacy_records[key] = {
            "line_number": record["line_number"],
            "original_text": record["original_text"],
            "translated_text": f"旧格式译文{record['line_number']}",
            "chapter_id": record["chapter_id"],
        }
    payload["content_mappings"] = legacy_records
    payload["project_info"]["original_file"] = str(source_epub)
    payload["project_info"]["source_content_hash"] = compute_file_hash(source_epub)

    (mapping_dir / "mapping_manifest.json").unlink()
    (mapping_dir / ".mapping_generations").rename(tmp_path / "stale-generations")
    (mapping_dir / "content_mapping.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    snapshot = EpubDocumentSnapshot(
        session_id="legacy",
        document_version=1,
        source_path=source_epub,
        mapping_dir=mapping_dir,
        source_lines=tuple(originals),
        target_lines=tuple(f"旧格式译文{i + 1}" for i in range(len(originals))),
    )
    output = tmp_path / "legacy.epub"
    export_epub(str(mapping_dir), str(output), options=_bilingual_options(), snapshot=snapshot)

    with ZipFile(output) as archive:
        chapter = _document(archive, "Text/chapter1.xhtml")
        assert len(_targets(chapter)) == 6


def test_existing_stylesheet_name_is_not_overwritten(tmp_path, tmp_app_paths):
    source = _write_epub(
        tmp_path / "styled.epub",
        [("chapter1", '<p id="p1">原有正文</p>')],
        styles={"Styles/ait-bilingual.css": b".original{color:red}"},
    )
    processor = EPUBProcessor(app_paths=tmp_app_paths)
    mapping_dir = Path(processor.import_epub(str(source))["mapping_dir"])
    originals, _ = load_content_mapping(str(mapping_dir))
    snapshot = _snapshot(source, mapping_dir, originals, ["新译文"])
    output = tmp_path / "styled-out.epub"

    export_epub(str(mapping_dir), str(output), options=_bilingual_options(), snapshot=snapshot)

    with ZipFile(output) as archive:
        names = archive.namelist()
        assert "EPUB/Styles/ait-bilingual.css" in names
        assert "EPUB/Styles/ait-bilingual-1.css" in names
        assert archive.read("EPUB/Styles/ait-bilingual.css") == b".original{color:red}"
        chapter = _document(archive, "Text/chapter1.xhtml")
        link = chapter.find(".//{*}link")
        assert link is not None and link.get("href") == "../Styles/ait-bilingual-1.css"


_PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfe\xa3\x1f\x16\xb7\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_inline_image_caption_does_not_invalidate_original_text(tmp_path, tmp_app_paths):
    source = _write_epub(
        tmp_path / "inline-image.epub",
        [("chapter1", '<p id="p1">插圖文字<img src="../Images/pic.png"/></p>')],
        images={"Images/pic.png": _PNG_1PX},
    )
    processor = EPUBProcessor(app_paths=tmp_app_paths)
    mapping_dir = Path(processor.import_epub(str(source))["mapping_dir"])
    originals, _ = load_content_mapping(str(mapping_dir))
    snapshot = _snapshot(source, mapping_dir, originals, ["插图文字译文"])
    image_text_map = {"Images/pic.png": {"translated_text": "图注译文", "original_text": "原图注"}}
    output = tmp_path / "inline-image-out.epub"

    export_epub(
        str(mapping_dir),
        str(output),
        None,
        image_text_map,
        options=_bilingual_options(),
        snapshot=snapshot,
    )

    with ZipFile(output) as archive:
        chapter = _document(archive, "Text/chapter1.xhtml")
        captions = [
            element for element in chapter.iter() if element.tag.rsplit("}", 1)[-1] == "figcaption"
        ]
        assert len(captions) == 1
        assert captions[0].get("data-ait-image-caption") == "true"
        assert _targets(chapter)


def test_plain_export_does_not_mark_image_captions(tmp_path, tmp_app_paths):
    source = _write_epub(
        tmp_path / "plain-image.epub",
        [("chapter1", '<p id="p1">插圖文字<img src="../Images/pic.png"/></p>')],
        images={"Images/pic.png": _PNG_1PX},
    )
    processor = EPUBProcessor(app_paths=tmp_app_paths)
    mapping_dir = Path(processor.import_epub(str(source))["mapping_dir"])
    originals, _ = load_content_mapping(str(mapping_dir))
    processor.save_translations(str(mapping_dir), [f"译文{i + 1}" for i in range(len(originals))])
    image_text_map = {"Images/pic.png": {"translated_text": "图注译文", "original_text": "原图注"}}
    output = tmp_path / "plain-image-out.epub"

    export_epub(str(mapping_dir), str(output), None, image_text_map)

    with ZipFile(output) as archive:
        chapter = _document(archive, "Text/chapter1.xhtml")
        caption = next(
            element for element in chapter.iter() if element.tag.rsplit("}", 1)[-1] == "figcaption"
        )
        assert caption.get("data-ait-image-caption") is None


def test_facade_keeps_four_argument_delegation(monkeypatch):
    from src.core import epub_processor as processor_module

    calls: list[tuple] = []

    def fake_export(*args):
        calls.append(args)
        return "ok"

    monkeypatch.setattr(processor_module, "_export_epub_impl", fake_export)
    processor = EPUBProcessor.__new__(EPUBProcessor)

    assert processor.export_epub("mapping", "out.epub") == "ok"
    assert calls == [("mapping", "out.epub", None, None)]


def test_facade_forwards_bilingual_options(monkeypatch):
    from src.core import epub_processor as processor_module

    captured: dict[str, object] = {}

    def fake_export(mapping_dir, output_path, image_map, image_text_map, **kwargs):
        captured.update(kwargs)
        return output_path

    monkeypatch.setattr(processor_module, "_export_epub_impl", fake_export)
    processor = EPUBProcessor.__new__(EPUBProcessor)
    snapshot = EpubDocumentSnapshot(
        session_id="s",
        document_version=1,
        source_path=Path("book.epub"),
        mapping_dir=Path("mapping"),
        source_lines=("a",),
        target_lines=("b",),
    )

    processor.export_epub("mapping", "out.epub", options=_bilingual_options(), snapshot=snapshot)

    assert captured["snapshot"] is snapshot
    assert isinstance(captured["options"], EpubExportOptions)
    assert captured["on_report"] is None


def test_embedded_fallback_structure_is_written_and_validated(tmp_path, tmp_app_paths):
    source = _write_epub(
        tmp_path / "embedded.epub",
        [("chapter1", '<div><p id="outer"><p id="inner">内嵌原文</p></p></div>')],
    )
    processor = EPUBProcessor(app_paths=tmp_app_paths)
    mapping_dir = Path(processor.import_epub(str(source))["mapping_dir"])
    originals, _ = load_content_mapping(str(mapping_dir))
    assert originals == ["内嵌原文"]
    snapshot = _snapshot(source, mapping_dir, originals, ["内嵌译文"])
    output = tmp_path / "embedded-out.epub"

    export_epub(str(mapping_dir), str(output), options=_bilingual_options(), snapshot=snapshot)

    with ZipFile(output) as archive:
        chapter = _document(archive, "Text/chapter1.xhtml")
        inner = chapter.find(".//{*}p[@id='inner']")
        assert inner is not None
        assert "内嵌原文" in "".join(inner.itertext())
        targets = _targets(chapter)
        assert len(targets) == 1
        assert "内嵌译文" in "".join(targets[0].itertext())


def test_validator_rejects_output_without_registered_stylesheet(tmp_path):
    source = _write_epub(tmp_path / "plain.epub", [("chapter1", "<p>正文</p>")])
    plan = BilingualRenderPlan(
        stylesheet_name="Styles/ait-bilingual.css",
        documents=(),
        report=BilingualExportReport(0, 0, 0, 0),
    )

    with pytest.raises(BilingualEpubExportError, match="样式表"):
        validate_bilingual_epub(source, plan)


def _new_controller() -> TranslationController:
    controller = TranslationController.__new__(TranslationController)
    controller._export_job = None
    controller._capture_export_snapshot = None
    controller._export_session_id = None
    controller._export_report_queue = None
    controller._export_kind = None
    controller._export_success_callback = None
    controller.get_mapping_dir = lambda: Path("mapping")
    controller.get_source_path = lambda: Path("book.epub")
    controller.get_table_data = lambda: (["source"], ["target"])
    controller._get_session_id = lambda: "session-1"
    controller._get_target_path = lambda: None
    controller.config_manager = SimpleNamespace(get_app_config=lambda: {"target_language": "中文"})
    return controller


def _patch_dialogs(monkeypatch, messages, *, save_path="out.epub"):
    monkeypatch.setattr(
        controller_module,
        "messagebox",
        SimpleNamespace(
            showerror=lambda _title, message: messages.append(("error", message)),
            showwarning=lambda _title, message: messages.append(("warning", message)),
            showinfo=lambda _title, message: messages.append(("info", message)),
        ),
    )
    monkeypatch.setattr(
        controller_module,
        "filedialog",
        SimpleNamespace(asksaveasfilename=lambda **_kwargs: save_path),
    )


def test_bilingual_entry_requires_snapshot_callback(monkeypatch):
    messages: list[tuple[str, str]] = []
    _patch_dialogs(monkeypatch, messages)
    controller = _new_controller()

    controller.export_bilingual_epub_file()

    assert messages and messages[0][0] == "error"
    assert "快照" in messages[0][1]
    assert controller._export_job is None


def test_bilingual_entry_stops_before_starting_when_no_translation(monkeypatch, tmp_path):
    messages: list[tuple[str, str]] = []
    _patch_dialogs(monkeypatch, messages, save_path=str(tmp_path / "out.epub"))
    controller = _new_controller()
    controller._capture_export_snapshot = lambda: EpubDocumentSnapshot(
        session_id="session-1",
        document_version=1,
        source_path=Path("book.epub"),
        mapping_dir=Path("mapping"),
        source_lines=("原文",),
        target_lines=("",),
    )

    controller.export_bilingual_epub_file()

    assert ("warning", "暂无可导出的译文") in messages
    assert controller._export_job is None


def test_bilingual_entry_reports_occupied_job_slot(monkeypatch):
    messages: list[tuple[str, str]] = []
    _patch_dialogs(monkeypatch, messages)
    controller = _new_controller()
    controller._export_job = object()

    controller.export_bilingual_epub_file()

    assert messages and messages[0][0] == "info"
    assert "已有导出" in messages[0][1]


class _FakeExportJob:
    is_running = False

    def __init__(self, events):
        self._events = events

    def drain_events(self):
        events, self._events = self._events, []
        return list(events)


@pytest.mark.parametrize(
    "entrypoint",
    ["save_translation", "export_comparison", "export_epub_file", "export_bilingual_epub_file"],
)
@pytest.mark.parametrize("failed", [False, True])
def test_export_entries_preserve_pending_bilingual_terminal(monkeypatch, entrypoint, failed):
    messages: list[tuple[str, str]] = []
    _patch_dialogs(monkeypatch, messages)
    for job_type in ("ExportJob", "TextExportJob"):
        monkeypatch.setattr(
            controller_module,
            job_type,
            lambda **_kwargs: pytest.fail("An occupied export slot must not start another worker"),
        )
    controller = _new_controller()
    result = (
        ExportResult(None, error_message="publish failed")
        if failed
        else ExportResult(Path("bilingual.epub"))
    )
    pending_job = _FakeExportJob([result])
    report_queue = queue.SimpleQueue()
    report_queue.put(BilingualExportReport(1, 1, 0, 0))
    controller._export_job = pending_job
    controller._export_kind = "双语 EPUB 导出"
    controller._export_session_id = "session-1"
    controller._export_report_queue = report_queue
    statuses: list[str] = []
    controller.status_updater = statuses.append

    getattr(controller, entrypoint)()

    assert controller._export_job is pending_job
    assert controller._export_session_id == "session-1"
    assert controller._export_report_queue is report_queue
    assert messages and messages[0][0] == "info"

    controller._poll_export_job()

    assert controller._export_job is None
    assert controller._export_report_queue is None
    assert not controller._export_is_running()
    if failed:
        assert messages[-1] == ("error", "双语 EPUB 导出失败: publish failed")
    else:
        assert "bilingual.epub" in messages[-1][1]
        assert "已对照 1 段" in messages[-1][1]


@pytest.mark.parametrize(
    "entrypoint", ["save_translation", "export_comparison", "export_epub_file"]
)
def test_export_entries_recheck_slot_after_save_dialog(monkeypatch, entrypoint):
    messages: list[tuple[str, str]] = []
    _patch_dialogs(monkeypatch, messages)
    for job_type in ("ExportJob", "TextExportJob"):
        monkeypatch.setattr(
            controller_module,
            job_type,
            lambda **_kwargs: pytest.fail("The save dialog occupied the export slot"),
        )
    controller = _new_controller()
    pending_job = _FakeExportJob([ExportResult(Path("bilingual.epub"))])
    report_queue = queue.SimpleQueue()
    report_queue.put(BilingualExportReport(1, 1, 0, 0))

    def save_dialog(**_kwargs):
        # Tk modal dialogs can run callbacks that occupy the shared slot.
        controller._export_job = pending_job
        controller._export_session_id = "session-1"
        controller._export_report_queue = report_queue
        return "output.epub" if entrypoint == "export_epub_file" else "output.txt"

    monkeypatch.setattr(controller_module.filedialog, "asksaveasfilename", save_dialog)

    result = getattr(controller, entrypoint)()

    if entrypoint == "save_translation":
        assert result is False
    assert controller._export_job is pending_job
    assert controller._export_report_queue is report_queue
    assert controller._export_session_id == "session-1"
    assert messages and messages[-1][0] == "info"


def test_plain_epub_export_still_cancels_a_running_job(monkeypatch):
    controller = _new_controller()
    cancelled: list[bool] = []
    statuses: list[str] = []
    running_job = SimpleNamespace(is_running=True, cancel=lambda: cancelled.append(True))
    controller._export_job = running_job
    controller.status_updater = statuses.append
    monkeypatch.setattr(controller_module.messagebox, "askyesno", lambda *_args: True)

    controller.export_epub_file()

    assert cancelled == [True]
    assert controller._export_job is running_job
    assert statuses == ["正在取消导出…"]


def _run_poll(controller, events, *, current_session_id, export_session_id):
    controller.status_updater = lambda message: controller._statuses.append(message)
    controller._statuses = []
    controller._get_session_id = lambda: current_session_id
    controller._export_kind = "双语 EPUB 导出"
    controller._export_success_callback = None
    controller._export_session_id = export_session_id
    controller._export_job = _FakeExportJob(events)
    controller._poll_export_job()
    return controller._statuses


def test_poll_skips_status_for_old_session_and_shows_report(monkeypatch):
    messages: list[tuple[str, str]] = []
    _patch_dialogs(monkeypatch, messages)
    controller = _new_controller()
    report = BilingualExportReport(
        total_segments=4, paired_segments=2, untranslated_segments=1, preserved_segments=1
    )
    controller._export_report_queue = queue.SimpleQueue()
    controller._export_report_queue.put(report)

    statuses = _run_poll(
        controller,
        [ExportProgress("export", "正在重写 EPUB 内容…"), ExportResult(Path("out.epub"))],
        current_session_id="another-session",
        export_session_id="session-1",
    )

    assert statuses == []
    info_messages = [message for kind, message in messages if kind == "info"]
    assert info_messages
    assert "已对照 2 段，1 段未译" in info_messages[0]
    assert "1 段按原布局保留" in info_messages[0]
    assert controller._export_job is None


def test_poll_updates_status_when_session_still_matches(monkeypatch):
    messages: list[tuple[str, str]] = []
    _patch_dialogs(monkeypatch, messages)
    controller = _new_controller()

    statuses = _run_poll(
        controller,
        [ExportProgress("export", "正在重写 EPUB 内容…"), ExportResult(Path("out.epub"))],
        current_session_id="session-1",
        export_session_id="session-1",
    )

    assert "正在重写 EPUB 内容…" in statuses
    assert any("双语 EPUB 导出已完成" in status for status in statuses)


def test_poll_discards_report_when_publish_fails(monkeypatch):
    messages: list[tuple[str, str]] = []
    _patch_dialogs(monkeypatch, messages)
    controller = _new_controller()
    report = BilingualExportReport(
        total_segments=1, paired_segments=1, untranslated_segments=0, preserved_segments=0
    )
    controller._export_report_queue = queue.SimpleQueue()
    controller._export_report_queue.put(report)

    _run_poll(
        controller,
        [ExportResult(None, error_message="publish failed")],
        current_session_id="session-1",
        export_session_id="session-1",
    )

    assert [kind for kind, _message in messages] == ["error"]
    assert "publish failed" in messages[0][1]
    assert controller._export_report_queue is None


def test_file_menu_exposes_bilingual_epub_export():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("当前环境无显示，无法创建 Tk root")
    try:
        window = MainWindow.__new__(MainWindow)
        window.root = root
        window.file_importer = SimpleNamespace(
            import_file=lambda: None, import_clipboard=lambda: None
        )
        invoked: list[str] = []
        window.translation_controller = SimpleNamespace(
            save_translation=lambda: None,
            export_comparison=lambda: None,
            export_epub_file=lambda: None,
            export_bilingual_epub_file=lambda: invoked.append("bilingual"),
            translate_selected_rows=lambda: None,
        )
        window.image_handler = SimpleNamespace(start_image_translation=lambda: None)
        for name in (
            "undo",
            "redo",
            "focus_search",
            "open_context_editor",
            "clear_selected_translations",
            "_run_primary_action",
            "_continue_translation",
            "run_quality_check",
            "open_concurrent",
            "open_glossary",
            "open_settings",
        ):
            setattr(window, name, lambda: None)

        window.create_menu()

        export_menu = window._export_menu
        last_index = export_menu.index("end")
        labels = [export_menu.entrycget(index, "label") for index in range(last_index + 1)]
        assert labels[-1] == "导出双语 EPUB"
        export_menu.invoke(window._bilingual_export_action_index)
        assert invoked == ["bilingual"]
    finally:
        root.destroy()


def test_controller_tracks_export_session_and_formats_report():
    controller = TranslationController.__new__(TranslationController)
    controller._export_session_id = "session-a"
    controller._get_session_id = lambda: "session-b"

    assert controller._export_belongs_to_current_session() is False
    controller._get_session_id = lambda: "session-a"
    assert controller._export_belongs_to_current_session() is True

    report = BilingualExportReport(
        total_segments=5, paired_segments=3, untranslated_segments=1, preserved_segments=1
    )
    message = TranslationController._format_bilingual_report(report)
    assert "已对照 3 段，1 段未译" in message
    assert "1 段按原布局保留" in message

    controller._export_report_queue = queue.SimpleQueue()
    controller._export_report_queue.put(report)
    assert controller._take_bilingual_report() is report
    assert controller._take_bilingual_report() is None
