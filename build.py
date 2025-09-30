#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
优化版打包脚本
使用PyInstaller将Python应用打包为exe文件
确保包含所有依赖和项目文件
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path
import time

def _is_package(dir_path: Path) -> bool:
    return dir_path.is_dir() and (dir_path / "__init__.py").exists()

def discover_hidden_imports() -> list:
    """自动扫描 src 目录，生成隐藏导入列表"""
    hidden = []
    src_dir = Path("src")
    if not src_dir.exists():
        return hidden
    for root, dirs, files in os.walk(src_dir):
        root_path = Path(root)
        # 包路径
        if _is_package(root_path):
            pkg = ".".join(root_path.parts).replace("\\", ".").replace("/", ".")
            hidden.append(pkg)
        # 模块文件
        for f in files:
            if f.endswith(".py") and f != "__init__.py":
                mod_path = root_path / f
                mod = ".".join(mod_path.with_suffix("").parts).replace("\\", ".").replace("/", ".")
                hidden.append(mod)
    # 去重并排序，优先短路径
    return sorted(set(hidden), key=lambda x: (x.count("."), x))

def discover_add_data() -> list:
    """动态收集需要打包的数据目录"""
    add_data = []
    # 始终包含 config 和 src
    if Path("config").exists():
        add_data.append(("config", "config"))
    if Path("src").exists():
        add_data.append(("src", "src"))
    # assets 只有在存在非空文件时才包含
    assets_dir = Path("assets")
    if assets_dir.exists():
        has_files = any(p.is_file() for p in assets_dir.rglob("*"))
        if has_files:
            add_data.append(("assets", "assets"))
    return add_data

def determine_entry_script() -> str:
    """确定入口脚本"""
    if Path("main.py").exists():
        return "main.py"
    fallback = Path("src/ui/main_window.py")
    return str(fallback) if fallback.exists() else "main.py"

def check_dependencies():
    """检查必要的依赖是否已安装（合并 requirements.txt 与内置清单）"""
    print("检查依赖...")
    missing_deps = []
    
    required_deps = {
        'httpx': 'httpx>=0.24.0',
        'chardet': 'chardet>=5.0.0',
        'PyInstaller': 'pyinstaller>=5.0'
    }
    
    # 读取 requirements.txt（若存在）
    req_file = Path("requirements.txt")
    if req_file.exists():
        try:
            for line in req_file.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 提取包名（去掉版本约束）
                pkg = line.split("==")[0].split(">=")[0].split("<=")[0].strip()
                if pkg:
                    import_name = pkg.lower()
                    required_deps.setdefault(import_name, line)
        except Exception as e:
            print(f"⚠ 读取 requirements.txt 失败: {e}")
    
    # 检测安装情况
    for dep, version_info in required_deps.items():
        try:
            __import__(dep if dep != 'pyinstaller' else 'PyInstaller')
            print(f"✓ {version_info} 已安装")
        except ImportError:
            missing_deps.append(version_info)
            print(f"✗ 未安装: {version_info}")
    
    if missing_deps:
        print(f"\n缺少以下依赖: {', '.join(missing_deps)}")
        print("请运行: pip install -r requirements.txt")
        return False
    
    return True

def prepare_build_environment():
    """准备构建环境"""
    print("准备构建环境...")
    
    # 确保config目录存在
    config_dir = Path("config")
    if not config_dir.exists():
        print("创建config目录...")
        config_dir.mkdir(exist_ok=True)
        
        # 创建默认配置文件
        default_configs = {
            "api_config.json": {
                "provider": "siliconflow",
                "api_key": "",
                "model_name": "deepseek-ai/DeepSeek-V3.1",
                "base_url": "https://api.siliconflow.cn/v1",
                "max_tokens": 4000,
                "temperature": 0.3
            },
            "app_config.json": {
                "target_language": "中文",
                "context_lines": 2,
                "chunk_size": 1000,
                "auto_save": True
            },
            "glossary.json": {
                "terms": [],
                "categories": ["通用", "技术", "专业"]
            }
        }
        
        import json
        for filename, content in default_configs.items():
            config_file = config_dir / filename
            if not config_file.exists():
                with open(config_file, 'w', encoding='utf-8') as f:
                    json.dump(content, f, ensure_ascii=False, indent=2)
                print(f"创建默认配置文件: {filename}")

