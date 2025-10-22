#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
添加navigation-documents.xhtml到spine_order
"""

import json
from pathlib import Path


def add_navigation_to_spine(mapping_dir: str):
    """添加导航文档到spine顺序"""
    format_file = Path(mapping_dir) / "format_info.json"
    
    print("=" * 70)
    print("添加导航文档到spine_order")
    print("=" * 70)
    print()
    
    if not format_file.exists():
        print(f"❌ 找不到文件: {format_file}")
        return False
    
    try:
        # 读取数据
        with open(format_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        spine_order = data.get("spine_order", [])
        print(f"📖 当前spine_order: {len(spine_order)}个章节")
        
        # 检查是否已存在
        nav_doc = "Text/navigation-documents.xhtml"
        if nav_doc in spine_order:
            print(f"✓ {nav_doc} 已存在于spine_order中")
            return True
        
        # 添加到最前面
        spine_order.insert(0, nav_doc)
        data["spine_order"] = spine_order
        
        # 备份
        backup_file = format_file.with_suffix('.json.bak2')
        with open(backup_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"📦 已备份到: {backup_file}")
        
        # 保存
        with open(format_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"✅ 已添加 {nav_doc} 到spine_order最前面")
        print(f"   新spine_order: {len(spine_order)}个章节")
        print()
        print("=" * 70)
        print("✅ 修复完成！")
        print("=" * 70)
        
        return True
        
    except Exception as e:
        print(f"❌ 操作失败: {e}")
        return False


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("用法: python add_navigation_to_spine.py <映射目录>")
        sys.exit(1)
    
    add_navigation_to_spine(sys.argv[1])
