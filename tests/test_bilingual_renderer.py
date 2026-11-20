#!/usr/bin/env python3
"""双语 EPUB DOM 渲染器单元测试。"""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.domain.errors import BilingualEpubExportError
from src.infrastructure.bilingual_renderer import (
    HEADING_TARGET_CLASS,
    INLINE_TARGET_CLASS,
    PAIR_ATTRIBUTE,
    SOURCE_CLASS,
    TARGET_CLASS,
    render_bilingual_document,
)
from src.infrastructure.segment_extractor import BLOCK_TAGS, is_leaf_block

CSS_NAME = "Styles/ait-bilingual.css"


def _soup(body: str, *, lang: str = "ja") -> BeautifulSoup:
    html = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{lang}">'
        "<head><title>t</title></head>"
        f"<body>{body}</body></html>"
    )
    return BeautifulSoup(html, "xml")


def _blocks(soup: BeautifulSoup) -> list:
    return [
        node
        for node in soup.find_all(True)
        if node.name in BLOCK_TAGS and is_leaf_block(node) and bool((node.get_text() or "").strip())
    ]


def _record(line: int, text: str) -> dict:
    return {"line_number": line, "translated_text": text}


def _render(soup, records, *, doc_name="Text/ch1.xhtml", language_tag="zh"):
    return render_bilingual_document(
        soup,
        doc_name,
        records,
        _blocks(soup),
        language_tag=language_tag,
        stylesheet_name=CSS_NAME,
    )


def _targets(soup) -> list:
    return [node for node in soup.find_all(True) if TARGET_CLASS in (node.get("class") or [])]


def test_paragraph_keeps_inline_markup_and_inserts_sibling_target():
    soup = _soup('<p id="p17" class="body-text">彼は<strong>静かに</strong>笑った。</p>')
    plan = _render(soup, [_record(17, "他静静地笑了。")])

    source = soup.find("p", {"id": "p17"})
    assert SOURCE_CLASS in source.get("class", [])
    assert source.get(PAIR_ATTRIBUTE) == "line_000017"
    assert source.strong is not None and source.strong.get_text() == "静かに"

    target = source.find_next_sibling("p")
    assert target is not None
    assert target.get_text() == "他静静地笑了。"
    assert target.get(PAIR_ATTRIBUTE) == "line_000017"
    assert target.get("lang") == "zh"
    assert target.get("xml:lang") == "zh"
    assert plan.targets[0].text == "他静静地笑了。"


def test_heading_inserts_plain_paragraph_target_without_copying_id():
    soup = _soup('<h2 id="h2-1">第一章</h2>')
    _render(soup, [_record(1, "第一章（译文）")])

    heading = soup.find("h2", {"id": "h2-1"})
    target = heading.find_next_sibling("p")
    assert target is not None
    assert target.name == "p"
    assert target.get("id") is None
    assert HEADING_TARGET_CLASS in target.get("class", [])


def test_list_item_appends_inline_target_without_second_li():
    soup = _soup("<ul><li>項目一</li></ul>")
    _render(soup, [_record(1, "项目一")])

    items = soup.find_all("li")
    assert len(items) == 1
    target = items[0].find("span")
    assert target is not None
    assert TARGET_CLASS in target.get("class", [])
    assert INLINE_TARGET_CLASS in target.get("class", [])
    assert target.get_text() == "项目一"


def test_blockquote_appends_paragraph_inside_quote():
    soup = _soup("<blockquote>引用内容</blockquote>")
    _render(soup, [_record(1, "引用内容译文")])

    quote = soup.find("blockquote")
    target = quote.find("p")
    assert target is not None
    assert TARGET_CLASS in target.get("class", [])
    assert target.get_text() == "引用内容译文"


def test_caption_and_figcaption_append_inline_target():
    soup = _soup(
        "<table><caption>表題</caption><tr><td>x</td></tr></table>"
        '<figure><img src="pic.png"/><figcaption>図の説明</figcaption></figure>'
    )
    _render(soup, [_record(1, "表格标题"), _record(2, "插图说明")])

    captions = soup.find_all(["caption", "figcaption"])
    assert len(captions) == 2
    for caption in captions:
        spans = [
            child for child in caption.find_all("span") if TARGET_CLASS in child.get("class", [])
        ]
        assert len(spans) == 1


def test_paragraph_in_paragraph_falls_back_to_inline_span():
    soup = _soup('<div><p id="outer"><p id="inner">嵌套文本</p></p></div>')
    plan = _render(soup, [_record(1, "嵌套译文")])

    inner = soup.find("p", {"id": "inner"})
    assert inner.find_next_sibling("p") is None
    target = inner.find("span")
    assert target is not None
    assert TARGET_CLASS in target.get("class", [])
    assert INLINE_TARGET_CLASS in target.get("class", [])
    assert plan.targets[0].source_text == "嵌套文本"


