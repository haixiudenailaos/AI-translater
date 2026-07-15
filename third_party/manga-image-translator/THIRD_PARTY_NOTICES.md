# 第三方许可声明

本文件汇总本项目（AI-translater-1.4）所使用的第三方软件许可。

## 1. manga-image-translator

- **用途**: 图片翻译默认 Manga Provider 的核心实现
- **仓库**: https://github.com/zyddnys/manga-image-translator
- **许可证**: MIT License
- **引入路径**: `third_party/manga-image-translator/`

### MIT License (manga-image-translator)

```
MIT License

Copyright (c) 2024 zyddnys

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

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
| ultralytics | AGPL-3.0 | YOLO 检测（可选） |

### AGPL-3.0 注意事项

`ultralytics` 采用 AGPL-3.0 许可证。本项目仅将其作为 manga-image-translator
CTD detector 的可选依赖使用，且通过 `requirements-image-manga.txt` 显式声明。
若分发闭源商业版本，请：

1. 改用 default detector（不依赖 ultralytics）
2. 或单独评估 AGPL-3.0 的传染性影响

## 3. 分发义务

- 本项目分发时必须随附本 THIRD_PARTY_NOTICES.md。
- 必须随附 manga-image-translator 的 MIT 许可证文本（见第 1 节）。
- 必须随附 vendor 的 manga_translator 源码或可获取源码的明确指引（见 UPSTREAM.md）。
- 间接依赖的许可证由各自发行版负责，本项目仅需在文档中列出所用依赖列表。

## 4. 许可证兼容性

- 本项目主许可证未定，但所有引入的依赖许可均允许商用与修改。
- AGPL-3.0 的 ultralytics 仅在显式启用 CTD detector 时加载，未启用时不影响其他模块。
