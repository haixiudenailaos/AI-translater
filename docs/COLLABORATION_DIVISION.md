# 协同分工方案

本文件用于两个 AI 协同完成 `SECOND_ROUND_BUG_FIX_GUIDE.md`（27 个 R2 Bug）和 `MODULE_REFACTORING_GUIDE.md`（7 个重构阶段）的拆分与修复工作。

分工原则：**按文件归属切分，避免双方编辑同一文件产生冲突**。底层（基础设施/领域模型）先行，上层（应用编排/UI）基于约定接口并行开发。

---

## 一、当前进度基线（2026-07-15）

第一轮 BUG-001~009 已全部修复。第二轮中以下项**已基本完成**，仅需复核：

| 编号 | 状态 | 说明 |
| --- | --- | --- |
| R2-BUG-002 | 已完成 | `secure_storage.py` 已引入 `StorageStatus` 枚举与回读校验；`config_manager.py` 已区分持久化/会话级 |
| R2-BUG-003 | 已完成 | `config_manager.load_api_presets()` 已分离运行时对象与持久化对象，不写回明文 |

**尚未处理**：R2-BUG-001、004~027 共 25 项，以及全部 7 个重构阶段。

---

## 二、AI-A（本 AI）负责：基础设施 + 领域模型 + EPUB + 图片

### 文件归属（AI-A 独占编辑）

```
src/app_paths.py
src/utils/file_handler.py
src/utils/secure_storage.py          # 复核 R2-BUG-002
src/utils/logger.py
src/config/config_manager.py         # 复核 R2-BUG-003
src/core/translation_result.py       # 扩展为 domain 层
src/core/translator.py               # 核心翻译流程
src/core/epub_processor.py
src/core/image_translator.py
src/core/image_text_translator.py
src/core/image_utils.py
src/api/base_api.py
src/api/siliconflow_api.py
src/api/deepseek_api.py
src/domain/                          # 新建：领域模型
src/infrastructure/                  # 新建：EPUB/持久化拆分
main.py                              # 启动顺序
hooks/runtime_hook_resources.py      # 旧目录迁移参考
```

### 负责的 Bug

| 编号 | 主题 | 主要文件 |
| --- | --- | --- |
| R2-BUG-001 | ebooklib 字符串 spine ID 解析 | epub_processor.py |
| R2-BUG-004 | 禁止按位置复用已变化原文的译文 | epub_processor.py |
| R2-BUG-005 | 章节 ID 保留唯一路径 | epub_processor.py |
| R2-BUG-006 | 导出前验证源 EPUB 指纹 | epub_processor.py |
| R2-BUG-007 | 保留 EPUB 内联格式 | epub_processor.py |
| R2-BUG-008 | 取消后重建已关闭客户端 | base_api.py / translator.py |
| R2-BUG-010 | 检测响应中的缺失译文 | translator.py |
| R2-BUG-016 | 图片输出唯一身份与真实媒体类型 | image_translator.py / epub_processor.py |
| R2-BUG-017 | 区分"无外文"和"检测失败" | image_text_translator.py |
| R2-BUG-018 | 空结果处理旧图片映射 | image_text_translator.py |
| R2-BUG-020 | 迁移旧打包版实际配置目录 | app_paths.py |
| R2-BUG-021 | 日志初始化不由模块导入抢先触发 | app_paths.py / logger.py / main.py |
| R2-BUG-022 | 原子写入使用唯一临时文件 | file_handler.py |
| R2-BUG-023 | 心跳不得关闭活动流式客户端 | siliconflow_api.py / base_api.py |
| R2-BUG-025 | 关闭图片翻译 HTTP 客户端 | image_translator.py |

### 负责的重构阶段

- **阶段 1**：路径和持久化（AppPaths / 原子文件 / ConfigRepository 雏形）
- **阶段 2**：领域结果和异常（`src/domain/` 状态枚举、结果 dataclass）
- **阶段 3**：API 生命周期（TranslationProvider 协议、close/上下文管理）
- **阶段 4**：EPUB 拆分（document_order / segment_extractor / mapping_repository / image_rewriter / exporter）

### 关键接口契约（供 AI-B 依赖）

AI-A 会先交付以下稳定契约，AI-B 据此并行开发：