def test_identical_source_and_target_still_creates_two_nodes():
    soup = _soup("<p>同じ</p>")
    plan = _render(soup, [_record(1, "同じ")])

    assert len(_targets(soup)) == 1
    assert plan.targets[0].text == "同じ"


def test_empty_translation_keeps_original_only():
    soup = _soup("<p>原文一</p><p>原文二</p>")
    plan = _render(soup, [_record(1, "  "), _record(2, "译文二")])

    assert len(_targets(soup)) == 1
    assert plan.untranslated_pair_ids == ("line_000001",)
    assert plan.targets[0].pair_id == "line_000002"


def test_multiline_translation_uses_br_text_nodes():
    soup = _soup("<p>原文</p>")
    _render(soup, [_record(1, "第一行\n第二行")])

    target = _targets(soup)[0]
    assert target.find_all("br")
    assert target.get_text() == "第一行第二行"
    assert list(target.stripped_strings) == ["第一行", "第二行"]


def test_special_characters_are_inserted_as_text_not_markup():
    soup = _soup("<p>原文</p>")
    payload = "<script>alert('x')</script> & <b>bold</b>"
    _render(soup, [_record(1, payload)])

    target = _targets(soup)[0]
    assert target.find("script") is None
    assert target.find("b") is None
    assert target.get_text() == payload


def test_footnote_anchor_and_ids_are_preserved_on_source_node():
    soup = _soup('<p id="p1">本文<a href="#fn1" id="ref1">1</a></p>')
    plan = _render(soup, [_record(1, "正文")])

    source = soup.find("p", {"id": "p1"})
    assert source.find("a").get("href") == "#fn1"
    assert source.find("a").get("id") == "ref1"
    assert "p1" in plan.original_ids


def test_ruby_and_emphasis_subtrees_are_preserved_verbatim():
    soup = _soup("<p><ruby><rb>漢字</rb><rt>かんじ</rt></ruby><em>強調</em></p>")
    _render(soup, [_record(1, "汉字")])

    assert soup.find("ruby") is not None
    assert soup.find("rt").get_text() == "かんじ"
    assert soup.find("em").get_text() == "強調"


def test_images_in_source_paragraph_are_not_duplicated():
    soup = _soup('<p>插圖<img src="pic.png"/></p>')
    _render(soup, [_record(1, "插图")])

    assert len(soup.find_all("img")) == 1
    target = _targets(soup)[0]
    assert target.find("img") is None


def test_image_only_page_is_preserved_without_new_text_nodes():
    soup = _soup('<img src="pic.png"/><h2 class="hidden">Running title</h2>')
    plan = _render(soup, [_record(1, "译题")])

    assert plan.preserved_pair_ids == ("line_000001",)
    assert not _targets(soup)
    assert soup.find("img").find_next_sibling("p") is None
    assert soup.find("link") is None


def test_document_with_only_hidden_segments_gets_no_stylesheet():
    soup = _soup('<p class="visually-hidden">仅隐藏标题</p>')
    plan = _render(soup, [_record(1, "隐藏译文")])

    assert plan.preserved_pair_ids == ("line_000001",)
    assert not _targets(soup)
    assert soup.find("link") is None


def test_hidden_nodes_are_preserved():
    soup = _soup('<p>可见</p><p style="display:none">隐藏</p>')
    plan = _render(soup, [_record(1, "可见译文"), _record(2, "隐藏译文")])

    assert plan.preserved_pair_ids == ("line_000002",)
    assert len(_targets(soup)) == 1


def test_hidden_node_without_translation_is_still_preserved():
    soup = _soup('<p>可见</p><p aria-hidden="true">隐藏</p>')
    plan = _render(soup, [_record(1, "可见译文"), _record(2, "")])

    assert plan.preserved_pair_ids == ("line_000002",)
    assert plan.untranslated_pair_ids == ()


def test_stylesheet_link_is_added_only_to_documents_with_targets():
    soup = _soup("<p>原文</p>")
    _render(soup, [_record(1, "")])
    assert soup.find("link") is None

    soup_with_target = _soup("<p>原文</p>")
    _render(soup_with_target, [_record(1, "译文")])
    link = soup_with_target.find("link")
    assert link is not None
    assert link.get("href") == "../Styles/ait-bilingual.css"


def test_relative_stylesheet_path_for_nested_document():
    soup = _soup("<p>原文</p>")
    _render(soup, [_record(1, "译文")], doc_name="Text/part/ch1.xhtml")
    assert soup.find("link").get("href") == "../../Styles/ait-bilingual.css"


def test_source_document_with_existing_markers_is_rejected():
    soup = _soup('<p class="ait-bilingual-source">旧导出</p>')
    try:
        _render(soup, [_record(1, "译文")])
    except BilingualEpubExportError as exc:
        assert "已包含双语导出标记" in str(exc)
    else:
        raise AssertionError("应拒绝已包含双语标记的源文档")
