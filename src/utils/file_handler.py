#!/usr/bin/env python3
"""
文件处理工具模块

P3-1：原子写入函数已统一到 infrastructure/atomic_file.py。
本模块保留 write_text_atomic / write_json_atomic 作为向后兼容的 re-export，
调用方应逐步迁移到 infrastructure.atomic_file 直接导入。
"""

import re
from html import unescape
from pathlib import Path

# P3-1：原子写入实现统一到 infrastructure/atomic_file.py，此处仅 re-export
from ..infrastructure.atomic_file import (
    write_json_atomic,
    write_text_atomic,
)
from .logger import get_logger

logger = get_logger(__name__)


# Detecting a legacy encoding is useful, but running chardet over an entire
# novel makes import CPU scale with file size for no additional benefit.  The
# sample is deliberately large enough for Chinese-language detectors while
# keeping detection bounded for 100 MiB+ TXT files.
_ENCODING_DETECTION_SAMPLE_BYTES = 64 * 1024


__all__ = [
    "write_text_atomic",
    "write_json_atomic",
    "FileHandler",
]


class FileHandler:
    def __init__(self):
        self.supported_encodings = ["utf-8", "gbk", "gb2312", "utf-16"]

    def read_file(self, file_path: str) -> str:
        """读取文件内容，自动检测编码，支持EPUB格式"""
        try:
            file_path = Path(file_path)

            # 检查是否为EPUB文件
            if file_path.suffix.lower() == ".epub":
                return self._read_epub_file(file_path)

            # 读取普通文件字节内容
            with open(file_path, "rb") as f:
                raw_data = f.read()

            # UTF-8 is the normal path.  Decode it before importing/running
            # chardet so UTF-8 and ASCII imports incur no detector cost.
            try:
                return raw_data.decode("utf-8-sig")
            except UnicodeDecodeError:
                pass

            # UTF-16 text is reliably identified by its BOM.  Handle it before
            # heuristic detection to avoid a false positive on short files.
            if raw_data.startswith((b"\xff\xfe", b"\xfe\xff")):
                try:
                    return raw_data.decode("utf-16")
                except UnicodeDecodeError:
                    pass

            # Encoding detection is only needed after the user imports a file.
            # Keep chardet out of module scope to preserve fast GUI startup,
            # and limit it to a fixed sample so its CPU cost is O(1).
            import chardet

            detected = chardet.detect(raw_data[:_ENCODING_DETECTION_SAMPLE_BYTES])
            encoding = str(detected.get("encoding") or "").lower()

            # 先尝试探测结果（GB2312/GBK 等），再用项目支持列表兜底。
            candidates: list[str] = []
            if encoding in self.supported_encodings:
                candidates.append(encoding)
            candidates.extend(enc for enc in self.supported_encodings if enc not in candidates)

            for candidate in candidates:
                try:
                    return raw_data.decode(candidate)
                except UnicodeDecodeError:
                    continue

            # 所有严格解码均失败时，保留旧行为：优先可读而不是中断导入。
            return raw_data.decode("utf-8", errors="ignore")

        except Exception as e:
            raise Exception(f"读取文件失败: {str(e)}")

    def _read_epub_file(self, file_path: Path) -> str:
        """读取EPUB文件内容，提取纯文本，支持多语言编码"""
        try:
            import ebooklib
            from bs4 import BeautifulSoup
            from ebooklib import epub
        except ImportError:
            raise Exception("需要安装ebooklib和beautifulsoup4库来支持EPUB文件")

        try:
            # 读取EPUB文件
            book = epub.read_epub(str(file_path))

            # 存储所有文本内容
            text_content = []

            # 获取所有文档项目
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    # 获取HTML内容
                    html_content = item.get_content().decode("utf-8", errors="ignore")

                    # 使用BeautifulSoup解析HTML
                    soup = BeautifulSoup(html_content, "html.parser")

                    # 移除脚本和样式标签
                    for script in soup(["script", "style"]):
                        script.decompose()

                    # 提取纯文本
                    text = soup.get_text()

                    # 清理文本：移除多余的空白字符
                    text = re.sub(r"\n\s*\n", "\n\n", text)  # 合并多个空行
                    text = re.sub(r"[ \t]+", " ", text)  # 合并多个空格
                    text = text.strip()

                    if text:
                        text_content.append(text)

            # 合并所有文本内容
            full_text = "\n\n".join(text_content)

            # HTML实体解码，确保特殊字符正确显示
            full_text = unescape(full_text)

            # 确保返回的文本是UTF-8编码
            if isinstance(full_text, bytes):
                full_text = full_text.decode("utf-8", errors="ignore")

            return full_text

        except Exception as e:
            raise Exception(f"读取EPUB文件失败: {str(e)}")

    def write_file(self, file_path: str, content: str, encoding: str = "utf-8") -> bool:
        """保存文件内容。

        BUG-006：使用原子写入，失败时抛出异常（不再吞掉异常返回 False）。
        - 写入失败保留原文件，不显示成功。
        - 成功返回 True；失败时抛出具体异常，由 UI 边界负责展示。
        """
        write_text_atomic(file_path, content, encoding)
        return True

    def create_comparison_file(self, source_content: str, target_content: str) -> str:
        """创建原文译文对照文件"""
        source_lines = source_content.split("\n")
        target_lines = target_content.split("\n")

        comparison_lines = []
        comparison_lines.append("=" * 60)
        comparison_lines.append("原文译文对照文件")
        comparison_lines.append("=" * 60)
        comparison_lines.append("")

        max_lines = max(len(source_lines), len(target_lines))

        for i in range(max_lines):
            source_line = source_lines[i] if i < len(source_lines) else ""
            target_line = target_lines[i] if i < len(target_lines) else ""

            if source_line.strip() or target_line.strip():
                comparison_lines.append(f"【原文 {i + 1:03d}】 {source_line}")
                comparison_lines.append(f"【译文 {i + 1:03d}】 {target_line}")
                comparison_lines.append("-" * 40)

        return "\n".join(comparison_lines)

    def auto_generate_filename(self, original_filename: str = None) -> str:
        """自动生成译文文件名"""
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if original_filename:
            # 如果有原文件名，生成 "原文_译文.txt" 格式
            original_path = Path(original_filename)
            name_without_ext = original_path.stem
            return f"{name_without_ext}_译文_{timestamp}.txt"
        else:
            # 如果没有原文件名，生成默认名称
            return f"翻译结果_{timestamp}.txt"

    def get_file_info(self, file_path: str) -> dict:
        """获取文件信息"""
        try:
            file_path = Path(file_path)

            if not file_path.exists():
                return {"error": "文件不存在"}

            stat = file_path.stat()

            return {
                "name": file_path.name,
                "size": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": stat.st_mtime,
                "is_text": self._is_text_file(file_path),
            }

        except Exception as e:
            return {"error": str(e)}

    def _is_text_file(self, file_path: Path) -> bool:
        """判断是否为文本文件"""
        text_extensions = {
            ".txt",
            ".md",
            ".py",
            ".js",
            ".html",
            ".css",
            ".json",
            ".xml",
            ".csv",
            ".epub",
        }

        if file_path.suffix.lower() in text_extensions:
            return True

        # 尝试读取文件开头判断
        try:
            with open(file_path, "rb") as f:
                chunk = f.read(1024)

            # 检测是否包含null字节（二进制文件特征）
            if b"\x00" in chunk:
                return False

            # 尝试解码
            try:
                chunk.decode("utf-8")
                return True
            except UnicodeDecodeError:
                return False

        except Exception:
            return False

    def backup_file(self, file_path: str, backup_dir: "Path | str | None" = None) -> str | None:
        """备份文件。

        STORAGE-6：可选 ``backup_dir`` 指定备份目标目录（应传入
        ``ResolvedStoragePaths.translation_backups_dir``），避免备份
        散落在源文件旁边；缺省保持旧行为（源文件旁），兼容存量调用。
        """
        try:
            file_path = Path(file_path)

            if not file_path.exists():
                return None

            # 生成备份文件名
            if backup_dir is not None:
                target_dir = Path(backup_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
                backup_path = target_dir / f"{file_path.stem}.backup{file_path.suffix}"
            else:
                backup_path = file_path.with_suffix(f".backup{file_path.suffix}")

            # 如果备份文件已存在，添加时间戳
            if backup_path.exists():
                from datetime import datetime

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                if backup_dir is not None:
                    backup_path = backup_path.with_name(
                        f"{file_path.stem}.backup_{timestamp}{file_path.suffix}"
                    )
                else:
                    backup_path = file_path.with_suffix(f".backup_{timestamp}{file_path.suffix}")

            # 复制文件
            import shutil

            shutil.copy2(file_path, backup_path)

            return str(backup_path)

        except Exception as e:
            logger.error("备份文件失败: %s", e)
            return None
