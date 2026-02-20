#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调试脚本：测试EPUB解析和图片提取
"""

import sys
from pathlib import Path
from ebooklib import epub
import ebooklib

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.core.epub_processor import EPUBProcessor

# 查找EPUB文件
epub_files = list(Path('.').glob('*.epub'))
if not epub_files:
    epub_files = list(Path('..').glob('*.epub'))

if not epub_files:
    print("❌ 没有找到EPUB文件")
    sys.exit(1)

epub_path = epub_files[0]
print(f"📖 找到EPUB文件: {epub_path}")

# 先测试ebooklib直接读取
print("\n" + "="*80)
print("【阶段1】用ebooklib直接读取")
print("="*80)

try:
    book = epub.read_epub(str(epub_path))
    
    print(f"\n📦 Manifest条目数: {len(book.get_items())}")
    print(f"\n📋 所有item类型和media_type:")
    for item in book.get_items():
        item_type = item.get_type()
        media_type = item.get_media_type()
        name = getattr(item, "file_name", None) or getattr(item, "href", None) or item.get_name()
        
        type_str = str(item_type)
        if item_type == ebooklib.ITEM_DOCUMENT:
            type_str = "ITEM_DOCUMENT"
        elif item_type == ebooklib.ITEM_IMAGE:
            type_str = "ITEM_IMAGE"
        elif item_type == ebooklib.ITEM_STYLE:
            type_str = "ITEM_STYLE"
        
        print(f"  - {name:<50} | {type_str:<20} | {media_type}")
        
        # 检查是否是图片
        if item_type == ebooklib.ITEM_IMAGE or (media_type and media_type.startswith('image/')):
            data = item.get_content()
            print(f"    ✓ 这是图片! 大小: {len(data)} bytes")
            
except Exception as e:
    print(f"❌ ebooklib读取失败: {e}")
    import traceback
    traceback.print_exc()

# 测试我们的EPUBProcessor
print("\n" + "="*80)
print("【阶段2】用EPUBProcessor解析")
print("="*80)

try:
    processor = EPUBProcessor()
    result = processor.import_epub(str(epub_path), extract_images=True)
    print(f"\n✅ 解析成功!")
    print(f"   mapping_dir: {result['mapping_dir']}")
    
    # 检查生成的images.json
    import json
    images_file = Path(result['images_file'])
    if images_file.exists():
        images_data = json.loads(images_file.read_text(encoding='utf-8'))
        image_mappings = images_data.get('image_mappings', {})
        print(f"   images.json包含 {len(image_mappings)} 张图片")
        
        if image_mappings:
            print("\n   图片列表:")
            for name, info in image_mappings.items():
                print(f"     - {name} ({info.get('file_size', 0)} bytes)")
    else:
        print("   ❌ images.json不存在!")
        
except Exception as e:
    print(f"❌ EPUBProcessor解析失败: {e}")
    import traceback
    traceback.print_exc()