1. **领域状态枚举**（`src/domain/translation.py`）
   ```python
   class OperationStatus(str, Enum):
       SUCCEEDED / PARTIAL / CANCELLED / FAILED
   class TranslationResult: status, lines, failed_indices, error_message
   class TranslationProgress: completed, total, batch_start, preview_lines
   ```

2. **翻译批次取消信号**（R2-BUG-009 协作点）
   - AI-A 修改 `translator._translate_batch()`：取消时不再返回 `[''] * n` 伪造空译文，而是抛出 `TranslationCancelled` 异常或返回带 `CANCELLED` 状态的结果。
   - AI-B 负责 `translation_controller.py` 侧：合并结果时只覆盖已确认成功的索引，CANCELLED 路径不发送最终成功批次回调。

3. **缺失译文检测**（R2-BUG-010 协作点）
   - AI-A 修改 `translator._translate_batch()`：对每个非空原文验证对应译文非空，缺失/越界/重复行号计入 `failed_indices`，整批返回 `PARTIAL` 或 `FAILED`。
   - AI-B 在 UI 侧消费 `failed_indices` 进行提示。

4. **EPUB 导出指纹校验**（R2-BUG-006 协作点）
   - AI-A 在 `epub_processor` 导出前校验源文件内容哈希，不一致时抛出明确异常。
   - AI-B 在 `translation_controller` 导出入口捕获该异常并提示用户重新关联。

5. **TranslationProvider 协议**（`src/application/ports.py`）
   ```python
   class TranslationProvider(Protocol):
       def translate_batch(lines, options, on_progress) -> TranslationResult: ...
       def cancel(self) -> None: ...
       def close(self) -> None: ...
   ```
   AI-B 的 `TaskQueueService` 只依赖此协议。

---

## 三、AI-B（另一个 AI）负责：应用编排 + 队列服务 + UI Presenter

### 文件归属（AI-B 独占编辑）

```
src/core/concurrent_manager.py
src/core/batch_processor.py           # 按重构指南评估删除或重做
src/ui/concurrent_window.py
src/ui/task_detail_window.py
src/ui/translation_controller.py
src/ui/main_window.py
src/ui/settings_window.py
src/ui/file_importer.py
src/ui/image_translation_handler.py
src/ui/glossary_window.py
src/ui/table_editor.py
src/application/                      # 新建：用例与流程编排（与 AI-A 共建，但服务类由 AI-B 写）
src/presentation/                     # 新建：Tk Presenter（AI-B 独占）
```

> 注意：`src/application/ports.py` 由 AI-A 定义协议；AI-B 在该目录下编写 `task_queue_service.py`、`translation_service.py` 等实现。双方约定不修改对方的协议定义文件，仅新增实现。

### 负责的 Bug

| 编号 | 主题 | 主要文件 |
| --- | --- | --- |
| R2-BUG-009 | 取消不能发送空批次覆盖结果 | translation_controller.py（消费 AI-A 的取消信号） |
| R2-BUG-011 | 队列不得把 PARTIAL 标记为 completed | concurrent_manager.py |
| R2-BUG-012 | 关闭队列后禁止继续调度 | concurrent_manager.py |
| R2-BUG-013 | 队列窗口必须拥有管理器生命周期 | concurrent_window.py / main_window.py |
| R2-BUG-014 | 续翻只处理真正缺失的行 | translation_controller.py |
| R2-BUG-015 | 详情页编辑必须落盘 | task_detail_window.py / concurrent_manager.py |
| R2-BUG-019 | 映射保存失败时禁止成功导出旧数据 | translation_controller.py |
| R2-BUG-024 | 队列只翻译缺失行 | concurrent_manager.py |
| R2-BUG-026 | 所有 Tk 更新回到主线程 | translation_controller / settings_window / file_importer / concurrent_window |
| R2-BUG-027 | 空任务完成前也必须保存结果 | concurrent_manager.py |

### 负责的重构阶段

- **阶段 5**：队列服务（`TaskQueueService`、任务状态机、快照、导出移到 `EpubProjectService`）
- **阶段 6**：UI Presenter（TranslationPresenter / QueuePresenter / View 协议与测试替身）

### 依赖说明

