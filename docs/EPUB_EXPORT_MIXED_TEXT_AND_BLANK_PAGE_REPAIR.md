# EPUB 导出“原文混入与图片相邻空白页”修复文档

> 适用版本：AI-translater-1.4 / LightNovelTranslator V1.6
>
> 问题现象：导出的译文中夹杂日文原文；文章中间的插图前后出现空白页。
>
> 文档性质：根因分析、修复设计、实施顺序和验收方案。本文不替代修复代码本身。

## 1. 结论摘要

这两个现象不是同一个单点故障，而是导出链路中三个契约没有完全统一造成的：

1. **内联文本没有被完整替换（已确认）**。导入用 `node.get_text()` 收集 `<em>`、`<a>`、`<ruby>`、`<span>` 等后代文本；导出函数 `_replace_text_preserving_inline()` 只替换块节点的直接文本子节点，内联标签中的原文因此残留。
2. **导入和导出的段落计数规则不一致（已确认）**。导出对包含 `<img>` 的块增加了“文本长度小于 2 则跳过”的规则，导入没有同样的规则。图片说明、单字、标点或无障碍文本会导致 `global_line_index` 从该段开始偏移，后续译文被写入错误段落。
3. **图片页的结构和资源路径没有经过导出后校验（高风险）**。导出保留原 EPUB 的 spine、XHTML 和 CSS；当前“空白清理”只删除图片父节点下的空白文本节点，不会删除空的块元素或 `page-break-before/after`。同时旧图片导出器只在 `mapping_dir/images` 查找文件，而 Manga Provider 返回 `translated_images/manga/...`，这会静默跳过图片替换。资源未替换、引用路径不匹配或源 EPUB 自带的分页 CSS，都可能表现为图片页或相邻页空白。

## 2. 导出链路与证据

```text
EPUB
  -> EPUBProcessor.import_epub()
     -> iter_spine_documents(book)
     -> BeautifulSoup + BLOCK_TAGS
     -> content_mapping.json（line_number / chapter_id / block_index / checksum）
  -> 翻译并保存 translated_text
  -> translation_controller.export_epub_file()
     -> save_translations()
     -> image_translation_result.json
  -> infrastructure.exporter.export_epub()
     -> 重新读取原 EPUB
     -> 全局 translations 列表 + global_line_index 替换正文
     -> 添加图片、改写 src、注入 figcaption、写出 EPUB
```

关键实现位置：

| 文件 | 位置 | 证据 |
| --- | --- | --- |
| `src/core/epub_processor.py` | 330-356 | 导入按 spine 和 `node.get_text()` 生成正文映射 |
| `src/infrastructure/segment_extractor.py` | 25-55 | 共享的块标签和叶子节点定义 |
| `src/infrastructure/exporter.py` | 118-160 | 导出构造平行译文列表并使用全局索引 |
| `src/infrastructure/exporter.py` | 241-259 | `_replace_text_preserving_inline()` 仅处理直接文本子节点 |
| `src/infrastructure/image_rewriter.py` | 91-136 | 添加翻译图片并生成 EPUB 内路径 |
| `src/infrastructure/image_rewriter.py` | 142-190 | 改写图片引用 |
| `src/infrastructure/image_translation/manga_provider.py` | 436-470 | Manga 结果保存到 `translated_images/manga` |
| `src/ui/translation_controller.py` | 936-1030 | 导出前同步映射，失败后仍可确认使用旧映射 |
| `src/infrastructure/document_order.py` | 102-174 | spine 文档解析和非线性条目过滤 |

## 3. 根因一：内联标签中的原文残留

### 3.1 当前行为

导入阶段使用 `node.get_text()`，例如：

```html
<p>“这是 <em>华恋</em> 的台词。”</p>
```

映射中的 `original_text` 是完整可见文本：`“这是 华恋 的台词。”`。

导出阶段 `_replace_text_preserving_inline()` 只收集 `node.children` 中的 `NavigableString`。它会替换 `<p>` 的直接文本，但不会触碰 `<em>` 内部的 `NavigableString`。结果可能变成：

```html
<p>这是译文 <em>华恋</em></p>
```

这与截图中“中文译文后仍出现日文片段”的表现一致。现有测试只验证 `<em>` 标签仍存在，没有验证标签内部的原文已被清除，因此该回归没有被拦截。

