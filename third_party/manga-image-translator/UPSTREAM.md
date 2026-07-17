# manga-image-translator 上游来源说明

## 项目信息

- **名称**: manga-image-translator
- **仓库**: https://github.com/zyddnys/manga-image-translator
- **许可证**: GPL-3.0-only (见 LICENSE 和 THIRD_PARTY_NOTICES.md)
- **用途**: 本项目 AI-translater-1.4 将其作为图片翻译的默认 Manga Provider 实现，
  通过惰性导入方式集成，由 `src/infrastructure/image_translation/manga_provider.py`
  封装其检测/OCR/翻译/擦除/渲染流水线。

## 引入版本

- **快照来源**: 官方 GitHub 仓库 main 分支
- **快照日期**: 2026-07-16
- **commit / tag**: 不可用（提供的本地快照不含 `.git` 元数据）
- **本地路径**: `D:\manga-image-translator-main\manga_translator\`
- **vendor 目标路径**: `third_party/manga-image-translator/manga_translator/`
- **快照完整性**: 见 `SOURCE_SHA256.txt`

## Vendor 流程

1. 从上游克隆最新源码：
   ```bash
   git clone https://github.com/zyddnys/manga-image-translator.git
   cd manga-image-translator
   git rev-parse HEAD  # 记录 commit hash 到本文件
   ```

2. 计算源码快照 SHA-256（用于完整性校验）：
   ```powershell
   Get-ChildItem -Recurse manga_translator | `
     Where-Object { -not $_.PSIsContainer } | `
     ForEach-Object { (Get-FileHash $_.FullName -Algorithm SHA256).Hash + "  " + $_.Name } | `
     Out-File third_party\manga-image-translator\SOURCE_SHA256.txt
   ```

3. 复制源码到 vendor 目录：
   ```powershell
   Copy-Item -Recurse D:\manga-image-translator-main\manga_translator `
     third_party\manga-image-translator\manga_translator
   ```

4. 更新本文件的 commit hash 与快照日期。

5. 在 `third_party/manga-image-translator/LOCAL_CHANGES.md` 记录任何本地修改。

## 复现性保证

- vendor 后禁止在 `third_party/manga-image-translator/` 目录内直接修改源码。
- 任何 bug 修复或适配必须先在 `LOCAL_CHANGES.md` 中记录修改文件、修改原因、
  对应的 upstream issue/PR（如有）。
- 每次升级上游版本必须重新计算 SOURCE_SHA256.txt，并在本文件记录新版本信息。

## 升级策略

- 跟踪上游 release 而非 main 分支，避免引入未稳定改动。
- 升级前先运行 `tests/test_image_translation_module.py` 确保契约不破坏。
- 若上游 API 变更（如 `MangaTranslator.translate()` 签名变化），
  必须同步更新 `src/infrastructure/image_translation/manga_provider.py` 的封装代码。