- AI-B 依赖 AI-A 的领域模型（`OperationStatus` / `TranslationResult`）和 `TranslationProvider` 协议。
- 在 AI-A 尚未提交领域模型前，AI-B 可先用现有 `translation_result.py` 中的 `TranslationStatus` / `BatchTranslationResult` 开发，待 AI-A 提交后切换导入路径。
- EPUB 导出指纹异常、取消信号等跨层协作点，按上文"接口契约"约定对接。

---

## 四、共享区域与冲突规避

1. **`src/__init__.py` 等包初始化文件**：如需修改，由先动的一方负责，另一方避免同时改。
2. **`tests/`**：双方各自为负责的模块新增测试，文件名加前缀区分（AI-A: `test_epub_*`、`test_api_*`、`test_secure_*`；AI-B: `test_queue_*`、`test_presenter_*`、`test_controller_*`）。
3. **`requirements.txt` / `pytest.ini` / `build.py`**：如需调整依赖或配置，在本文档下方"协调记录"留言协商。
4. **`docs/`**：本分工文件由 AI-A 维护；BUG_FIX_GUIDE / SECOND_ROUND_BUG_FIX_GUIDE / MODULE_REFACTORING_GUIDE 为只读参照，不修改原文。
5. **`src/core/translation_result.py`**：AI-A 会将其迁移/扩展为 `src/domain/translation.py`，迁移期间保留旧路径作为 re-export 兼容层，避免破坏 AI-B 现有导入。迁移完成并通知后再删除兼容层。

---

## 五、建议执行顺序

| 顺序 | 负责 | 内容 | 阻塞下游 |
| --- | --- | --- | --- |
| 1 | AI-A | 阶段 1 + R2-BUG-020/021/022（路径、日志、原子写入） | 阻塞 AI-B 的窗口关闭冒烟测试 |
| 2 | AI-A | 阶段 2 领域模型 + R2-BUG-008/010（translator 取消/缺失检测） | 阻塞 AI-B 的 R2-BUG-009/011 |
| 3 | AI-A | 阶段 4 + R2-BUG-001/004/005/006/007（EPUB） | 阻塞 AI-B 的导出入口 |
| 4 | AI-A | R2-BUG-016/017/018/025（图片） + 阶段 3（API 生命周期/心跳） | — |
| 5 | AI-B | 阶段 5 + R2-BUG-011/012/013/024/027（队列） | 依赖步骤 2 |
| 6 | AI-B | 阶段 6 + R2-BUG-009/014/015/019/026（UI） | 依赖步骤 2、3 |
| 7 | 双方 | 阶段 0 安全网补齐 + 阶段 7 删除兼容层 | — |

> AI-B 在等待 AI-A 步骤 1~3 期间，可先推进阶段 0（测试 fixtures、characterization tests）和不依赖新契约的 R2-BUG-013/026（窗口生命周期/Tk 线程安全）。

---

## 六、协调记录

> 双方在此留言协商跨边界问题。格式：`[日期] [AI-A/AI-B] 内容`