补充区分：`image_rewriter.inject_figcaption()` 会在启用图片文字注释时主动写入 `[原文: ...]`。这属于显式的图片说明，不是本文所说的正文原文残留；验收扫描应限定在正文块节点，避免把合法注释误报为泄漏。

### 3.2 修复要求

将正文替换定义为“替换块节点的全部可见文本，保留内联标签、属性和层级”。推荐实现顺序：

1. 收集 `node.find_all(string=True, recursive=True)` 的全部文本节点。
2. 保留第一个文本节点所在的内联标签位置。
3. 将完整译文写入第一个文本节点。
4. 删除其余所有文本节点；不得删除 `<em>`、`<strong>`、`<a>`、`<ruby>`、`<span>` 等标签及属性。
5. 对 `<ruby>`、`<rt>`、`<rp>`、脚注链接等语义特殊节点增加白名单测试；如果标签内部文本具有独立语义，应改为按文本节点分配译文，而不是简单全部放入第一个节点。

短期最小修复可以沿用现有标签结构，将所有后代文本清空后把译文放入第一个文本节点；长期应引入“可见文本节点分配器”，按原文字符区间把译文分配到对应内联标签，避免强调范围丢失。

## 4. 根因二：含图片块导致全局索引错位

### 4.1 当前行为

导入端在 `epub_processor.py:347-356` 对所有叶子块执行：

```python
text = (node.get_text() or "").strip()
if text:
    # 写入一个 mapping，并递增全局 line_number
```

导出端在 `exporter.py:147-160` 额外执行：

```python
if node.find("img"):
    text_content = ...
    if not text_content or len(text_content) < 2:
        continue
```

因此以下内容会在导入端占一行、在导出端不占一行：

```html
<p><img src="../image/i001.jpg"/>。</p>
<p><img src="../image/i002.jpg"/><span>图</span></p>
```

一旦发生偏移，后续每一段都会读取错误的 `translations[global_line_index]`。这既会造成译文与原文混排，也会把空译文写入本应有内容的段落。

### 4.2 修复要求

正文段落必须由一个共享定位器生成，导入和导出不得各自维护一套 DOM 筛选逻辑：

- 复用 `segment_extractor.extract_segments_from_document()` 或将其扩展为迭代器；
- 统一 `BLOCK_TAGS`、叶子节点判断、图片容器处理和空文本判断；
- 导出优先按 `chapter_id + block_index + source_checksum` 查找映射；
- 不再用跨章节的平行数组和 `global_line_index` 作为唯一定位；
- 若导出时发现“当前 DOM 段落数量”和 mapping 数量不一致，立即中止并报告章节、索引和原文摘要，禁止继续写出看似成功的 EPUB。

建议的定位结构：

```text
chapter_id = normalize_chapter_id(item.get_name())
block_index = 章节内叶子块序号
source_checksum = compute_source_checksum(可见原文)
locator = chapter_id + "|" + block_index + "|" + source_checksum
```

`line_number` 仍可保留用于 UI 排序，但不能作为 DOM 重放时的唯一锚点。

## 5. 根因三：图片相邻空白页

### 5.1 需要区分的两类空白页

**A. 源 EPUB 已有的结构性空白页**

- 源 XHTML 在 spine 中本身就是图片页或空壳页；
- CSS 含 `page-break-before/after`、`break-before/after` 或 `height: 100vh`；
- 图片用“前后各占一页”的版式排版，导出保留原结构后，阅读器会显示图片页前后的空白页。

**B. 导出产生的空白页或空白图片页**

- 图片引用改写失败，页面只剩空容器；
- 添加的翻译图片未找到本地文件，`add_translated_images()` 直接 `continue`；
- `file_name` 保留了错误的 `OEBPS/` 或 `EPUB/` 前缀，manifest 与 XHTML 相对路径不一致；
- 新图片 `uid` 仅由文件 stem 生成，不同目录同名结果可能产生重复 UID；
- 图片父级存在空 `<p>`、空 `<div>` 或 CSS 强制分页，当前清理逻辑只删除空白字符，不会清理这些结构。

### 5.2 当前代码中的具体风险

