#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析特定行的对齐问题工具
用于深入分析用户指定的行号范围的对齐状况
"""

import sys
import json
from pathlib import Path

def analyze_specific_lines(mapping_dir: str, start_line: int, end_line: int):
    """分析指定行号范围的对齐问题"""
    mapping_path = Path(mapping_dir)
    content_file = mapping_path / "content_mapping.json"
    
    if not content_file.exists():
        print(f"❌ 文件不存在: {content_file}")
        return False
    
    print(f"\n{'='*80}")
    print(f"特定行对齐问题分析")
    print(f"{'='*80}")
    print(f"映射文件: {content_file}")
    print(f"分析范围: 第 {start_line} 行到第 {end_line} 行")
    print()
    
    # 读取数据
    try:
        data = json.loads(content_file.read_text(encoding="utf-8"))
        items = data.get("content_mappings", {})
    except Exception as e:
        print(f"❌ 读取文件失败: {e}")
        return False
    
    # 收集指定范围的条目
    target_entries = []
    for key, value in items.items():
        line_num = value.get("line_number")
        if line_num and start_line <= int(line_num) <= end_line:
            target_entries.append({
                "key": key,
                "line_number": int(line_num),
                "original_text": value.get("original_text", ""),
                "translated_text": value.get("translated_text", ""),
                "chapter_id": value.get("chapter_id", ""),
                "translated_at": value.get("translated_at", "")
            })
    
    # 按行号排序
    target_entries.sort(key=lambda x: x["line_number"])
    
    print(f"找到 {len(target_entries)} 个条目")
    print()
    
    # 详细分析每一行
    alignment_issues = []
    
    for entry in target_entries:
        print(f"{'-'*80}")
        print(f"行号: {entry['line_number']} (键: {entry['key']})")
        print(f"章节: {entry['chapter_id']}")
        print(f"原文: {entry['original_text']}")
        print(f"译文: {entry['translated_text']}")
        print(f"翻译时间: {entry['translated_at']}")
        
        # 检查对齐问题
        issues = []
        
        # 1. 检查译文是否为空
        if not entry['translated_text'].strip():
            issues.append("译文为空")
        
        # 2. 检查原文和译文长度差异过大
        if entry['original_text'] and entry['translated_text']:
            orig_len = len(entry['original_text'])
            trans_len = len(entry['translated_text'])
            ratio = trans_len / orig_len if orig_len > 0 else 0
            
            if ratio > 3 or ratio < 0.3:  # 长度差异过大
                issues.append(f"长度差异异常 (原文:{orig_len}字, 译文:{trans_len}字, 比例:{ratio:.2f})")
        
        # 3. 检查是否包含日文字符但译文是中文
        if entry['original_text'] and entry['translated_text']:
            has_japanese = any('\u3040' <= c <= '\u309F' or '\u30A0' <= c <= '\u30FF' or '\u4E00' <= c <= '\u9FAF' for c in entry['original_text'])
            has_chinese = any('\u4E00' <= c <= '\u9FAF' for c in entry['translated_text'])
            
            if has_japanese and not has_chinese and entry['translated_text'].strip():
                issues.append("原文是日文但译文不是中文")
        
        # 4. 检查译文是否看起来像是其他行的内容
        if entry['translated_text'] and len(entry['translated_text']) > 20:
            # 检查是否包含明显的叙述性内容而原文是对话
            if entry['original_text'].startswith('「') and entry['original_text'].endswith('」'):
                if not (entry['translated_text'].startswith('"') or entry['translated_text'].startswith('「')):
                    issues.append("原文是对话但译文不是对话格式")
        
        if issues:
            alignment_issues.append({
                "line_number": entry['line_number'],
                "key": entry['key'],
                "issues": issues
            })
            print(f"⚠️  发现问题: {', '.join(issues)}")
        else:
            print("✓ 对齐正常")
        
        print()
    
    # 总结分析结果
    print(f"{'='*80}")
    print(f"分析总结")
    print(f"{'='*80}")
    
    if alignment_issues:
        print(f"❌ 发现 {len(alignment_issues)} 行存在对齐问题:")
        for issue in alignment_issues:
            print(f"  - 第 {issue['line_number']} 行 ({issue['key']}): {', '.join(issue['issues'])}")
        
        print(f"\n可能的原因:")
        print(f"  1. 翻译过程中出现了行号错位")
        print(f"  2. 批量翻译时API返回的译文顺序与原文不匹配")
        print(f"  3. 手动编辑时误操作导致内容错位")
        print(f"  4. 翻译工具的对齐机制存在bug")
        
        print(f"\n建议修复方案:")
        print(f"  1. 检查翻译工具的save_translations方法是否正确按line_number对齐")
        print(f"  2. 重新翻译这些有问题的行")
        print(f"  3. 手动修正错位的译文内容")
        
        return False
    else:
        print(f"✓ 所有行对齐正常，未发现问题")
        return True

def main():
    """主函数"""
    print(f"\n{'#'*80}")
    print(f"特定行对齐问题分析工具")
    print(f"{'#'*80}")
    
    if len(sys.argv) < 4:
        print("\n用法:")
        print("  python analyze_specific_alignment.py <mapping目录路径> <起始行号> <结束行号>")
        print("\n示例:")
        print("  python analyze_specific_alignment.py mapping/my_book 630 633")
        print("  python analyze_specific_alignment.py \"C:/Users/user/Desktop/mapping/日文原版(1)\" 630 633")
        sys.exit(1)
    
    mapping_dir = sys.argv[1]
    try:
        start_line = int(sys.argv[2])
        end_line = int(sys.argv[3])
    except ValueError:
        print("❌ 行号必须是数字")
        sys.exit(1)
    
    if start_line > end_line:
        print("❌ 起始行号不能大于结束行号")
        sys.exit(1)
    
    try:
        is_aligned = analyze_specific_lines(mapping_dir, start_line, end_line)
        
        if is_aligned:
            print(f"\n{'#'*80}")
            print(f"✓ 分析完成：指定行对齐正常")
            print(f"{'#'*80}")
            sys.exit(0)
        else:
            print(f"\n{'#'*80}")
            print(f"❌ 分析完成：发现对齐问题")
            print(f"{'#'*80}")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\n\n分析已取消")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 分析异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()