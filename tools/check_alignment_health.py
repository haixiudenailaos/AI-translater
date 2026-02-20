#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速对齐健康检查工具
用于诊断现有 mapping 文件的对齐状态
"""

import sys
import json
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def check_mapping_health(mapping_dir: str):
    """检查 mapping 文件的健康状态"""
    mapping_path = Path(mapping_dir)
    content_file = mapping_path / "content_mapping.json"
    
    if not content_file.exists():
        print(f"❌ 文件不存在: {content_file}")
        return False
    
    print(f"\n{'='*60}")
    print(f"对齐健康检查")
    print(f"{'='*60}")
    print(f"映射文件: {content_file}")
    print()
    
    # 读取数据
    try:
        data = json.loads(content_file.read_text(encoding="utf-8"))
        items = data.get("content_mappings", {})
    except Exception as e:
        print(f"❌ 读取文件失败: {e}")
        return False
    
    total_count = len(items)
    print(f"总条目数: {total_count}")
    
    # 检查1：line_number 字段完整性
    print(f"\n{'-'*60}")
    print("检查1：line_number 字段完整性")
    print(f"{'-'*60}")
    
    missing_line_number = []
    entries = []
    
    for key, value in items.items():
        line_num = value.get("line_number")
        if line_num is None:
            missing_line_number.append(key)
        else:
            entries.append({
                "key": key,
                "line_number": int(line_num),
                "original_text": value.get("original_text", ""),
                "translated_text": value.get("translated_text", "")
            })
    
    if missing_line_number:
        print(f"❌ 发现 {len(missing_line_number)} 个条目缺少 line_number 字段")
        print(f"   示例: {missing_line_number[:5]}")
        return False
    else:
        print(f"✓ 所有条目都有 line_number 字段")
    
    # 检查2：line_number 连续性
    print(f"\n{'-'*60}")
    print("检查2：line_number 连续性")
    print(f"{'-'*60}")
    
    entries.sort(key=lambda x: x["line_number"])
    
    discontinuities = []
    duplicates = []
    
    for i, entry in enumerate(entries):
        expected = i + 1
        actual = entry["line_number"]
        
        if actual != expected:
            discontinuities.append({
                "position": i,
                "expected": expected,
                "actual": actual,
                "key": entry["key"]
            })
        
        # 检查重复
        if i > 0 and entry["line_number"] == entries[i-1]["line_number"]:
            duplicates.append({
                "line_number": actual,
                "keys": [entries[i-1]["key"], entry["key"]]
            })
    
    if discontinuities:
        print(f"❌ 发现 {len(discontinuities)} 处 line_number 不连续")
        print("   前10处不连续位置：")
        for d in discontinuities[:10]:
            print(f"     位置 {d['position']}: 期望 {d['expected']}, 实际 {d['actual']} (键: {d['key']})")
        return False
    else:
        print(f"✓ line_number 从 1 到 {total_count} 连续递增")
    
    if duplicates:
        print(f"❌ 发现 {len(duplicates)} 处 line_number 重复")
        for dup in duplicates[:5]:
            print(f"   line_number {dup['line_number']}: {dup['keys']}")
        return False
    else:
        print(f"✓ 没有重复的 line_number")
    
    # 检查3：翻译进度统计
    print(f"\n{'-'*60}")
    print("检查3：翻译进度统计")
    print(f"{'-'*60}")
    
    translated_count = sum(1 for e in entries if e["translated_text"].strip())
    empty_count = total_count - translated_count
    progress = (translated_count / total_count * 100) if total_count > 0 else 0
    
    print(f"总行数: {total_count}")
    print(f"已翻译: {translated_count}")
    print(f"未翻译: {empty_count}")
    print(f"进度: {progress:.1f}%")
    
    # 检查4：数据完整性
    print(f"\n{'-'*60}")
    print("检查4：数据完整性")
    print(f"{'-'*60}")
    
    missing_original = sum(1 for e in entries if not e["original_text"].strip())
    if missing_original > 0:
        print(f"⚠ 警告：发现 {missing_original} 行原文为空")
    else:
        print(f"✓ 所有行都有原文内容")
    
    # 总结
    print(f"\n{'='*60}")
    print("健康检查总结")
    print(f"{'='*60}")
    print(f"✓ 映射文件健康状态良好")
    print(f"✓ 原文译文对齐机制正常")
    print(f"✓ 可以安全进行翻译操作")
    
    return True


def main():
    """主函数"""
    print(f"\n{'#'*60}")
    print(f"EPUB 翻译对齐健康检查工具")
    print(f"{'#'*60}")
    
    if len(sys.argv) < 2:
        print("\n用法:")
        print("  python check_alignment_health.py <mapping目录路径>")
        print("\n示例:")
        print("  python check_alignment_health.py mapping/my_book")
        print("  python check_alignment_health.py D:/projects/epub/mapping/book1")
        sys.exit(1)
    
    mapping_dir = sys.argv[1]
    
    try:
        is_healthy = check_mapping_health(mapping_dir)
        
        if is_healthy:
            print(f"\n{'#'*60}")
            print(f"✓ 检查完成：映射文件健康")
            print(f"{'#'*60}")
            sys.exit(0)
        else:
            print(f"\n{'#'*60}")
            print(f"❌ 检查失败：发现对齐问题")
            print(f"{'#'*60}")
            print("\n建议修复方案：")
            print("  1. 重新导入原始 EPUB 文件（推荐）")
            print("  2. 检查是否有手动修改过 content_mapping.json")
            print("  3. 联系技术支持获取修复工具")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\n\n检查已取消")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 检查异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