1. `exporter.py:182-185` 仅删除 `<img>` 父节点下的空白字符串，不能删除空块或分页 CSS。
2. `image_rewriter.py:114-124` 直接把 `orig_path.parent / new_filename` 写入 EPUB，未统一去除容器前缀、未校验路径是否与原引用同一资源根。
3. `image_rewriter.py:126` 使用 `img_{Path(new_filename).stem}` 生成 UID，无法保证全书唯一。
4. `exporter.py:107-114` 假设翻译图片位于 `mapping_dir/images`；Manga Provider 在 `manga_provider.py:195` 以后保存到 `mapping_dir/translated_images/manga`，两者契约不一致。
5. `add_translated_images()` 找不到文件时只打印警告，导出仍继续并最终显示成功，导致“图片页空白”难以追踪。

### 5.3 修复设计

#### 5.3.1 统一图片结果契约

`image_translation_result.json` 中的值必须统一为“相对于 mapping_dir 的文件路径”，例如：

```json
{
  "result_map": {
    "image/i001.jpg": "translated_images/manga/i001_<hash>.png"
  }
}
```

导出器应使用 `mapping_dir / relative_result_path` 读取文件，不再固定拼接 `mapping_dir/images`。旧版 `images/<filename>` 继续作为兼容路径，但应记录迁移日志。

#### 5.3.2 规范化 EPUB 内资源路径

- 使用 POSIX 路径处理 EPUB 内部路径；
- 去掉 `OEBPS/`、`EPUB/`、`OPS/` 容器前缀后再计算相对路径；
- 对 `src` 先拆除 URL fragment 和 query，再进行匹配；
- 新资源 UID 使用原始 EPUB 路径哈希，确保全书唯一；
- `path_mapping` 写入前检查新资源确实存在于 book manifest；
- 任何一张映射图片缺失都应进入 `failed_images`，不能静默导出成功。

#### 5.3.3 只清理导出器生成的空结构

不要全局删除空页。应在每个 spine 文档处理后：

1. 判断 body 是否只有空白、空块和图片引用；
2. 对图片容器执行“空白文本清理 + 空块折叠”；
3. 仅当图片已经成功绑定到 manifest 且容器没有可见文本时，移除无意义的空 `<p>/<div>`；
4. 对源文件已有的 `page-break-*` 保留并记录诊断，不要默认删除；
5. 若产品要求图片紧跟正文，则由显式的导出策略移除图片容器上的 `page-break-before/after`，并增加开关和回归测试。

## 6. 旧映射和失败状态风险

`translation_controller.py:948-965` 在 `save_translations()` 失败后允许用户确认继续导出旧 mapping。该行为本身是兼容策略，但会造成表格内容与 EPUB 内容不一致，尤其在用户刚修改过图片附近段落时更难判断问题来源。

修复建议：

- 默认停止导出；
- 只有用户明确确认“使用旧映射”才允许继续；
- 输出文件名和日志中记录 `mapping_source=current|stale`；
- 导出前显示 mapping 的更新时间、源 EPUB 指纹和译文行数；
- `image_translation_result.json` 必须携带源 EPUB 指纹，源文件变化时拒绝复用旧图片结果。

## 7. 实施顺序

### P0：先修正文和图片定位

1. 把导入、导出统一到同一个段落定位器。
2. 用 locator 替代 `global_line_index` 重放正文。
3. 修复 `_replace_text_preserving_inline()`，清除所有后代原文文本节点。
4. 增加 mapping 数量与 DOM 段落数量不一致时的硬失败。

### P1：修复图片资源和分页

1. 统一 `result_map` 的相对路径契约，兼容旧 `images/` 路径。
2. 统一 EPUB 内路径根和 UID 生成；处理 query/fragment。
3. 为每张图片输出“原路径、结果路径、manifest UID、引用文档、是否成功替换”的诊断记录。
4. 对空图片页、空块、分页 CSS 做结构化检测；只按策略移除生成的空结构。

### P2：收紧导出成功条件

1. 源 EPUB 指纹、mapping 指纹和图片 manifest 指纹全部匹配后才允许导出。
2. 任何图片资源缺失、正文段落未对齐、EPUB manifest 引用无效时，导出状态为失败。
3. 导出成功后重新读取输出 EPUB，执行结构校验和文本残留扫描。

