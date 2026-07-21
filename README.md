# 轻小说翻译器 V1.6

一个基于Python和tkinter的轻小说翻译工具，支持EPUB格式文件的批量翻译处理。

## ✨ 主要功能

- 📚 **EPUB文件支持**: 完整的EPUB格式文件读取、处理和保存
- 🌐 **多API支持**: 集成DeepSeek和SiliconFlow翻译API
- 🔄 **批量处理**: 支持多文件批量翻译
- 💾 **智能缓存**: 避免重复翻译，提高效率
- 📖 **术语表管理**: 自定义术语翻译，保持翻译一致性
- 🎨 **图形界面**: 直观的tkinter GUI界面
- ⚙️ **配置管理**: 灵活的API配置和应用设置

## 🚀 快速开始

### 环境要求

- Python 3.10 及以上
- Windows/macOS/Linux

### 安装依赖

```bash
pip install -r requirements.txt
```

### 运行程序

```bash
python main.py
```

## 📦 预编译版本

当前 CI 只构建 **Text Edition**：包含 TXT/EPUB 文本翻译和在线图片翻译 Provider，不包含本地 Manga 推理依赖。

- **Windows**: 单文件 `LightNovelTranslator-1.6-Windows-x64.exe`。
- **macOS**: `LightNovelTranslator-1.6-macOS.app.zip`，解压后双击 `.app`。

Full Edition 仍保留 `translator.spec` 供本地构建，包含本地漫画图片翻译的深度学习栈；它尚未由 CI 发布。Text 产物会作为 V1.6 分支推送和 Pull Request 的 GitHub Actions artifact 保存 30 天。

> macOS 产物为当前 `macos-latest` runner 的架构，不是 Universal 二进制。

## 🔧 配置说明

### API 配置

**推荐方式（安全）**：启动程序后在「设置」窗口中选择 API 提供商并填入密钥。
密钥通过系统密钥环（keyring）加密存储，不会写入仓库配置文件，也不会以明文落盘。

如需在仓库中保留一份示例配置（**不要填入真实密钥**），可复制示例：

1. 复制 `config/api_config_sample.json` 为 `config/api_config.json`
2. 保留占位符，**不要填入真实密钥**——真实密钥请通过「设置」窗口写入 keyring：

```json
{
  "provider": "siliconflow",
  "model_name": "deepseek-ai/DeepSeek-V3.2",
  "base_url": "https://api.siliconflow.cn/v1",
  "temperature": 0.3
}
```

> ⚠️ 把真实 API 密钥写入 `config/api_config.json` 并提交到仓库会导致密钥泄露，
> 请始终通过「设置」窗口（keyring）管理密钥。

### 术语表配置

1. 复制 `config/glossary_sample.json` 为 `config/glossary.json`
2. 添加自定义术语翻译对：

```json
{
  "专有名词": "Proper Noun",
  "角色名": "Character Name"
}
```

## 📖 使用指南

### 基本翻译流程

1. **启动程序**: 运行 `main.py` 或双击可执行文件
2. **选择文件**: 点击"选择文件"按钮，选择要翻译的EPUB文件
3. **配置设置**: 
   - 选择翻译API（DeepSeek或SiliconFlow）
   - 设置目标语言
   - 调整翻译参数
4. **开始翻译**: 点击"开始翻译"按钮
5. **保存结果**: 翻译完成后保存输出文件

### 高级功能

- **批量处理**: 在设置中启用批量模式，可同时处理多个文件
- **术语管理**: 使用术语表窗口管理专有名词翻译
- **缓存管理**: 智能缓存避免重复翻译相同内容

## 🏗️ 项目结构

```
LightNovelTranslator-V1.6/
├── main.py                 # 主程序入口
├── requirements.txt        # Python依赖
├── translator.spec         # PyInstaller配置
├── src/                    # 源代码目录
│   ├── api/               # API接口模块
│   ├── core/              # 核心功能模块
│   ├── ui/                # 用户界面模块
│   ├── config/            # 配置管理模块
│   └── utils/             # 工具函数模块
├── config/                # 配置文件目录
├── hooks/                 # PyInstaller钩子
├── tools/                 # 辅助工具
└── .github/workflows/     # GitHub Actions配置
```

## 🔨 开发构建

### 本地构建

```bash
# 安装项目、开发工具和 PyInstaller（开发工具包含 Ruff、Pyright、pytest、build、pip-tools）
pip install -e ".[dev]"
pip install "pyinstaller==6.11.1"

# 构建 Text Edition（仅文本翻译，体积小）
pyinstaller translator_text.spec --clean --noconfirm

# 构建 Full Edition（含漫画图片翻译依赖）
pyinstaller translator.spec --clean --noconfirm
```

Text spec 生成单文件 `dist/LightNovelTranslatorV1.6[.exe]`；Full spec 生成 onedir 目录 `dist/LightNovelTranslatorV1.6/`。

Windows Text 发布构建使用 Python 3.10 和 `requirements-text-win-py310.lock.txt`。修改发布依赖后，必须在对应平台与 Python 版本执行 `python tools/generate_dependency_locks.py <target>`，并提交更新后的 hash lock。

### 自动化构建

项目配置了 GitHub Actions 自动化构建，支持：

- ✅ Python 3.10-3.13 的 Ruff、格式、编译和 pytest 质量矩阵
- ✅ Python 3.11 覆盖率报告、Pyright 迁移报告和 wheel 安装 smoke
- ✅ Windows/macOS 的 Text Edition 构建与启动 smoke

V1.6 分支推送、Pull Request 和手动触发都会执行工作流。Pyright 目前是迁移报告，待存量类型错误清零后升级为阻断门禁。

## 🤝 贡献指南

欢迎提交Issue和Pull Request！

1. Fork本仓库
2. 创建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 创建Pull Request

## 📄 许可证

本项目采用MIT许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

## 🙏 致谢

- [tkinter](https://docs.python.org/3/library/tkinter.html) - GUI框架
- [ebooklib](https://github.com/aerkalov/ebooklib) - EPUB文件处理
- [httpx](https://github.com/encode/httpx) - HTTP客户端
- [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) - HTML解析
- [PyInstaller](https://github.com/pyinstaller/pyinstaller) - 打包工具

## 📞 联系方式

如有问题或建议，请通过以下方式联系：

- 提交 [Issue](https://github.com/haixiudenailaos/AI-translater/issues)
- 发起 [Discussion](https://github.com/haixiudenailaos/AI-translater/discussions)

---

⭐ 如果这个项目对您有帮助，请给个Star支持一下！
