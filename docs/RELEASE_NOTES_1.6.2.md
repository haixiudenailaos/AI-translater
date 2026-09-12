# LightNovelTranslator 1.6.2

## Highlights

- Added ultra-long-context translation for the main editor and batch translation queue.
- Added a per-run context budget with model-capacity, output-capacity, and provider-limit safeguards.
- Preserved long-context settings in project snapshots, including retry and resume flows.
- Disabled DeepSeek V4's default reasoning mode for translation requests to reduce first-token delays.
- Updated the official DeepSeek model identifier to `deepseek-flash`; existing `deepseek-v4-flash` settings are migrated automatically.
- Restored the Ctrl+Shift+D diagnostic shortcut across Windows keyboard state variations.

## Downloads

- Windows: `LightNovelTranslator-1.6.2-Windows-x64.exe`
- macOS: `LightNovelTranslator-1.6.2-macOS.app.zip`
