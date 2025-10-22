# 轻小说翻译器 V1.1

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

- Python 3.11+
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

我们提供了预编译的可执行文件，无需安装Python环境即可使用：

- **Windows**: `轻小说翻译器V1.1-Windows-x64.exe`
- **macOS**: `轻小说翻译器V1.1-macOS-Universal.app`
- **Linux**: `轻小说翻译器V1.1-Linux-x64`

从 [Releases](https://github.com/haixiudenailaos/AI-translater/releases) 页面下载对应平台的版本。

## 🔧 配置说明

### API配置

1. 复制 `config/api_config_sample.json` 为 `config/api_config.json`
2. 填入您的API密钥：

```json
{
  "deepseek": {
    "api_key": "your_deepseek_api_key",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat"
  },
  "siliconflow": {
    "api_key": "your_siliconflow_api_key",
    "base_url": "https://api.siliconflow.cn/v1",
    "model": "deepseek-ai/DeepSeek-V2.5"
  }
}
```

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
轻小说翻译原代码V1.1/
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
# 安装PyInstaller
pip install pyinstaller

# 构建可执行文件
pyinstaller translator.spec
```

### 自动化构建

项目配置了GitHub Actions自动化构建，支持：

- ✅ 跨平台构建（Windows/macOS/Linux）
- ✅ 自动发布Release
- ✅ 构建产物上传

每次推送代码或创建标签时会自动触发构建。

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