# 第三方许可声明

本文件汇总本项目（AI-translater-1.4）所使用的第三方软件许可。

## 1. manga-image-translator

- **用途**: 图片翻译默认 Manga Provider 的核心实现
- **仓库**: https://github.com/zyddnys/manga-image-translator
- **许可证**: GNU General Public License v3.0 only (GPL-3.0-only)
- **引入路径**: `third_party/manga-image-translator/`

完整 GPL-3.0 文本随源码快照保存在本目录 `LICENSE`。

## 2. 间接依赖（随 manga-image-translator 引入）

以下依赖通过 `requirements-image-manga.txt` 安装，各自遵循其许可证：

| 依赖 | 许可证 | 用途 |
|---|---|---|
| torch | BSD-3-Clause | 深度学习框架 |
| torchvision | BSD-3-Clause | 图像模型工具 |
| onnxruntime | MIT License | ONNX 模型推理 |
| opencv-python | Apache 2.0 | 计算机视觉 |
| numpy | BSD-3-Clause | 数值计算 |
| Pillow | HPND License | 图像处理 |
| huggingface-hub | Apache 2.0 | 模型下载 |
| safetensors | Apache 2.0 | 模型权重加载 |
| tokenizers | Apache 2.0 | 分词器 |
| transformers | Apache 2.0 | NLP 模型（可选） |

## 3. 分发义务

- 本项目分发时必须随附本 THIRD_PARTY_NOTICES.md。
- 必须随附 manga-image-translator 的 GPL-3.0 许可证文本和对应源码。
- 必须随附 vendor 的 manga_translator 源码或可获取源码的明确指引（见 UPSTREAM.md）。
- 间接依赖的许可证由各自发行版负责，本项目仅需在文档中列出所用依赖列表。

## 4. 许可证兼容性

- manga-image-translator 为 GPL-3.0-only。分发包含该 Provider 的程序前，必须确认
  整体项目的许可证与源码提供方式满足 GPL-3.0 的要求。