## 8. 必须新增的回归测试

### 8.1 原文残留

- `<p>前缀 <em>日文</em> 后缀</p>` 替换后，输出中不存在原文“日文”；
- `<a>`、`<strong>`、`<ruby><rb>…</rb><rt>…</rt></ruby>` 保留标签和属性；
- 一个块含多个内联文本节点时，输出可见文本与译文完全一致。

### 8.2 图片容器索引

- 图片后带一个字符标题；
- 图片后带标点；
- 图片容器含 `alt`、`title`、`figcaption`；
- 图片前后各有正文，验证后续每个 locator 仍对应正确段落；
- 导入和导出使用同一段落计数，mapping 数量与重放数量一致。

### 8.3 图片资源

- Manga Provider 的 `translated_images/manga/...` 结果可以被导出器读取；
- 旧版 `images/<filename>` 结果仍可读取；
- 同名图片、不同目录图片不会 UID 冲突或互换；
- `../image/a.jpg#fragment` 和 `../image/a.jpg?x=1` 可以正确匹配；
- 结果文件缺失时导出失败，并指出原始图片路径。

### 8.4 空白页和分页

- 图片嵌入正文中间，前后正文均存在，输出没有新增空 XHTML 文档；
- 源文件带 `page-break-before/after` 时，诊断结果明确标记为“源结构分页”；
- 仅含图片的 spine 文档可正常显示图片，不被误当作正文段落；
- 空 body、空 `<p>`、空 `<div>`、无效图片引用均被检测；
- 导出后 EPUB 可被 EPUBCheck 或等价结构检查器打开，所有 spine/manifest/href 引用有效。

## 9. 手工诊断流程

在修复代码前，先对“原 EPUB”和“导出 EPUB”分别执行：

1. 解压 EPUB，列出 `META-INF/container.xml`、OPF、spine 和 manifest。
2. 对每个 spine XHTML 统计：可见文本长度、`img` 数量、空块数量、分页 CSS 数量。
3. 建立 `src -> manifest item -> 新 src` 映射，检查是否存在 `OEBPS/` 重复前缀、绝对路径、query/fragment 或大小写差异。
4. 扫描导出 XHTML：
   - 是否仍包含已翻译段落的原文 checksum 对应文本；
   - 是否存在空 body 或只有空块的 spine 文档；
   - 是否存在指向不存在资源的 `img/@src`、SVG `image/@href`。
5. 在阅读器中定位图片页前后两页，记录对应 XHTML 文件名，而不是只记录阅读器页码；阅读器页码会因字体和分页 CSS 变化。

建议把上述结果写入 `logs/epub_export_diagnostics.json`，至少包含 `source_hash`、`output_hash`、`chapter_id`、`block_index`、`image_path`、`page_break_rules` 和 `errors`。

## 10. 验收标准

- 导出的正文可见文本不再出现“译文 + 同一段原文”的混排；
- 含图片的块不会改变前后正文的映射顺序；
- 图片结果无论由 Manga 还是 AI Provider 生成，都能被导出器正确读取；
- 图片页不因资源路径、UID 或空结构产生新增空白页；
- 源 EPUB 明确要求的扉页/隔页仍保留，导出器不会擅自改变书籍版式；
- 任一结构错误都会使导出失败并给出可定位错误，而不是显示“导出成功”；
- 新增测试在干净的 Python 3.11 环境、项目依赖安装完成后全部通过。

## 11. 二次修复：开头插图新增空白页（2026-07-17）

### 11.1 回归原因

上一轮修复把图片文字注释移动到 `src` 改写之前，使图片路径能够按原始 EPUB
坐标正确匹配。这修复了注释串图，却同时让过去匹配不到的独立插图页开始注入
`figcaption`。真实样本的开头 `part0002.xhtml` 到 `part0006.xhtml` 是
`body.class-0` 的满页图片文档，正文可见文字为零。新增说明节点后，固定高度内容发生
溢出，分页阅读器会将溢出区域排成额外空白页。

另一个独立问题是 `add_translated_images()` 已改为返回
`(path_mapping, failed_count)`，但导出器仍把整个二元组当作路径字典。图片引用改写
因此异常，旧代码又在文档级捕获异常并继续写出，导致界面显示成功而 EPUB 内容不完整。