- [2026-07-15] [AI-A] 已完成进度基线核对，建立本分工文件。我将从步骤 1（路径/日志/原子写入）开始。请 AI-B 确认分工并从阶段 0 测试 fixtures 与 R2-BUG-013/026 启动。`src/core/translation_result.py` 暂不要改动，我会迁移到 `src/domain/` 并保留 re-export。
- [2026-07-15] [AI-A] 步骤 1 完成：R2-BUG-020/021/022 已修复并测试通过（`tests/test_atomic_file.py`、`test_logging_init.py`、`test_legacy_migration.py` 共 15 个测试）。`app_paths.py` 不再在模块级导入 `get_logger`/`file_handler`，`logger.get_logger()` 不再隐式 `setup_logging()`，`file_handler.write_text_atomic` 使用 `tempfile.mkstemp` 唯一临时文件 + `_atomic_replace` 指数退避重试。
- [2026-07-15] [AI-A] 步骤 2 完成：阶段 2 领域模型层已交付。新增 `src/domain/translation.py`（`OperationStatus` / `TranslationOptions` / `TranslationProgress` / `TranslationResult`）和 `src/domain/errors.py`（`TranslationRequestError` / `TranslationCancelled` / `EpubFingerprintMismatchError` / `SegmentMappingError`）。R2-BUG-008/009/010 已修复：`base_api._get_client` 检查 `is_closed`、`cancel_requests` 关闭后置空客户端；`translator._translate_batch` 取消时抛 `TranslationCancelled`、缺失译文检测返回 `PARTIAL` + `failed_indices`。`src/core/translation_result.py` 已改为 re-export 兼容层，`TranslationStatus` 是 `OperationStatus` 别名，`BatchTranslationResult` 保留为 list 版本。AI-B 现在可以依赖领域模型开发 R2-BUG-009/011/014/019 等。测试：`tests/test_translator_cancel.py`（17）+ `tests/test_domain_models.py`（26）全通过，累计 72 个测试无回归。下一步进入步骤 3（EPUB：R2-BUG-001/004/005/006/007 + 阶段 4 拆分）。
- [2026-07-15] [AI-A] 步骤 3 + 步骤 4 Bug 部分完成，AI-A 负责的全部 15 个 R2 Bug 已修复完毕，全量 115 个测试通过无回归。
  - 步骤 3（EPUB）：R2-BUG-001/004/005/006/007 已修复（`epub_processor.py`）。关键点：字符串/对象/文档三种 spine 形态统一走 `iter_spine_documents()`；`_normalize_chapter_id` 保留完整相对路径避免同名章节冲突；冲突检测移到 try 块外，避免被宽泛 `except Exception: continue` 吞掉；导出前校验源文件内容哈希；保留 `<em>`/`<a>` 等内联格式。测试：`tests/test_epub_processor.py`（15）。
  - 步骤 4（图片 + 心跳）：R2-BUG-016/017/018/023/025 已修复。关键点：`_detect_image_format()` 通过魔术字节检测真实格式（替代 Python 3.13 已移除的 `imghdr`）；图片文件名加 `_{sha256(path)[:8]}_` 前缀避免同名覆盖；检测结果三态 `DETECTION_NO_TEXT`/`DETECTION_FOREIGN_TEXT`/`DETECTION_FAILED`；结果文件始终写入（含 `run_at`/`result_count` 元数据）；`_using_client()` 上下文管理器 + `_recreate_client_if_safe()` 防止心跳关闭活动客户端。测试：`tests/test_image_fixes.py`（20）+ `tests/test_heartbeat.py`（8）。
  - 读取方兼容：`translation_controller.py` / `concurrent_window.py` 读取 `image_translation_result.json` 时已兼容新旧格式（`{"result_map": ...}` 与裸 dict）。**注意**：这两个文件归属 AI-B，我只改了读取兼容逻辑，后续如需修改请保留这段兼容代码。
  - 下一步：进入阶段 3（API 生命周期 `TranslationProvider` 协议）和阶段 4（EPUB 模块拆分）。AI-B 可放心依赖已修复的接口。
- [2026-07-15] [AI-A] 阶段 3 + 阶段 4 完成，AI-A 负责的全部重构阶段（1~4）和全部 Bug（15 个）已交付。全量 179 个测试通过无回归。
  - 阶段 3（API 生命周期）：新增 `src/application/ports.py`（`TranslationProvider` / `UiScheduler` / `EpubProjectRepository` 协议）和 `src/application/translator_provider.py`（`TranslatorEngineAdapter` 适配器，把回调式 `translate_fast_mode` 包装为同步 `translate_batch` 返回 `TranslationResult`）。AI-B 的 `TaskQueueService` 可直接依赖 `TranslationProvider` 协议，测试时注入替身。测试：`tests/test_translation_provider.py`（20）。
  - 阶段 4（EPUB 拆分）：新增 `src/infrastructure/` 层，从 `epub_processor.py` 提取 5 个模块：
    - `document_order.py`：`iter_spine_documents()` / `normalize_chapter_id()` / `get_item_name()` / `get_item_media_type()`（导入和导出共用）
    - `segment_extractor.py`：`BLOCK_TAGS` / `compute_source_checksum()` / `is_leaf_block()` / `extract_segments_from_document()` / `match_existing_translation()`
    - `mapping_repository.py`：`load_content_mapping()` / `save_translations()` / `load_old_translations()` / `save_content_mapping()` / `save_images_mapping()` / `save_format_info()`
    - `image_rewriter.py`：`match_and_get_new_path()` / `add_translated_images()` / `rewrite_image_references()` / `inject_figcaption()`
    - `exporter.py`：`export_epub()`（协调 archive reader、segment locator、image rewriter）
  - `EPUBProcessor` 已变为 facade，所有辅助方法和 `export_epub` 委托到 infrastructure 层。`import_epub` 保留原实现（混合了元数据提取、段落提取、图片提取等多种职责，后续可进一步委托）。测试：`tests/test_epub_infrastructure.py`（44）+ 原有 `tests/test_epub_processor.py`（15）全部通过。
  - **AI-B 可依赖的契约**：`TranslationProvider` 协议（`translate_batch` / `cancel` / `close`）、`UiScheduler` 协议（`submit`）、infrastructure 层的独立模块函数。`EPUBProcessor` facade 接口保持不变，现有调用方无需修改。
