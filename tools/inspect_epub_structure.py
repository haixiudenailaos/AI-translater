#!/usr/bin/env python3
"""检查EPUB HTML结构，找出嵌套标签问题"""

import sys
from pathlib import Path
from ebooklib import epub
import ebooklib
from bs4 import BeautifulSoup

def inspect_epub_chapter(epub_file: str, chapter_id: str):
    """检查指定章节的HTML结构"""
    
    book = epub.read_epub(epub_file)
    
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = getattr(item, "file_name", None) or getattr(item, "href", None) or item.get_name()
            
            if chapter_id in name:
                print(f"\n{'='*80}")
                print(f"章节: {name}")
                print(f"{'='*80}\n")
                
                html = item.get_content().decode("utf-8", errors="ignore")
                soup = BeautifulSoup(html, "html.parser")
                
                # 定义块级标签
                BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre"}
                
                seq = 1
                for node in soup.find_all(True):
                    if node.name in BLOCK_TAGS:
                        # 获取节点的直接文本（不递归）
                        direct_text = ''.join(node.find_all(string=True, recursive=False))
                        # 获取所有文本（递归）
                        all_text = node.get_text()
                        
                        if all_text.strip():
                            print(f"\n--- 序号 {seq:05d} ---")
                            print(f"标签: <{node.name}>")
                            print(f"是否有子标签: {bool(node.find_all(BLOCK_TAGS))}")
                            print(f"子块级标签: {[child.name for child in node.find_all(BLOCK_TAGS)]}")
                            print(f"\n直接文本 (recursive=False):")
                            print(f"  {direct_text.strip()}")
                            print(f"\n全部文本 (recursive=True):")
                            print(f"  {all_text.strip()}")
                            
                            # 如果全部文本和直接文本不同，说明有嵌套
                            if all_text.strip() != direct_text.strip():
                                print(f"\n⚠ 警告：检测到嵌套！全部文本包含了子标签的内容")
                            
                            seq += 1
                            
                            # 只显示前15个块
                            if seq > 15:
                                print(f"\n... 省略剩余内容 ...")
                                break
                
                break

if __name__ == "__main__":
    epub_file = r"C:\Users\hanshuncheng\Desktop\日文原版(1).epub"
    chapter_id = "p-0013.xhtml"
    
    if len(sys.argv) > 1:
        epub_file = sys.argv[1]
    if len(sys.argv) > 2:
        chapter_id = sys.argv[2]
    
    inspect_epub_chapter(epub_file, chapter_id)
