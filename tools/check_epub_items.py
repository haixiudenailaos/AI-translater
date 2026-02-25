#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查EPUB中的所有文档文件
"""

import ebooklib
from ebooklib import epub


def check_epub_items(epub_path: str):
    """检查EPUB中的所有项目"""
    print(f"📖 读取EPUB: {epub_path}")
    print()
    
    try:
        book = epub.read_epub(epub_path)
    except Exception as e:
        print(f"❌ 读取失败: {e}")
        return
    
    # 获取所有文档类型的items
    documents = []
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = item.get_name()
            documents.append(name)
    
    print(f"📄 文档类型文件总数: {len(documents)}")
    print()
    
    # 检查navigation-documents.xhtml
    nav_files = [d for d in documents if 'navigation' in d.lower()]
    if nav_files:
        print("🔍 找到导航文件:")
        for nf in nav_files:
            print(f"  - {nf}")
            
            # 检查是否在spine中
            in_spine = False
            for item in book.spine:
                if isinstance(item, tuple):
                    itemref = item[0]
                else:
                    itemref = item
                
                # 获取文件名
                if hasattr(itemref, 'idref'):
                    try:
                        spine_item = book.get_item_with_id(itemref.idref)
                        if spine_item and spine_item.get_name() == nf:
                            in_spine = True
                            break
                    except:
                        pass
            
            print(f"    在spine中: {'是' if in_spine else '否'}")
        print()
    
    # 显示所有文档
    print("📋 所有文档文件:")
    for idx, doc in enumerate(sorted(documents), 1):
        print(f"{idx:3}. {doc}")


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("用法: python check_epub_items.py <EPUB文件>")
        sys.exit(1)
    
    check_epub_items(sys.argv[1])
