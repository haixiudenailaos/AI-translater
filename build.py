#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻小说翻译器V1.1自动化构建脚本
使用 PyInstaller 构建可执行文件，支持清理、构建、信息更新等功能
"""

import os
import sys
import shutil
import subprocess
import json
import argparse
import platform
from datetime import datetime
from pathlib import Path


class BuildManager:
    """构建管理器"""
    
    def __init__(self):
        self.project_root = Path(__file__).parent
        self.build_dir = self.project_root / "build"
        self.dist_dir = self.project_root / "dist"
        self.spec_file = self.project_root / "translator.spec"
        self.build_info_file = self.dist_dir / "build_info.json"
        self.version = "1.1"  # 构建脚本版本
        
    def print_status(self, message, status="INFO"):
        """打印状态信息"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        status_colors = {
            "INFO": "\033[94m",    # 蓝色
            "SUCCESS": "\033[92m", # 绿色
            "WARNING": "\033[93m", # 黄色
            "ERROR": "\033[91m",   # 红色
            "RESET": "\033[0m"     # 重置
        }
        
        color = status_colors.get(status, status_colors["INFO"])
        reset = status_colors["RESET"]
        print(f"{color}[{timestamp}] [{status}] {message}{reset}")
    
    def clean_build_dirs(self):
        """清理构建目录"""
        self.print_status("开始清理构建目录...")
        
        dirs_to_clean = [self.build_dir, self.dist_dir]
        
        for dir_path in dirs_to_clean:
            if dir_path.exists():
                try:
                    shutil.rmtree(dir_path)
                    self.print_status(f"已删除目录: {dir_path}", "SUCCESS")
                except Exception as e:
                    self.print_status(f"删除目录失败 {dir_path}: {e}", "ERROR")
                    return False
            else:
                self.print_status(f"目录不存在，跳过: {dir_path}", "WARNING")
        
        self.print_status("构建目录清理完成", "SUCCESS")
        return True
    
    def check_dependencies(self):
        """检查构建依赖"""
        self.print_status("检查构建依赖...")
        
        # 检查 PyInstaller
        try:
            result = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"], 
                                  capture_output=True, text=True, check=True)
            pyinstaller_version = result.stdout.strip()
            self.print_status(f"PyInstaller 版本: {pyinstaller_version}", "SUCCESS")
        except subprocess.CalledProcessError:
            self.print_status("PyInstaller 未安装或版本检查失败", "ERROR")
            return False
        except FileNotFoundError:
            self.print_status("Python 解释器未找到", "ERROR")
            return False
        
        # 检查 spec 文件
        if not self.spec_file.exists():
            self.print_status(f"规格文件不存在: {self.spec_file}", "ERROR")
            return False
        
        self.print_status("依赖检查完成", "SUCCESS")
        return True
    
    def run_pyinstaller(self):
        """运行 PyInstaller 构建"""
        self.print_status("开始 PyInstaller 构建...")
        
        # 构建命令
        cmd = [
            sys.executable, "-m", "PyInstaller",
            str(self.spec_file),
            "--clean",  # 清理临时文件
            "--noconfirm",  # 不询问覆盖
        ]
        
        self.print_status(f"执行命令: {' '.join(cmd)}")
        
        try:
            # 执行构建
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(self.project_root)
            )
            
            # 实时输出构建日志
            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    # 过滤和格式化输出
                    line = output.strip()
                    if line:
                        if "ERROR" in line.upper():
                            self.print_status(line, "ERROR")
                        elif "WARNING" in line.upper():
                            self.print_status(line, "WARNING")
                        elif any(keyword in line for keyword in ["INFO", "Building", "Analyzing"]):
                            self.print_status(line)
            
            # 检查构建结果
            return_code = process.poll()
            if return_code == 0:
                self.print_status("PyInstaller 构建完成", "SUCCESS")
                return True
            else:
                self.print_status(f"PyInstaller 构建失败，退出码: {return_code}", "ERROR")
                return False
                
        except Exception as e:
            self.print_status(f"构建过程中发生异常: {e}", "ERROR")
            return False
    
    def update_build_info(self):
        """更新构建信息文件"""
        self.print_status("更新构建信息...")
        
        # 确保 dist 目录存在
        self.dist_dir.mkdir(exist_ok=True)
        
        # 获取 PyInstaller 版本
        try:
            result = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"], 
                                  capture_output=True, text=True, check=True)
            pyinstaller_version = result.stdout.strip()
        except:
            pyinstaller_version = "unknown"
        
        # 构建信息
        build_info = {
            "build_time": datetime.now().isoformat(),
            "python_version": sys.version,
            "platform": platform.system().lower(),
            "build_script_version": self.version,
            "pyinstaller_version": pyinstaller_version,
            "target_architecture": platform.machine().lower(),
            "working_directory": str(self.project_root),
            "dist_directory": str(self.dist_dir)
        }
        
        try:
            with open(self.build_info_file, 'w', encoding='utf-8') as f:
                json.dump(build_info, f, indent=2, ensure_ascii=False)
            
            self.print_status(f"构建信息已更新: {self.build_info_file}", "SUCCESS")
            return True
            
        except Exception as e:
            self.print_status(f"更新构建信息失败: {e}", "ERROR")
            return False
    
    def verify_build_result(self):
        """验证构建结果"""
        self.print_status("验证构建结果...")
        
        # 查找生成的可执行文件
        exe_files = list(self.dist_dir.glob("*.exe"))
        
        if not exe_files:
            self.print_status("未找到生成的可执行文件", "ERROR")
            return False
        
        # 显示生成的文件信息
        self.print_status("构建成功！生成的文件:", "SUCCESS")
        
        for exe_file in exe_files:
            file_size = exe_file.stat().st_size
            file_size_mb = file_size / (1024 * 1024)
            self.print_status(f"  📁 {exe_file.name} ({file_size_mb:.1f} MB)")
        
        # 显示其他重要文件
        other_files = []
        for pattern in ["*.json", "config/*", "assets/*"]:
            other_files.extend(self.dist_dir.glob(pattern))
        
        if other_files:
            self.print_status("其他文件:")
            for file_path in other_files:
                if file_path.is_file():
                    self.print_status(f"  📄 {file_path.relative_to(self.dist_dir)}")
                elif file_path.is_dir():
                    self.print_status(f"  📁 {file_path.relative_to(self.dist_dir)}/")
        
        return True
    
    def build(self, clean=True):
        """执行完整构建流程"""
        self.print_status("=" * 60)
        self.print_status("轻小说翻译器V1.1 - 自动化构建", "INFO")
        self.print_status("=" * 60)
        
        # 1. 清理构建目录（可选）
        if clean:
            if not self.clean_build_dirs():
                return False
        
        # 2. 检查依赖
        if not self.check_dependencies():
            return False
        
        # 3. 运行 PyInstaller
        if not self.run_pyinstaller():
            return False
        
        # 4. 更新构建信息
        if not self.update_build_info():
            return False
        
        # 5. 验证构建结果
        if not self.verify_build_result():
            return False
        
        self.print_status("=" * 60)
        self.print_status("构建流程全部完成！", "SUCCESS")
        self.print_status("=" * 60)
        
        return True


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="轻小说翻译器V1.1自动化构建脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python build.py                    # 完整构建（包含清理）
  python build.py --no-clean         # 构建但不清理旧文件
  python build.py --clean-only       # 仅清理构建目录
  python build.py --check-deps       # 仅检查依赖
        """
    )
    
    parser.add_argument(
        "--no-clean", 
        action="store_true", 
        help="构建时不清理旧的构建文件"
    )
    
    parser.add_argument(
        "--clean-only", 
        action="store_true", 
        help="仅清理构建目录，不执行构建"
    )
    
    parser.add_argument(
        "--check-deps", 
        action="store_true", 
        help="仅检查构建依赖"
    )
    
    args = parser.parse_args()
    
    # 创建构建管理器
    builder = BuildManager()
    
    try:
        # 根据参数执行不同操作
        if args.clean_only:
            success = builder.clean_build_dirs()
        elif args.check_deps:
            success = builder.check_dependencies()
        else:
            # 完整构建流程
            clean = not args.no_clean
            success = builder.build(clean=clean)
        
        # 退出码
        sys.exit(0 if success else 1)
        
    except KeyboardInterrupt:
        builder.print_status("构建被用户中断", "WARNING")
        sys.exit(1)
    except Exception as e:
        builder.print_status(f"构建过程中发生未预期的错误: {e}", "ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()