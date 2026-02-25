#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EPUB解析与映射生成模块
负责：
- 解析EPUB，提取格式信息、内容段落、图片（Base64）
- 生成mapping目录及content_mapping.json、images.json、format_info.json
- 提供译文更新与装载辅助函数
"""

from pathlib import Path
from typing import Dict, List, Tuple
import json
import base64
import datetime
import logging
from ..utils.logger import get_logger


logger = get_logger(__name__)


class EPUBProcessor:
    BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "caption", "figcaption"}

    @staticmethod
    def _normalize_chapter_id(name: str) -> str:
        """规范化章节ID，统一不同目录结构为稳定键：优先返回"Text/<filename>"。
        
        修复说明：确保始终返回"Text/"前缀的章节ID，保持spine_order与content_mappings一致。
        """
        if not name:
            return name
        
        # 统一路径分隔符
        n = name.replace("\\", "/")
        low = n.lower()
        
        # 移除常见的EPUB容器前缀（OEBPS/, EPUB/, OPS/等）
        for prefix in ["oebps/", "epub/", "ops/"]:
            if low.startswith(prefix):
                n = n[len(prefix):]
                low = n.lower()
                break
        
        # 如果已经是Text/开头，直接返回
        if low.startswith("text/"):
            return n
        
        # 如果包含/text/路径，提取Text/部分
        idx = low.rfind("/text/")
        if idx != -1:
            return n[idx+1:]  # 返回从Text/开始的部分
        
        # 处理路径形式：某目录/Text/文件名
        parts = n.split("/")
        if len(parts) >= 2:
            for i, part in enumerate(parts[:-1]):
                if part.lower() == "text":
                    # 返回Text/文件名
                    return "/".join(parts[i:])
        
        # 默认：在文件名前加Text/前缀（确保一致性）
        filename = parts[-1] if parts else n
        return f"Text/{filename}"

    @staticmethod
    def _get_item_media_type(item) -> str:
        """安全地获取EpubItem的media_type，兼容不同版本的ebooklib"""
        if hasattr(item, 'get_media_type'):
            try:
                return item.get_media_type()
            except:
                pass
        if hasattr(item, 'media_type'):
            return item.media_type
        # 尝试从file_name扩展名推断
        if hasattr(item, 'file_name'):
            fname = item.file_name.lower()
            if fname.endswith(('.html', '.xhtml', '.htm')):
                return 'application/xhtml+xml'
            elif fname.endswith(('.css',)):
                return 'text/css'
            elif fname.endswith(('.jpg', '.jpeg')):
                return 'image/jpeg'
            elif fname.endswith(('.png',)):
                return 'image/png'
            elif fname.endswith(('.gif',)):
                return 'image/gif'
            elif fname.endswith(('.svg',)):
                return 'image/svg+xml'
            elif fname.endswith(('.ncx',)):
                return 'application/x-dtbncx+xml'
        return ''

    @staticmethod
    def _get_item_name(item) -> str:
        """安全地获取EpubItem的文件名"""
        if hasattr(item, 'file_name'):
            return item.file_name
        if hasattr(item, 'href'):
            return item.href
        if hasattr(item, 'get_name'):
            try:
                return item.get_name()
            except:
                pass
        return ''

    def import_epub(self, epub_path: str, extract_images: bool = True) -> Dict[str, str]:
        """解析EPUB并生成mapping目录与三个映射文件。

        返回：{"mapping_dir": str, "content_file": str, "images_file": str, "format_file": str}
        """
        try:
            from ebooklib import epub
            import ebooklib
            from bs4 import BeautifulSoup
        except ImportError:
            raise Exception("需要安装ebooklib和beautifulsoup4库来支持EPUB文件解析")

        epub_path = Path(epub_path)
        if not epub_path.exists() or epub_path.suffix.lower() != ".epub":
            raise Exception("文件不存在或不是EPUB格式")

        # 读取书籍
        book = epub.read_epub(str(epub_path))

        # 为每个EPUB创建独立的映射子文件夹
        # 子文件夹命名规则：workspace/mappings/<epub文件名（去除扩展名）>
        # 修改：不再在源文件目录下创建，而是在项目根目录的workspace下创建
        epub_name = epub_path.stem  # 获取不带扩展名的文件名
        
        # 获取当前运行目录作为项目根目录
        project_root = Path.cwd()
        mapping_root = project_root / "workspace" / "mappings"
        mapping_dir = mapping_root / epub_name
        mapping_dir.mkdir(parents=True, exist_ok=True)

        # 数据容器
        content_mappings: Dict[str, Dict] = {}
        images_mapping: Dict[str, Dict] = {}
        format_info: Dict[str, Dict] = {
            "metadata": {},
            "css_styles": {},
            "spine_order": [],
            "toc_structure": [],
            "manifest_items": {}
        }

        # 元数据
        try:
            # 常见DC元数据
            md = {}
            for tag in ["title", "creator", "language", "identifier", "publisher", "date", "description"]:
                vals = book.get_metadata("DC", tag)
                if vals:
                    # 取第一个值
                    md[tag] = vals[0][0]
            format_info["metadata"] = md
        except Exception:
            pass

        # 清点manifest与spine
        try:
            # manifest
            for item in book.get_items():
                try:
                    name = self._get_item_name(item)
                    if name:
                        format_info["manifest_items"][name] = {
                            "media_type": self._get_item_media_type(item),
                            "properties": getattr(item, "properties", None)
                        }
                except Exception as e:
                    print(f"⚠ 警告：解析manifest条目失败: {e}")
                    continue
        except Exception as e:
            print(f"⚠ 警告：提取manifest时发生错误: {e}")
        
        # spine（顺序）- 健壮的多版本兼容处理（单独try以确保即使manifest失败也能提取spine）
        try:
            spine_order = []
            for item in book.spine:
                try:
                    # 提取itemref（处理不同版本的spine格式）
                    if isinstance(item, tuple):
                        # 新版本：(itemref, linear)
                        itemref = item[0] if len(item) > 0 else None
                    else:
                        # 旧版本：直接是itemref
                        itemref = item
                    
                    if not itemref:
                        continue
                    
                    # 获取文件名（尝试多种属性）
                    name = self._get_item_name(itemref)
                    if not name and isinstance(itemref, str):
                        name = itemref
                    
                    # 如果是对象，尝试从book.items中查找
                    if not name and hasattr(itemref, 'idref'):
                        try:
                            item_obj = book.get_item_with_id(itemref.idref)
                            if item_obj:
                                name = self._get_item_name(item_obj)
                        except:
                            pass
                    
                    if name:
                        normalized_name = self._normalize_chapter_id(name)
                        spine_order.append(normalized_name)
                except Exception:
                    # 单个spine条目失败不影响其他条目的提取
                    continue
            
            # 【关键修复】只有在spine_order非空时才赋值，否则发出警告
            if spine_order:
                format_info["spine_order"] = spine_order
            else:
                # 如果spine提取失败，至少记录警告，并尝试从content_mappings推断
                print("⚠ 警告：无法从EPUB提取spine_order，将从文档内容推断章节顺序")
                format_info["spine_order"] = []  # 保持为空列表，后续会从mappings推断
        except Exception as e:
            # spine提取异常也要记录，不能静默失败
            print(f"⚠ 警告：提取spine_order时发生错误: {e}")
            format_info["spine_order"] = []

        # TOC结构（简化保存：标题、href、层级）
        try:
            def _flatten_toc(toc, level=1):
                for entry in toc:
                    try:
                        title = entry.title if hasattr(entry, "title") else str(entry)
                        href = entry.href if hasattr(entry, "href") else None
                        format_info["toc_structure"].append({"title": title, "href": href, "level": level})
                        if hasattr(entry, "children") and entry.children:
                            _flatten_toc(entry.children, level + 1)
                    except Exception:
                        continue
            _flatten_toc(book.toc, 1)
        except Exception:
            pass

        # 提取CSS样式
        try:
            import ebooklib
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_STYLE:
                    name = self._get_item_name(item)
                    if name:
                        try:
                            format_info["css_styles"][name] = item.get_content().decode("utf-8", errors="ignore")
                        except Exception:
                            format_info["css_styles"][name] = base64.b64encode(item.get_content()).decode("ascii")
        except Exception:
            pass

        # 【关键修复】检查是否已存在旧的翻译数据，以便保留翻译进度
        existing_translations = {}
        content_file = mapping_dir / "content_mapping.json"
        if content_file.exists():
            try:
                old_data = json.loads(content_file.read_text(encoding="utf-8"))
                old_mappings = old_data.get("content_mappings", {})
                # 按原文构建翻译缓存（用于匹配）
                for key, item in old_mappings.items():
                    original = item.get("original_text", "")
                    translated = item.get("translated_text", "")
                    translated_at = item.get("translated_at", "")
                    if original and translated:  # 只保留已翻译的内容
                        existing_translations[original] = {
                            "translated_text": translated,
                            "translated_at": translated_at
                        }
                print(f"✓ 检测到已有翻译数据，已保留 {len(existing_translations)} 条翻译记录")
            except Exception as e:
                print(f"⚠ 警告：读取旧翻译数据失败: {e}")
                existing_translations = {}
        
        # 提取文档内容为段落映射（严格按全局行号顺序）
        # 【关键重构】使用全局行号（global_line_number）代曾spine+sequence_order
        global_line_number = 1  # 全局行号，从1开始
        
        # 按spine顺序遍历所有文档
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                try:
                    html = item.get_content().decode("utf-8", errors="ignore")
                    soup = BeautifulSoup(html, "html.parser")
                    base_name = self._normalize_chapter_id(self._get_item_name(item))
                    
                    # 按文档真实顺序遍历所有节点，筛选块级标签
                    for node in soup.find_all(True):
                        try:
                            if node.name in self.BLOCK_TAGS:
                                # 【关键修复】检查是否为叶子块节点（避免重复提取嵌套内容）
                                has_block_children = any(child.name in self.BLOCK_TAGS for child in node.find_all(True, recursive=False))
                                if has_block_children:
                                    continue
                                
                                # 提取文本（递归获取所有文本，因为此时确认没有块级子标签）
                                text = (node.get_text() or "").strip()
                                if text:
                                    # 使用全局行号作为键（采用6位数字填充）
                                    cid = f"line_{global_line_number:06d}"
                                    
                                    # 【关键修复】检查是否有已存在的翻译
                                    translated_text = ""
                                    translated_at = ""
                                    if text in existing_translations:
                                        translated_text = existing_translations[text]["translated_text"]
                                        translated_at = existing_translations[text]["translated_at"]
                                    
                                    content_mappings[cid] = {
                                        "original_text": text,
                                        "translated_text": translated_text,  # 保留已有翻译
                                        "line_number": global_line_number,
                                        "chapter_id": base_name,
                                        "translated_at": translated_at  # 保留翻译时间戳
                                    }
                                    global_line_number += 1
                        except Exception:
                            continue
                except Exception:
                    continue

        # 图片Base64映射
        if extract_images:
            try:
                import ebooklib
                logger.debug(f"[import_epub] ====== 开始提取EPUB图片 ======")
                print(f"📷 开始提取EPUB图片...")
                image_count = 0
                
                total_items = 0
                for item in book.get_items():
                    total_items += 1
                logger.debug(f"[import_epub] EPUB共有 {total_items} 个item")
                
                for idx, item in enumerate(book.get_items()):
                    try:
                        item_type = item.get_type()
                        media_type = self._get_item_media_type(item)
                        item_name = self._get_item_name(item)
                        
                        logger.debug(f"[import_epub] 检查item {idx+1}/{total_items}: name={item_name}, type={item_type}, media_type={media_type}")
                        
                        # 检查是否是图片
                        is_image = False
                        if item_type == ebooklib.ITEM_IMAGE:
                            logger.debug(f"[import_epub] item_type == ebooklib.ITEM_IMAGE，判定为图片")
                            is_image = True
                        elif media_type and media_type.startswith('image/'):
                            logger.debug(f"[import_epub] media_type.startswith('image/')，判定为图片")
                            is_image = True
                        
                        if is_image:
                            name = self._get_item_name(item)
                            if not name:
                                logger.warning(f"[import_epub] 图片item缺少name，跳过")
                                continue
                            data = item.get_content()
                            mime = media_type or "image/png"
                            b64 = base64.b64encode(data).decode("ascii")
                            
                            logger.debug(f"[import_epub] 图片信息: name={name}, size={len(data)} bytes, mime={mime}, base64_length={len(b64)}")
                            
                            images_mapping[name] = {
                                "original_path": name,
                                "base64_data": f"data:{mime};base64,{b64}",
                                "mime_type": mime,
                                "file_size": len(data)
                            }
                            image_count += 1
                            print(f"  ✓ 提取图片: {name} ({len(data)} bytes)")
                        else:
                            logger.debug(f"[import_epub] 非图片item，跳过: name={item_name}")
                    except Exception as e:
                        logger.error(f"[import_epub] 提取单个图片失败: {type(e).__name__}: {e}", exc_info=True)
                        print(f"⚠ 警告：提取单个图片失败: {e}")
                        continue
                
                logger.debug(f"[import_epub] ====== 图片提取完成 ======")
                logger.debug(f"[import_epub] 成功提取 {image_count} 张图片")
                logger.debug(f"[import_epub] images_mapping包含 {len(images_mapping)} 个条目")
                print(f"✅ 图片提取完成，共 {image_count} 张图片")
            except Exception as e:
                logger.error(f"[import_epub] 提取图片时发生错误: {type(e).__name__}: {e}", exc_info=True)
                print(f"⚠ 警告：提取图片时发生错误: {e}")

        # 写入文件
        logger.debug(f"[import_epub] ====== 开始写入文件 ======")
        content_file = mapping_dir / "content_mapping.json"
        images_file = mapping_dir / "images.json"
        format_file = mapping_dir / "format_info.json"
        logger.debug(f"[import_epub] 输出文件:")
        logger.debug(f"[import_epub]   - content_file: {content_file}")
        logger.debug(f"[import_epub]   - images_file: {images_file}")
        logger.debug(f"[import_epub]   - format_file: {format_file}")

        project_info = {
            "project_id": f"epub_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "original_file": str(epub_path),
            "created_at": datetime.datetime.now().isoformat(),
            "updated_at": datetime.datetime.now().isoformat()
        }

        content_payload = {
            "project_info": project_info,
            "content_mappings": content_mappings
        }
        logger.debug(f"[import_epub] content_payload: {len(content_mappings)} 个content_mappings")

        images_payload = {
            "image_mappings": images_mapping
        }
        logger.debug(f"[import_epub] images_payload: {len(images_mapping)} 个image_mappings")
        
        # 【关键修复】如果spine_order为空，从content_mappings推断章节顺序
        if not format_info.get("spine_order"):
            logger.warning("[import_epub] spine_order为空，正在从content_mappings推断章节顺序...")
            print("⚠ spine_order为空，正在从content_mappings推断章节顺序...")
            # 提取所有章节ID并排序
            chapters = set()
            for item in content_mappings.values():
                chapter = item.get("chapter_id", "")
                if chapter:
                    chapters.add(chapter)
            
            # 按文件名自然顺序排序（通常与p-0001, p-0002...的命名规则匹配）
            inferred_spine = sorted(chapters)
            format_info["spine_order"] = inferred_spine
            logger.debug(f"[import_epub] 已推断 {len(inferred_spine)} 个章节: {inferred_spine}")
            print(f"✓ 已推断 {len(inferred_spine)} 个章节（按文件名排序）")
            print("⚠ 建议：使用 tools/fix_spine_order.py 从原EPUB提取精确的spine顺序")

        # 保存JSON
        logger.debug(f"[import_epub] 保存content_mapping.json...")
        content_file.write_text(json.dumps(content_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug(f"[import_epub] 保存images.json...")
        images_file.write_text(json.dumps(images_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug(f"[import_epub] 保存format_info.json...")
        format_file.write_text(json.dumps(format_info, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug(f"[import_epub] ====== 所有文件保存完成 ======")

        result = {
            "mapping_dir": str(mapping_dir),
            "content_file": str(content_file),
            "images_file": str(images_file),
            "format_file": str(format_file)
        }
        logger.debug(f"[import_epub] 返回结果: {result}")
        return result

    def load_content_mapping(self, mapping_dir: str) -> Tuple[List[str], List[str]]:
        """加载content_mapping，严格按行号顺序返回原文和译文列表。
        
        最可靠的对齐机制：
        - 使用 line_number 作为唯一标识符，从1开始
        - 不依赖 JSON 键的顺序（JSON 无序）
        - 不依赖外部索引，只依赖内部 line_number 字段
        - 返回格式：([原文], [译文])
        """
        md = Path(mapping_dir) / "content_mapping.json"
        data = json.loads(md.read_text(encoding="utf-8"))
        items: Dict[str, Dict] = data.get("content_mappings", {})
        
        # 收集所有条目，必须有有效的 line_number
        entries = []
        for k, v in items.items():
            line_num = v.get("line_number")
            if line_num is None:
                raise Exception(f"条目 {k} 缺少 line_number 字段，数据损坏")
            
            entries.append({
                "key": k,
                "line_number": int(line_num),
                "original_text": v.get("original_text", ""),
                "translated_text": v.get("translated_text", "")
            })
        
        # 严格按 line_number 排序
        entries.sort(key=lambda x: x["line_number"])
        
        # 验证 line_number 连续性（关键：检测数据损坏）
        for i, entry in enumerate(entries):
            expected_line = i + 1
            actual_line = entry["line_number"]
            if actual_line != expected_line:
                print(f"⚠ 警告：line_number 不连续！位置 {i}: 期望 {expected_line}, 实际 {actual_line}")
        
        # 提取原文和译文列表
        originals = [e["original_text"] for e in entries]
        translations = [e["translated_text"] for e in entries]
        
        return originals, translations


    def save_translations(self, mapping_dir: str, translated_lines: List[str]) -> None:
        """将译文列表按行号严格对齐保存到content_mapping.json。
        
        最可靠的对齐机制：
        - 不修改原有的 line_number（保持绝对稳定）
        - 按 line_number 排序后，第 i 个条目对应 translated_lines[i]
        - 自动更新 translated_at 时间戳
        - 未翻译的行保持空字符串
        """
        md = Path(mapping_dir) / "content_mapping.json"
        obj = json.loads(md.read_text(encoding="utf-8"))
        items = obj.get("content_mappings", {})
        now = datetime.datetime.now().isoformat()
        
        # 按 line_number 排序所有条目（关键：不修改 line_number）
        sorted_items = sorted(items.items(), key=lambda x: x[1].get("line_number", 999999))
        
        # 严格按位置对应更新译文（不重新分配 line_number）
        for idx, (key, item_data) in enumerate(sorted_items):
            # 获取对应位置的译文（如果索引超出范围则为空字符串）
            translation = translated_lines[idx] if idx < len(translated_lines) else ""
            
            # 获取旧译文
            old_translation = item_data.get("translated_text", "")
            
            # 更新译文（关键：不修改 line_number！）
            items[key]["translated_text"] = translation
            
            # 只有内容变化时才更新时间戳
            if translation != old_translation:
                items[key]["translated_at"] = now
        
        obj["project_info"]["updated_at"] = now
        md.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def _match_and_get_new_path(self, src: str, path_mapping: Dict[str, str], doc_dir: Path) -> tuple:
        """匹配图片路径并返回新的相对路径
        
        Returns:
            (matched: bool, new_rel_path: str)
        """
        try:
            logger.debug(f"[export_epub] 匹配路径: src={src}")
            
            # 模拟绝对路径计算
            # 将路径分隔符统一为 /
            current_dir = str(doc_dir).replace("\\", "/")
            if current_dir == ".": 
                current_dir = ""
            
            # 处理 ../
            src_parts = src.split("/")
            curr_parts = current_dir.split("/") if current_dir else []
            
            while src_parts and src_parts[0] == "..":
                src_parts.pop(0)
                if curr_parts:
                    curr_parts.pop()
                    
            abs_src = "/".join(curr_parts + src_parts)
            if abs_src.startswith("/"): 
                abs_src = abs_src[1:]
            
            logger.debug(f"[export_epub] 计算得到 abs_src: {abs_src}")
            
            # 检查映射
            matched = False
            new_abs_path = None
            if abs_src in path_mapping:
                logger.debug(f"[export_epub] 精确匹配成功: {abs_src}")
                new_abs_path = path_mapping[abs_src]
                matched = True
            else:
                # 尝试更宽松的匹配：通过文件名匹配
                logger.debug(f"[export_epub] 精确匹配失败，尝试文件名匹配...")
                src_filename = Path(src).name
                for orig_path, new_path in path_mapping.items():
                    orig_filename = Path(orig_path).name
                    if orig_filename == src_filename:
                        logger.debug(f"[export_epub] 文件名匹配成功: {orig_filename} == {src_filename}")
                        new_abs_path = new_path
                        matched = True
                        break
            
            if matched and new_abs_path:
                logger.debug(f"[export_epub] 使用 new_abs_path: {new_abs_path}")
                # 计算新的相对路径
                import os
                rel_path = os.path.relpath(new_abs_path, str(doc_dir))
                rel_path = rel_path.replace("\\", "/")
                logger.debug(f"[export_epub] 计算得到相对路径: {rel_path}")
                return True, rel_path
            
            return False, ""
        except Exception as e:
            logger.error(f"[export_epub] _match_and_get_new_path 异常: {type(e).__name__}: {e}", exc_info=True)
            return False, ""

    def export_epub(self, mapping_dir: str, output_path: str, image_map: Dict[str, str] = None,
                    image_text_map: Dict[str, Dict] = None) -> str:
        """根据mapping重建并导出EPUB（保留原结构与样式，文本替换为译文）。

        改进说明：
        1. 严格按line_number全局顺序读取译文
        2. 确保第N行译文替换第N个原文段落
        3. 没有译文的段落保留原文
        4. 修复图片前后的多余空白问题
        5. 修正特殊格式标题识别问题
        6. 支持插图翻译替换（新增）
        7. 支持图片文字翻译注释（figcaption）

        返回输出文件路径
        """
        try:
            from ebooklib import epub
            import ebooklib
            from bs4 import BeautifulSoup, NavigableString
            import mimetypes
        except ImportError:
            raise Exception("需要安装ebooklib和beautifulsoup4库来支持EPUB导出")

        mapping_dir_p = Path(mapping_dir)
        content_file = mapping_dir_p / "content_mapping.json"
        if not content_file.exists():
            raise Exception("缺少content_mapping.json，无法导出EPUB")

        content_obj = json.loads(content_file.read_text(encoding="utf-8"))
        items = content_obj.get("content_mappings", {})
        project_info = content_obj.get("project_info", {})
        original_file = project_info.get("original_file")
        if not original_file or not Path(original_file).exists():
            raise Exception("original_file不存在，无法基于原结构导出EPUB")

        # 加载原书以保留结构
        book = epub.read_epub(str(original_file))
        
        # 处理图片替换
        logger.debug(f"[export_epub] ====== 开始处理图片替换 ======")
        logger.debug(f"[export_epub] image_map: {image_map}")
        if image_map:
            print(f"正在处理 {len(image_map)} 张图片的替换...")
            logger.debug(f"[export_epub] 正在处理 {len(image_map)} 张图片的替换")
            # 1. 添加新图片到书籍
            # image_map: {original_epub_path: local_new_filename}
            # 我们需要构建 new_epub_path，通常是原来的目录 + 新文件名
            
            # 预处理：构建 原始路径 -> 新路径 的映射，并添加Item
            path_mapping = {} # {original_epub_path: new_epub_path}
            
            # 获取images目录
            local_images_dir = mapping_dir_p / "images"
            logger.debug(f"[export_epub] local_images_dir: {local_images_dir}")
            
            for orig_path, new_filename in image_map.items():
                logger.debug(f"[export_epub] 处理图片: orig_path={orig_path}, new_filename={new_filename}")
                local_file = local_images_dir / new_filename
                logger.debug(f"[export_epub] local_file: {local_file}, 存在={local_file.exists()}")
                if not local_file.exists():
                    print(f"⚠ 警告：新图片文件丢失: {local_file}")
                    continue
                
                try:
                    # 读取新图片内容
                    with open(local_file, "rb") as f:
                        img_content = f.read()
                    logger.debug(f"[export_epub] 读取图片成功，大小={len(img_content)} bytes")
                    
                    # 构建新图片在EPUB中的路径 (保持在同一目录下)
                    # orig_path 类似于 OEBPS/images/cover.jpg
                    orig_p = Path(orig_path)
                    new_epub_path = str(orig_p.parent / new_filename).replace("\\", "/")
                    logger.debug(f"[export_epub] new_epub_path: {new_epub_path}")
                    
                    # 创建新的EpubImage Item
                    # ID需要唯一，使用文件名作为基础
                    new_id = f"img_{Path(new_filename).stem}"
                    
                    img_item = epub.EpubImage(
                        uid=new_id,
                        file_name=new_epub_path,
                        media_type=mimetypes.guess_type(new_filename)[0] or "image/jpeg",
                        content=img_content
                    )
                    
                    # 添加到书籍
                    book.add_item(img_item)
                    logger.debug(f"[export_epub] 图片已添加到书籍: {new_epub_path}")
                    
                    path_mapping[orig_path] = new_epub_path
                    logger.debug(f"[export_epub] path_mapping 条目: {orig_path} -> {new_epub_path}")
                    
                except Exception as e:
                    logger.error(f"[export_epub] 添加图片 {new_filename} 失败: {type(e).__name__}: {e}", exc_info=True)
                    print(f"⚠ 添加图片 {new_filename} 失败: {e}")
            
            logger.debug(f"[export_epub] path_mapping 完整内容: {path_mapping}")
            
            # 2. 替换文档中的引用
            # 遍历所有文档
            logger.debug(f"[export_epub] ====== 开始替换文档中的图片引用 ======")
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    try:
                        doc_name = item.get_name()
                        logger.debug(f"[export_epub] 处理文档: {doc_name}")
                        content = item.get_content().decode("utf-8", errors="ignore")
                        # 简单字符串替换？不安全，因为路径可能是相对的
                        # 使用BS4解析
                        soup = BeautifulSoup(content, "html.parser")
                        modified = False
                        
                        # 获取当前文档的路径，用于计算相对路径
                        doc_path = Path(doc_name)
                        doc_dir = doc_path.parent
                        logger.debug(f"[export_epub] doc_dir: {doc_dir}")
                        
                        # 替换 <img> src
                        logger.debug(f"[export_epub] 文档内容片段: {content[:1000]}...")
                        all_tags = [tag.name for tag in soup.find_all(True)]
                        logger.debug(f"[export_epub] 文档中的所有标签: {all_tags}")
                        
                        # 1. 处理 <img> 标签
                        for img_idx, img in enumerate(soup.find_all("img")):
                            src = img.get("src")
                            logger.debug(f"[export_epub] 找到 <img> {img_idx}: src={src}")
                            if src:
                                matched, new_rel_path = self._match_and_get_new_path(src, path_mapping, doc_dir)
                                if matched:
                                    img["src"] = new_rel_path
                                    modified = True
                                    logger.debug(f"[export_epub] 已更新 <img> src: {new_rel_path}")
                        
                        # 2. 处理 SVG 中的 <image> 标签
                        for img_idx, image in enumerate(soup.find_all("image")):
                            href = image.get("xlink:href") or image.get("href")
                            logger.debug(f"[export_epub] 找到 <image> {img_idx}: href={href}")
                            if href:
                                matched, new_rel_path = self._match_and_get_new_path(href, path_mapping, doc_dir)
                                if matched:
                                    if image.has_attr("xlink:href"):
                                        image["xlink:href"] = new_rel_path
                                    else:
                                        image["href"] = new_rel_path
                                    modified = True
                                    logger.debug(f"[export_epub] 已更新 <image> href: {new_rel_path}")

                        if modified:
                            logger.debug(f"[export_epub] 文档 {doc_name} 有修改，保存...")
                            item.set_content(str(soup).encode("utf-8"))
                        else:
                            logger.debug(f"[export_epub] 文档 {doc_name} 无修改")
                            
                    except Exception as e:
                        logger.error(f"[export_epub] 处理图片引用失败 ({item.get_name()}): {type(e).__name__}: {e}", exc_info=True)
                        print(f"⚠ 处理图片引用失败 ({item.get_name()}): {e}")

                # 处理 CSS 中的 background-image
                elif item.get_type() == ebooklib.ITEM_STYLE:
                    try:
                        content = item.get_content().decode("utf-8", errors="ignore")
                        # 简单正则替换 url(...)
                        # 这比较复杂，因为CSS中的url也是相对的
                        pass
                    except Exception:
                        pass

        # 构造按line_number排序的译文列表（关键：严格按line_number从1开始排序）
        sorted_items = sorted(items.items(), key=lambda x: x[1].get("line_number", 999999))
        
        # 构建译文列表和原文列表（用于对照验证）
        translations = []
        originals = []
        for k, v in sorted_items:
            translation = v.get("translated_text", "")
            original = v.get("original_text", "")
            # 关键修复：只有当译文非空且与原文不同时才替换
            if translation.strip() and translation.strip() != original.strip():
                translations.append(translation.strip())
                originals.append(original.strip())
            else:
                # 没有译文或译文与原文相同，使用空字符串标记（保留原文）
                translations.append("")
                originals.append(original.strip())
        
        # 全局行号计数器（从0开始索引translations列表）
        global_line_index = 0

        # 替换文档文本
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                try:
                    html = item.get_content().decode("utf-8", errors="ignore")
                    soup = BeautifulSoup(html, "html.parser")
                    
                    # 按文档顺序遍历所有块级标签（与导入时一致）
                    for node in soup.find_all(True):
                        if node.name in self.BLOCK_TAGS:
                            # 检查是否为叶子块节点（与导入时保持一致）
                            has_block_children = any(child.name in self.BLOCK_TAGS for child in node.find_all(True, recursive=False))
                            if has_block_children:
                                continue
                            
                            # 检查是否仅包含图片（img标签），跳过图片容器
                            img_only = False
                            if node.find('img'):
                                # 检查除了img以外是否还有其他有意义的内容
                                text_content = ''.join([str(s) for s in node.find_all(string=True, recursive=True)]).strip()
                                if not text_content or len(text_content) < 2:
                                    img_only = True
                            
                            if img_only:
                                # 图片容器，不计入行号，跳过
                                continue
                            
                            has_text = bool((node.get_text() or "").strip())
                            if has_text:
                                # 使用全局行号获取对应译文
                                if global_line_index < len(translations):
                                    translation = translations[global_line_index]
                                    if translation:  # 只替换非空译文
                                        try:
                                            # 清除现有文本节点，保留标签结构
                                            for s in list(node.find_all(string=True)):
                                                s.extract()
                                            # 插入译文
                                            node.insert(0, soup.new_string(translation))
                                        except Exception:
                                            # 回退：直接设置字符串
                                            node.string = translation
                                global_line_index += 1
                    
                    # 清理图片前后的多余空白
                    # 找到所有图片节点
                    for img in soup.find_all('img'):
                        # 获取图片的父节点
                        parent = img.parent
                        if parent:
                            # 移除父节点中的多余空白文本节点
                            for child in list(parent.children):
                                if isinstance(child, NavigableString):
                                    # 如果是纯空白文本节点，删除
                                    if not str(child).strip():
                                        child.extract()

                    # 注入图片文字翻译注释（figcaption）
                    if image_text_map:
                        for img in soup.find_all('img'):
                            src = img.get('src', '')
                            if not src:
                                continue
                            # 尝试匹配image_text_map中的图片路径
                            matched_key = None
                            for img_path in image_text_map:
                                # 通过文件名匹配（src可能是相对路径）
                                if Path(src).name == Path(img_path).name:
                                    matched_key = img_path
                                    break
                                # 也尝试路径尾部匹配
                                if src.endswith(img_path) or img_path.endswith(src.lstrip('../')):
                                    matched_key = img_path
                                    break
                            if matched_key and image_text_map[matched_key].get('translated_text'):
                                trans_info = image_text_map[matched_key]
                                translated = trans_info['translated_text']
                                original = trans_info.get('original_text', '')
                                # 构建figcaption内容
                                caption_text = f"[翻译] {translated}"
                                if original:
                                    caption_text = f"[原文: {original}] {translated}"
                                # 在img的父节点后插入figcaption
                                figcaption = soup.new_tag('figcaption')
                                figcaption.string = caption_text
                                figcaption['style'] = (
                                    'font-size: 0.85em; color: #666; '
                                    'text-align: center; margin-top: 4px; '
                                    'font-style: italic;'
                                )
                                # 插入到img之后（在其父节点内）
                                img_parent = img.parent
                                if img_parent:
                                    img.insert_after(figcaption)
                    
                    new_html = str(soup)
                    # 尝试安全设置内容
                    try:
                        item.set_content(new_html.encode("utf-8"))
                    except Exception:
                        try:
                            item._content = new_html.encode("utf-8")
                        except Exception:
                            pass
                except Exception as e:
                    print(f"⚠ 警告：处理文档时出错: {e}")
                    continue

        # 输出路径
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        epub.write_epub(str(out_path), book)
        return str(out_path)