def build_exe():
    """构建exe文件"""
    print("开始构建exe文件...")
    entry = determine_entry_script()
    print(f"入口脚本: {entry}")
    
    # 基础参数
    cmd = [
        "pyinstaller",
        "--onefile",                           # 打包成单个exe文件
        "--windowed",                          # 不显示控制台窗口
        "--name=轻量级翻译工具",                 # 设置exe文件名
        "--clean",                             # 清理临时文件
        "--noconfirm",                         # 不询问覆盖
    ]
    
    # 动态数据文件包含
    for src_path, dst_path in discover_add_data():
        cmd.append(f"--add-data={src_path};{dst_path}")
    
    # 核心依赖的隐藏导入（GUI/网络/编码）
    base_hidden = [
        "tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog", "tkinter.simpledialog",
        "httpx", "httpx._client", "httpx._config", "httpx._models",
        "chardet", "chardet.universaldetector",
        "json", "threading", "pathlib", "concurrent.futures", "hashlib", "datetime", "shutil", "re", "time",
    ]
    for mod in base_hidden:
        cmd.append(f"--hidden-import={mod}")
    
    # 项目模块隐藏导入（自动扫描）
    for mod in discover_hidden_imports():
        cmd.append(f"--hidden-import={mod}")
    
    # 排除不需要的模块以减小文件大小
    excludes = ["matplotlib", "numpy", "pandas", "scipy", "PIL"]
    for m in excludes:
        cmd.append(f"--exclude-module={m}")
    
    # 如果图标文件存在，添加图标参数
    if os.path.exists("assets/icon.ico"):
        cmd.append("--icon=assets/icon.ico")
        print("使用自定义图标")
    
    # 入口脚本
    cmd.append(entry)
    
    try:
        print("执行PyInstaller命令...")
        print(f"命令: {' '.join(cmd[:8])} ... (共{len(cmd)}个参数)")
        
        # 使用python -m PyInstaller而不是直接调用pyinstaller
        cmd[0] = sys.executable
        cmd.insert(1, "-m")
        cmd.insert(2, "PyInstaller")
        
        # 执行打包命令
        start_time = time.time()
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, encoding='utf-8')
        end_time = time.time()
        
        print(f"✓ 打包成功！耗时: {end_time - start_time:.1f}秒")
        
        # 显示关键输出信息
        if result.stdout:
            lines = result.stdout.split('\n')
            important_lines = [line for line in lines if any(keyword in line.lower() 
                             for keyword in ['warning', 'error', 'successfully', 'building', 'completed'])]
            if important_lines:
                print("构建信息:")
                for line in important_lines[-10:]:  # 显示最后10行重要信息
                    print(f"  {line}")
        
        # 检查生成的文件
        return check_build_result()
        
    except subprocess.CalledProcessError as e:
        print(f"✗ 打包失败: {e}")
        if e.stderr:
            print("错误详情:")
            error_lines = e.stderr.split('\n')
            for line in error_lines[-20:]:  # 显示最后20行错误信息
                if line.strip():
                    print(f"  {line}")
        return False
    except Exception as e:
        print(f"✗ 打包过程中出现异常: {e}")
        return False

def check_build_result():
    """检查构建结果"""
    print("检查构建结果...")
    
    dist_dir = Path("dist")
    if not dist_dir.exists():
        print("✗ dist目录不存在")
        return False
    
    exe_files = list(dist_dir.glob("*.exe"))
    if not exe_files:
        print("✗ 未找到生成的exe文件")
        return False
    
    exe_file = exe_files[0]
    file_size = exe_file.stat().st_size / (1024 * 1024)  # MB
    
    print(f"✓ 生成的exe文件: {exe_file}")
    print(f"✓ 文件大小: {file_size:.1f} MB")
    
    # 检查文件是否可执行
    if exe_file.suffix.lower() == '.exe':
        print("✓ exe文件格式正确")
        return True
    else:
        print("✗ 生成的文件格式不正确")
        return False

def clean_build():
    """清理构建文件"""
    print("清理构建文件...")
    
    import shutil
    
    # 删除构建目录
    dirs_to_remove = ["build", "dist", "__pycache__"]
    for dir_name in dirs_to_remove:
        if os.path.exists(dir_name):
            shutil.rmtree(dir_name)
            print(f"已删除: {dir_name}")
    
    # 删除spec文件
    spec_files = list(Path(".").glob("*.spec"))
    for spec_file in spec_files:
        spec_file.unlink()
        print(f"已删除: {spec_file}")

def create_build_info():
    """创建构建信息文件"""
    try:
        import json
        from datetime import datetime
        
        build_info = {
            "build_time": datetime.now().isoformat(),
            "python_version": sys.version,
            "platform": sys.platform,
            "build_script_version": "2.0"
        }
        
        with open("dist/build_info.json", 'w', encoding='utf-8') as f:
            json.dump(build_info, f, ensure_ascii=False, indent=2)
        print("✓ 创建构建信息文件")
    except Exception as e:
        print(f"⚠ 创建构建信息文件失败: {e}")

def main():
    """主函数"""
    print("=" * 60)
    print("轻量级翻译工具 - 优化版打包脚本 v2.0")
    print("=" * 60)
    
    # 处理命令行参数
    if len(sys.argv) > 1:
        if sys.argv[1] == "clean":
            clean_build()
            return
        elif sys.argv[1] == "help" or sys.argv[1] == "-h":
            print("使用方法:")
            print("  python build.py        - 构建exe文件")
            print("  python build.py clean  - 清理构建文件")
            print("  python build.py help   - 显示帮助信息")
            return
    
    try:
        # 步骤1: 检查依赖
        print("\n[1/5] 检查依赖...")
        if not check_dependencies():
            print("✗ 依赖检查失败，请安装缺失的依赖后重试")
            return
        
        # 步骤2: 准备构建环境
        print("\n[2/5] 准备构建环境...")
        prepare_build_environment()
        
        # 步骤3: 清理旧的构建文件
        print("\n[3/5] 清理旧的构建文件...")
        clean_build()
        
        # 步骤4: 执行构建
        print("\n[4/5] 执行构建...")
        if not build_exe():
            print("✗ 构建失败！请检查错误信息")
            return
        
        # 步骤5: 创建构建信息
        print("\n[5/5] 创建构建信息...")
        create_build_info()
        
        print("\n" + "=" * 60)
        print("🎉 构建完成！")
        print("📁 exe文件位于 dist 目录中")
        print("💡 提示: 生成的exe文件可以独立运行，无需安装Python环境")
        print("=" * 60)
        
    except KeyboardInterrupt:
        print("\n⚠ 构建被用户中断")
    except Exception as e:
        print(f"\n✗ 构建过程中出现未预期的错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()