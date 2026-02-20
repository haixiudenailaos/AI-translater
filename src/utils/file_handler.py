#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件处理工具模块
"""

import os
from pathlib import Path
from typing import Optional
import chardet
import re
from html import unescape

class FileHandler:
    def __init__(self):
        self.supported_encodings = ['utf-8', 'gbk', 'gb2312', 'utf-16']
        
    def read_file(self, file_path: str) -> str:
        """读取文件内容，自动检测编码，支持EPUB格式"""
        try:
            file_path = Path(file_path)
            
            # 检查是否为EPUB文件
            if file_path.suffix.lower() == '.epub':
                return self._read_epub_file(file_path)
            
            # 读取普通文件字节内容
            with open(file_path, 'rb') as f:
                raw_data = f.read()
                
            # 检测编码
            detected = chardet.detect(raw_data)
            encoding = detected.get('encoding', 'utf-8')
            
            # 如果检测到的编码不在支持列表中，使用utf-8
            if encoding not in self.supported_encodings:
                encoding = 'utf-8'
                
            # 尝试用检测到的编码读取
            try:
                content = raw_data.decode(encoding)
                return content
            except UnicodeDecodeError:
                # 如果失败，尝试其他编码
                for enc in self.supported_encodings:
                    try:
                        content = raw_data.decode(enc)
                        return content
                    except UnicodeDecodeError:
                        continue
                        
                # 如果所有编码都失败，使用utf-8并忽略错误
                return raw_data.decode('utf-8', errors='ignore')
                
        except Exception as e:
            raise Exception(f"读取文件失败: {str(e)}")
            
    def _read_epub_file(self, file_path: Path) -> str:
        """读取EPUB文件内容，提取纯文本，支持多语言编码"""
        try:
            import ebooklib
            from ebooklib import epub
            from bs4 import BeautifulSoup
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
                    html_content = item.get_content().decode('utf-8', errors='ignore')
                    
                    # 使用BeautifulSoup解析HTML
                    soup = BeautifulSoup(html_content, 'html.parser')
                    
                    # 移除脚本和样式标签
                    for script in soup(["script", "style"]):
                        script.decompose()
                    
                    # 提取纯文本
                    text = soup.get_text()
                    
                    # 清理文本：移除多余的空白字符
                    text = re.sub(r'\n\s*\n', '\n\n', text)  # 合并多个空行
                    text = re.sub(r'[ \t]+', ' ', text)      # 合并多个空格
                    text = text.strip()
                    
                    if text:
                        text_content.append(text)
            
            # 合并所有文本内容
            full_text = '\n\n'.join(text_content)
            
            # HTML实体解码，确保特殊字符正确显示
            full_text = unescape(full_text)
            
            # 确保返回的文本是UTF-8编码
            if isinstance(full_text, bytes):
                full_text = full_text.decode('utf-8', errors='ignore')
            
            return full_text
            
        except Exception as e:
            raise Exception(f"读取EPUB文件失败: {str(e)}")
            
    def write_file(self, file_path: str, content: str, encoding: str = 'utf-8') -> bool:
        """保存文件内容"""
        try:
            file_path = Path(file_path)
            
            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 保存文件
            with open(file_path, 'w', encoding=encoding) as f:
                f.write(content)
                
            return True
            
        except Exception as e:
            print(f"保存文件失败: {e}")
            return False

    def save_file(self, file_path: str, content: str, encoding: str = 'utf-8') -> bool:
        """兼容旧调用：保存文件内容，委托到 write_file"""
        return self.write_file(file_path, content, encoding)
            
    def create_comparison_file(self, source_content: str, target_content: str) -> str:
        """创建原文译文对照文件"""
        source_lines = source_content.split('\n')
        target_lines = target_content.split('\n')
        
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
                comparison_lines.append(f"【原文 {i+1:03d}】 {source_line}")
                comparison_lines.append(f"【译文 {i+1:03d}】 {target_line}")
                comparison_lines.append("-" * 40)
                
        return '\n'.join(comparison_lines)
        
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
                "is_text": self._is_text_file(file_path)
            }
            
        except Exception as e:
            return {"error": str(e)}
            
    def _is_text_file(self, file_path: Path) -> bool:
        """判断是否为文本文件"""
        text_extensions = {'.txt', '.md', '.py', '.js', '.html', '.css', '.json', '.xml', '.csv', '.epub'}
        
        if file_path.suffix.lower() in text_extensions:
            return True
            
        # 尝试读取文件开头判断
        try:
            with open(file_path, 'rb') as f:
                chunk = f.read(1024)
                
            # 检测是否包含null字节（二进制文件特征）
            if b'\x00' in chunk:
                return False
                
            # 尝试解码
            try:
                chunk.decode('utf-8')
                return True
            except UnicodeDecodeError:
                return False
                
        except:
            return False
            
    def backup_file(self, file_path: str) -> Optional[str]:
        """备份文件"""
        try:
            file_path = Path(file_path)
            
            if not file_path.exists():
                return None
                
            # 生成备份文件名
            backup_path = file_path.with_suffix(f'.backup{file_path.suffix}')
            
            # 如果备份文件已存在，添加时间戳
            if backup_path.exists():
                from datetime import datetime
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = file_path.with_suffix(f'.backup_{timestamp}{file_path.suffix}')
                
            # 复制文件
            import shutil
            shutil.copy2(file_path, backup_path)
            
            return str(backup_path)
            
        except Exception as e:
            print(f"备份文件失败: {e}")
            return None