### 11.2 实施修复

1. 图片文字注入前检测当前 `body`；只要页面含 `img`/SVG `image` 且无可见文字，
   就完整跳过 `figcaption` 注入，保持满页插图 DOM 不增高。可见文字判断忽略
   SVG `title`/`desc`、脚本/样式、零宽字符和显式隐藏的辅助文本，避免误判固定版式插图页。
2. 删除会无条件改写图片父节点空白文本的清理步骤。空白字符属于源 XHTML 结构，
   不应在没有证据时修改固定版式页面。
3. 导出器正确解包 `add_translated_images()` 的返回值，失败图片明确记录并保留原图。
4. 正文替换改为共享块规则；新 mapping 使用
   `chapter_id + block_index + source_checksum` 校验，旧 mapping 按章节和原文顺序校验。
5. 历史 `Text/partXXXX.xhtml` 与当前 `partXXXX.xhtml` 仅允许唯一后缀匹配；
   匹配不唯一或有效译文无法定位时中止导出。
6. 文本替换清除块内全部后代文本节点，保留内联标签和属性，防止译文后残留原文。

### 11.3 验证结果

- 执行 `pytest tests/test_epub_export_repair.py tests/test_epub_infrastructure.py -q`：
  85 项全部通过。
- 真实第 1 卷源 EPUB 临时导出成功，源和输出的 spine 数均为 52。
- 输出中识别到 29 个纯图片页；每页均为 1 张图片、0 可见文字、0 个
  `figcaption`，没有增加空白 XHTML 或说明节点。
- 损坏图片、宽高比异常图片继续回退原图；测试夹具改用 Pillow 生成的有效 PNG，
  不降低生产图片完整性校验。

## 12. 三次修复：保留原始 XHTML 外壳（2026-07-17）

对实际导出的 `負けヒロインが多すぎる！ (ガガガ文庫)_译文.epub` 解包对比后确认，
文件没有新增 spine 或空 XHTML。真正的空白页原因是 `ebooklib.EpubHtml.get_content()`
会通过章节模板重建已读取的文档，导致原始 `<head>`、stylesheet link、`body` class、
`xml:lang` 和 SVG 大小写敏感属性丢失。满页插图因此失去 `width: 100%` 和
`page-break-inside: avoid`，按 1343×1920 固有尺寸溢出分页。

修复后导出器从 `item.content` 读取原始 XHTML，并让 ebooklib 原样写回完整文档。
为兼容历史 mapping，HTML DOM 继续负责段落定位和原文校验，XML DOM 负责实际替换与
序列化；两套 DOM 的块数量不一致时中止导出。回归测试覆盖 stylesheet、body class、
语言属性以及 SVG `viewBox` / `preserveAspectRatio` 的保留。

## 13. 四次修复：撤销普通插图页的全高 SVG 改写（2026-07-17）

再次检查用户 13:02 导出的真实 EPUB 后确认，52 个 spine 项和 52 个
`rendition:spread-none` 均存在，但 28 个原本使用普通 `<img width="100%">` 的插图页
被导出器改写成了 `svg width="100%" height="100%"`，同时在 `html/body` 上加入
`height:100%`。百分比高度在部分纵排 EPUB 阅读器中仍参与流式分页，可能形成插图页
前后的溢出空页；这也是源书没有而导出书新增的结构差异。

本次修复不再重构独立插图页，只保留出版社原始 XHTML、body class 和 CSS。源书本来
就是 SVG 的封面继续原样保留，普通插图不会新增 SVG manifest 属性。另将译文书标识从
固定的源文件哈希改为“源哈希前缀 + 每次导出的随机 UUID”，避免阅读器把新文件识别为
同一本旧书并复用分页缓存。

真实样本验证结果：spine 52，`rendition:spread-none` 52，纯图片页 29；其中 SVG 仅 1
个（源书封面），全高插图页 0，manifest SVG 项 1。针对性测试 89 项全部通过；新增的
普通插图端到端回归测试要求 `<img>` body 原样保留，且禁止出现导出器新增的 SVG、
`height:100%`、`overflow:hidden` 和 `properties="svg"`。