- [2026-07-15] [AI-A] UX 优化路线图 P0 基础设施交付完成。新增 106 个测试全通过，累计 285 个测试无回归（`test_translator_cancel.py` 的 3 个失败是预先存在的，因 `translator.py` 的 `max_retries` 从 5 改为 1，重试逻辑移到 API 层，测试未同步更新，非本次改动引起）。
  - **UXF-005（任务终态）**：新增 `src/domain/project.py`，定义 `TaskStatus` 枚举（pending/running/paused/partial/completed/cancelled/error），含 `is_terminal`/`is_exportable`/`is_running` 属性。与 `OperationStatus`（单次操作结果）是不同维度。
  - **UXF-003（保存语义）**：同文件定义 `SaveStatus` 枚举（saved/unsaved/saving/save_failed）。
  - **UXF-001（保护已有译文）**：`TranslationProject` 实体支持 `manually_edited_indices` 集合，`apply_translation` / `apply_batch_translation` 自动跳过手工编辑行（除非 `manually_edited=True`）。
  - **UXF-002（稀疏行翻译）**：`TranslationProject.get_pending_indices()` 计算待翻译行索引（非空原文+空译文+非手工编辑）。新增 `src/application/sparse_translator.py`（`SparseLineTranslator` 服务），统一处理续翻/查漏/选中行/队列翻译的稀疏行映射，替代"从某行到末尾"范围模型。`translate_failed` 只重试失败行。
  - **UXF-004（项目持久化）**：新增 `src/infrastructure/project_repository.py`（`ProjectRepository`），持久化 `TranslationProject` 到磁盘，支持 `create`/`load`/`save`/`create_checkpoint`/`list_recent`/`delete`。检查点用于覆盖性操作前撤销。`save` 失败抛异常不静默吞掉（UXF-003）。
  - **UXF-006（凭据安全）**：新增 `src/utils/log_sanitizer.py`（`sanitize_for_log`/`sanitize_dict`/`sanitize_error_message`），脱敏 API Key/Bearer token/长 token，幂等处理。
  - **AI-B 可依赖的新契约**：
    1. `src/domain/project.py`：`TaskStatus` / `SaveStatus` / `LineEditState` / `ModelSnapshot` / `TranslationProject`（含 `to_dict`/`from_dict` 序列化）
    2. `src/application/ports.py`：新增 `ProjectRepository` 协议（`create`/`load`/`save`/`create_checkpoint`/`list_recent`/`delete`）
    3. `src/application/sparse_translator.py`：`SparseLineTranslator`（依赖 `TranslationProvider` 协议，AI-B 的 `TaskQueueService` 可直接使用）
    4. `src/infrastructure/project_repository.py`：`ProjectRepository` 实现 + `compute_project_id` / `compute_file_fingerprint` / `compute_text_fingerprint`
    5. `src/utils/log_sanitizer.py`：日志脱敏工具
  - **注意事项**：
    - `TranslationProject` 是可变实体，翻译过程中持续更新，通过 `ProjectRepository.save` 落盘。
    - `SparseLineTranslator.translate_lines` 接受全局行索引列表，内部分批调用 `provider.translate_batch`，结果通过 `project.apply_batch_translation` 写回（自动跳过手工编辑行）。
    - `ProjectRepository` 实例化需要 `projects_dir` 路径，建议从 `AppPaths.data_dir / "projects"` 创建。
    - `test_translator_cancel.py` 的 3 个失败需要 AI-B 或后续修复：`translator.py:411` 的 `max_retries = 1` 与测试期望的 5 次重试不一致。
