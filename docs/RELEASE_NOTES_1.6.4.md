# LightNovelTranslator 1.6.4

## Highlights

- Added bilingual EPUB export (「导出双语 EPUB」): original and translated paragraphs are interleaved while preserving inline formatting, headings, lists, captions, and images.
- Added selective bilingual export for translated paragraphs only, plus a dedicated default filename (`*_双语对照.epub`).
- Added a compact word count indicator at the bottom-left status bar, counting non-whitespace characters for both source and translation, refreshed at most once every 300 ms.
- Added output path validation for bilingual export so the source EPUB, the current session target, and project mapping directories cannot be overwritten.
- Fixed a shared export-slot race where a finished bilingual export job could be replaced by a normal save/export before its result was consumed, losing success or failure notifications.
- Fixed mapping snapshot reads falsely reporting "mapping update in progress" when a stable manifest referenced a permanently missing generation file.

## Downloads

- Windows: `LightNovelTranslator-1.6.4-Windows-x64.exe`
- macOS: `LightNovelTranslator-1.6.4-macOS.app.zip`
