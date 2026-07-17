# Python 与 UI/UX 最佳实践审查及优化计划

> 历史快照说明：本文记录的是较早工作树。当前复核结论与实施路线请以
> [`PYTHON_UIUX_CURRENT_OPTIMIZATION_AUDIT.md`](PYTHON_UIUX_CURRENT_OPTIMIZATION_AUDIT.md) 为准。

## 1. 结论

审查日期：2026-07-17（UTC+08:00）。

当前项目只能判定为**部分符合** Python 与桌面 UI/UX 最佳实践，暂不满足稳定发布条件。

项目已经建立了较好的重构基础：领域模型、Protocol、组合根、统一路径、原子写入、密钥环、日志脱敏、事件邮箱、文档模型、自动保存协调器、分块表格和较丰富的自动化测试都已出现。当前最主要的问题不是“完全没有工程实践”，而是**新基础设施与生产 UI 的最后一段接线尚未闭合**，导致模型、视图、文件路径和后台任务之间仍可能失去一致性。

发布前必须优先解决以下问题：

1. PyInstaller 已改成 `onedir`，CI 和构建验证仍按单文件产物处理，发布链路与产物格式冲突。
2. 主表和队列详情的人工编辑没有写回各自唯一模型，已编辑内容可能按旧值保存或导出。
3. EPUB、TXT、剪贴板共用的路径状态没有原子切换，可能把新项目译文写入旧文件或旧 EPUB 映射。
4. 翻译过程中仍可导入、粘贴和编辑，旧任务事件可能按旧索引写入新文档。
5. 未保存状态依附于自动保存；关闭自动保存、保存超时或保存失败时仍可直接退出并丢失数据。

在上述 P0 问题关闭前，不建议继续扩大并发数、增加新的导出格式或继续堆叠主窗口操作入口。

## 2. 审查范围与限制

### 2.1 范围

- Python 第一方代码：`main.py`、`build.py`、`src/**`、`tools/**`、`tests/**`。
- 工程配置：`pyproject.toml`、`requirements*.txt`、PyInstaller spec、GitHub Actions、README、`.gitignore`。
- 生产 UI：`src/ui/**` 及其直接调用的应用、领域和基础设施代码。
- `third_party/manga-image-translator/**`：不审查上游实现风格，只审查依赖、许可证、打包和本项目集成边界。
- `build/**`、`dist/**`、`workspace/**`：视为生成物，不纳入源码风格审查。

`creative_proposal.html` 没有被生产入口引用，且页面自身标注为创意方案展示，本次只在附录记录其未来发布前需要处理的问题，不把它当成当前 Tkinter 产品界面。

### 2.2 动态工作区限制

审查时工作区存在大量未提交改动。2026-07-17 14:00 至 14:20 期间又新增了 `queue_checkpoint.py`、`queue_provider.py`、`queue_scheduler.py`、4 个队列测试文件，`concurrent_manager.py` 也切换为新调度器的兼容门面。

因此：

- 本文以 2026-07-17 14:31 左右的工作区为最终复核快照。
- 新队列算法只记录生产接线与用户可见行为，不对仍在变化的内部实现给出最终质量结论。
- 合并前必须冻结工作区并重新运行完整测试、Ruff 和真实打包冒烟测试。
- 本文不会覆盖或回退任何已有改动。

### 2.3 参考基线

- Python：PEP 8、类型注解与 Protocol、窄化异常、显式资源生命周期、隔离副作用、可复现依赖、`pyproject.toml` 与 CI 质量门禁。
- Tkinter：所有 UI 状态变更由 Tk 主线程执行；后台线程只产生不可变结果或事件；窗口、任务和文件会话具有明确 owner 与关闭路径。
- `ui-ux-pro-max`：按“桌面 AI 翻译生产力工具、数据表格、长任务、专业、紧凑、多语言”生成设计基线，并补充焦点、键盘、表单、进度、错误恢复和对比度规则。
- Vercel Web Interface Guidelines：2026-07-17 从最新规则源读取。ARIA、URL 状态、hydration、HTML 语义等 Web 专属规则不适用于 Tkinter；其键盘、焦点、错误恢复、长列表、破坏性操作、对比度和异步反馈原则转换为桌面等价验收。

设计技能给出的 `#0D9488` 只适合作为候选主色，不应凌驾于可访问性要求。该颜色配白色小字的对比度约为 3.74:1，不满足普通文本 4.5:1；可改用当前已有的 `#0F766E`（约 5.47:1）或交由系统主题决定。

## 3. 验证结果

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| 只读 AST 语法解析 | 131/131 通过 | 最终快照第一方 Python 文件均可解析；不代表导入和运行时行为通过 |
| `python -m pip check` | 通过 | 只说明当前宿主已安装包之间没有已声明冲突，不代表 requirements 可复现 |
| UI 启动相关 `unittest` | 4 passed | 覆盖懒加载、密钥延迟读取、底部布局预留、分块加载 |
| 临时干净环境完整 pytest | 596 passed / 14 failed / 7 errors | Python 3.13、最新下限依赖；结果早于最后加入的 4 个队列测试，不能作为最终基线 |
| EbookLib 定位测试 | 60 passed | 将 EbookLib 从 0.20 降到 0.18 后，EPUB processor/exporter 的 60 个聚焦测试通过；只证明 10 个相关失败与依赖漂移有关 |
| Ruff | 325 个问题 | 以导入、现代类型语法、无效 f-string、未使用导入和格式问题为主；需先修风险规则再做机械格式化 |
| 分支覆盖率 | 约 29% | 来自失败测试运行的诊断值，只能用于识别盲区，不能作为正式基线 |
| Wheel 构建 | 构建成功但内容错误 | wheel 只有 7 个条目，仅包含 `src/__init__.py`、`app_paths.py`、`bootstrap.py` 和元数据，所有子包缺失 |
| 生产 GUI 视觉/键盘实测 | 未执行 | 避免首次引导、配置迁移和用户目录写入；必须在修复阶段补做真实桌面验收 |

本机常用 Python 环境中的 pytest 安装是残缺的 namespace package，且没有 Ruff，因此主审查过程不能直接使用该环境。完整 pytest、覆盖率和 Ruff 结果来自系统临时隔离环境；没有把依赖安装进项目目录。

## 4. 符合度矩阵

| 领域 | 结论 | 当前证据 |
| --- | --- | --- |
| 分层与依赖方向 | 部分符合 | `domain/application/infrastructure`、Protocol 和 `bootstrap.py` 已建立；主文本流程仍主要是 `ui -> core -> api` |
| 状态一致性与数据安全 | 不符合 | Treeview、`TranslationDocument`、路径字段和后台任务仍存在多真相来源 |
| Tk 线程模型 | 不符合 | 多处 worker 直接调用 `after()`、`winfo_exists()`，异常被吞掉 |
| 异常与错误恢复 | 部分符合 | 原子写和结构化结果已有；导入、保存和 UI 调度仍有静默失败 |
| 类型与接口 | 部分符合 | `domain/application` 注解覆盖较好；UI 注解、公开接口和私有成员边界较弱 |
| 测试 | 部分符合 | 最终快照 36 个测试文件、约 656 个测试函数；最后加入的队列测试未包含在上述完整运行中，且 CI 不执行测试 |
| 依赖与打包 | 不符合 | wheel 漏包、依赖仅下限、Full/Text edition 与 CI 不一致、产物格式冲突 |
| 凭据与日志安全 | 部分符合 | keyring、脱敏和结构化状态已建立；多个 UI 调用方仍丢弃持久化状态 |
| 信息架构与工作流 | 部分符合 | 空状态、动态主按钮、筛选、撤销和质检入口已建立；命令重复且项目切换不安全 |
| 键盘与辅助功能 | 部分符合 | 有 Ctrl/F 键路径和焦点处理；右键、模态窗口、进度文本、mnemonic 和屏幕阅读器路径不完整 |
| DPI、主题与布局 | 不符合 | 固定不可缩放窗口、硬编码字体/颜色、横向工具条在小屏和高 DPI 下有裁切风险 |
| 长列表与感知性能 | 部分符合 | 主表分块加载和事件邮箱较好；详情、筛选和术语搜索仍有高频 O(n) Tk 操作 |

## 5. P0：发布阻断问题

### P0-1：构建产物格式与 CI/发布脚本冲突

证据：

- `translator.spec:137-169`、`translator_text.spec:158-190` 使用 `exclude_binaries=True + COLLECT`，明确生成 `onedir` 目录。
- `.github/workflows/build.yml:42-54` 只在 `dist` 根目录查找和上传 `*.exe`。
- `.github/workflows/build.yml:81-85` 把整个 onedir 目录移动成 `Contents/MacOS/LightNovelTranslator`，但 `Info.plist:93-94` 又把该路径声明为可执行文件。
- `build.py:181-190` 同样只检查 `dist/*.exe`。

影响：Windows CI 会把成功的 onedir 构建判为失败；macOS 组装出的 `.app` 结构无效；当前发布工作流无法证明产物可启动。

改进：明确选择一种产物策略。推荐保留启动更快的 onedir：Windows 打包并上传整个目录的 zip；macOS 使用 PyInstaller `BUNDLE` 或正确复制主二进制和 `_internal`，再做 `Info.plist`、`codesign` 前的启动检查。Text 与 Full 必须使用独立 edition matrix。

验收标准：

- CI 能定位真实主二进制，而不是只检查文件名。
- Windows 在干净 runner 解压后能启动到首帧并正常退出。
- macOS `plutil -lint`、bundle 主二进制检查和最小启动 smoke 通过。
- README 下载文件名、目录结构和 CI artifact 完全一致。

### P0-2：人工编辑没有写回唯一模型

证据：

- `src/ui/table_editor.py:82-94` 先直接修改 Treeview，再调用 `on_save`。
- `src/ui/main_window.py:460-464` 的 `_on_cell_edited()` 只记录撤销、人工标记和保存调度，没有调用 `TranslationDocument.update_target/update_source`。
- `src/ui/main_window.py:864-869`、`src/application/autosave.py:297-304` 明确以 `TranslationDocument` 快照作为保存与业务数据来源。
- 最终队列快照中，`src/core/concurrent_manager.py:236-259` 返回任务及 `target_lines` 的副本；`src/ui/task_detail_window.py:184-188` 只修改该副本，随后 `src/core/concurrent_manager.py:272-274` 又让 coordinator 保存其内部旧值。最后新增的队列测试没有覆盖详情页人工编辑。

只读复现调用 `_on_cell_edited('item-0', 2, 'old', 'new')` 后，撤销栈已有 `new`，但 `document_target` 仍为 `old`，`manually_edited` 仍为 `False`。

影响：主表和队列详情中，用户看见的新译文都可能按旧值自动保存、手动保存或导出；后续刷新还可能把界面恢复为旧值。这是直接的数据丢失风险。

改进：建立唯一编辑入口。`TableCellEditor` 只提交 edit command，不直接把 Treeview 当真相来源；MainWindow 先更新 `TranslationDocument`，成功后再由 adapter 渲染。队列详情向 coordinator 提交带 task/version/row 的编辑命令，不修改快照副本。源文和译文编辑、撤销、重做、批量清空全部走各自模型的单一命令。

验收标准：

- 新增 `test_manual_cell_edit_updates_document_and_snapshot`。
- 新增 `test_queue_detail_edit_updates_coordinator_and_checkpoint`，并验证下一次 500ms 刷新不会回滚编辑。
- 双击、F2、撤销、重做后，Treeview、document、dirty/manual 标记和保存文件完全一致。
- 保存失败不会丢弃模型中的新值，且 UI 提供重试。

### P0-3：项目路径与文档会话不是原子状态

证据：

- `src/ui/file_importer.py:79,93,177` 分散修改 `current_source_path`、`current_target_path`、`current_mapping_dir`。
- TXT 导入没有清空旧 `current_mapping_dir`；`src/ui/file_importer.py:345-352` 的剪贴板导入没有清空任何旧路径。
- `src/ui/main_window.py:812,889-897` 使用这些残留字段决定 EPUB 操作和自动保存目标。
- `src/application/autosave.py:427-436` 在 mapping 存在时还会写 EPUB 映射，而且映射失败仍保持主保存成功状态。

影响：EPUB -> TXT 或 EPUB -> 剪贴板后，界面仍可能启用 EPUB 操作，并把新译文写入旧 TXT、旧辅助译文文件或旧 EPUB mapping。失败的 EPUB 导入还可能留下“新 source + 旧 target/mapping”的混合状态。

改进：引入不可分割的 `DocumentSession`：`session_id`、kind、source path、target path、mapping dir、document、dirty/save state 一次创建、一次提交。后台导入先生成 `ImportResult`，只有全部成功后才在 Tk 主线程替换当前 session；失败时旧 session 完全不变。

验收标准：

- 覆盖 EPUB -> TXT、EPUB -> 剪贴板、TXT -> EPUB、失败 EPUB -> 原项目四组测试。
- 每次切换后标题、按钮能力、保存目标、导出目标只属于新 session。
- 测试用文件监控确认旧项目零写入。

### P0-4：后台翻译事件没有绑定文档会话

证据：

- `src/ui/translation_controller.py:166-170,242-246` 只禁用翻译按钮。
- `src/ui/main_window.py:201-204,340-347,478-505` 在翻译时仍允许导入、粘贴、表格编辑和文档整体 replace。
- `src/ui/translation_controller.py:537-603` 使用旧任务保存的行索引写入当前 `TranslationDocument`；事件只检查 `run_id`，没有检查 `session_id`。
- `src/ui/translation_controller.py:139-150` 只把新 ID 写入可变的 `self._current_run_id`；worker 在真正发布回调时才于 `src/ui/translation_controller.py:421-427,462-467,494-497` 读取它。停止后立即开始新任务时，旧线程可能把迟到事件标成新 run。
- `src/ui/image_translation_handler.py:217-235` 的 worker 启动后才读取当前 mapping，也可能在切项目后取到另一个项目路径。
- `src/application/translation_document.py:114-133` 只保护人工行不被空响应覆盖，非空机器结果仍可覆盖人工编辑。

影响：翻译 A 时导入 B，A 的迟到事件可写入 B 的同行索引；翻译中人工校对也可能被后续流式或批次结果覆盖。

改进：启动 worker 时通过闭包或不可变 command 捕获 `session_id + run_id/attempt_id`，所有 publish 方法显式接收该 ID，禁止回调再读取可变“当前 ID”。渲染前同时校验 session 和 run。替换文档前必须停止并等待当前任务，或明确拒绝切换；人工编辑发生后，该行对应旧 generation 的机器结果必须丢弃。

验收标准：

- 翻译中导入、粘贴、切换 EPUB、编辑当前行均有并发回归测试。
- 旧 session 的任何事件、保存结果或图片结果都不能改变新 session。
- 旧 run 的回调在新 run 启动后到达时，必须仍携带旧 ID 并被丢弃。
- 人工编辑后的旧 generation 机器结果不得覆盖人工译文。

### P0-5：dirty、自动保存和关闭语义耦合

证据：

- `src/ui/main_window.py:881-904` 在 `auto_save=False` 时直接返回，不建立独立未保存状态。
- `src/ui/main_window.py:927-939,1149-1157` 保存超时只返回 `False` 和更新状态，`close()` 忽略该结果。
- `main.py:78-90` 无论保存是否成功都会销毁 root。
- 新文档导入在 `src/ui/main_window.py:478-505` 直接 replace，未先执行 Save/Discard/Cancel。
- `src/application/autosave.py:421-436` 在辅助 TXT 写入后先置 `succeeded=True`，随后 EPUB mapping 写入失败只记日志，最终仍会把状态推进为 `CLEAN`。

影响：关闭自动保存后，编辑、导入新项目或退出都可能静默丢失；保存失败状态在窗口立即销毁后用户也看不到。

改进：`dirty` 是文档状态，不是自动保存状态。自动保存只决定何时尝试保存。退出、替换 session、关闭队列等统一走 guard：无 dirty 直接继续；有 dirty 提供“保存 / 放弃 / 取消”；保存失败默认留在当前窗口。

验收标准：

- 自动保存开/关两组路径都能正确显示未保存状态。
- 磁盘满、只读路径、保存超时和 mapping 保存失败时不能无确认退出。
- EPUB 模式只有辅助 TXT 和主 mapping 都成功后才能显示“已保存”；mapping 失败必须保持 DIRTY/SAVE_FAILED 并可重试。
- Save/Discard/Cancel 的每个分支都有测试，取消后 session 与窗口保持原状。

### P0-6：导入错误被当作空译文继续执行

证据：

- `src/ui/file_importer.py:97-108` 读取已有 `_译文.txt` 失败时使用空列表；创建目标失败也直接 `pass`。
- `src/ui/file_importer.py:200-206` EPUB 辅助译文文件创建失败同样被吞掉。
- 后续仍在 `src/ui/file_importer.py:110-114` 报告导入成功。

影响：无权限、文件锁、损坏编码等错误会伪装成“没有译文”，后续翻译或保存可能覆盖原有成果。

改进：导入返回结构化 `ImportResult`，明确区分源读取失败、目标读取失败、目标不可写、mapping 失败。任何必要输入失败都不提交新 session；可恢复错误必须显示路径、原因和下一步。

验收标准：锁定文件、只读目录、损坏编码、磁盘满的测试均不加载空译文、不修改原文件，并保留旧 session。

## 6. P1：高优先级工程与体验问题

### P1-1：Tk 线程边界仍不可靠

证据：

- `src/ui/file_importer.py:55-60,165-174,236-238` 从 worker 调用 `winfo_exists()` 和 `after()`。
- `src/ui/settings_window.py:78-84,544,571,778-808` 使用相同模式回传连接测试结果。
- `src/ui/image_translation_handler.py:217-224,340-345` 从图片 worker 直接调用 `root.after()`。
- `src/ui/concurrent_window.py:135-143` 的后台进度回调也直接接触 Tk。

影响：这种模式依赖 Tcl 构建和 mainloop 时序，不是稳定的主线程边界；窗口销毁、应用退出或高并发时可能静默丢事件、卡住或抛 TclError。宽泛 `except Exception: pass` 又隐藏了真正故障。

改进：复用已经存在的 mailbox/event-pump 模式。worker 只 `put()` 不可变事件；一个由 Tk 主线程创建的固定 `after` 轮询器消费。关闭时先标记 closed，再取消轮询，最后停止/等待 worker。

验收标准：测试记录所有 Tk 方法的调用线程 ID，任何后台线程调用都失败；窗口销毁后的迟到事件被安全丢弃且有可观测计数。

### P1-2：密钥持久化状态在 UI 层再次被压成布尔值

证据：

- `src/domain/secret.py:59-92` 已能区分 `PERSISTED / SESSION_ONLY / FAILED` 并提供 `user_message`。
- `src/ui/settings_window.py:859-872` 仍把 `save_api_config()` 用作布尔值并固定显示“设置已保存”。
- `src/config/config_manager.py:632-654,762-777` 的预设和火山 Key 保存忽略 `store_key()` 返回状态。
- `src/config/config_manager.py:178-216,617-629` 的读取路径仍部分使用全局 `get_key()`，没有完整使用注入的 SecretStore。

影响：keyring 不可用时，用户会以为密钥已永久保存，重启后才发现丢失；预设可能只剩模型名而无密钥；测试替身不能完整替代真实存储。

改进：所有凭据保存统一返回 `SecretSaveResult` 或同类结果，UI 直接展示 `user_message`。预设和火山 Key 也必须区分三态。读写都通过注入的 `SecretStore`，移除新旧全局函数混用。

验收标准：SESSION_ONLY 明确提示“重启后需重新输入”且不表述为永久成功；FAILED 不关闭设置窗口；预设、主 API、火山 Key 三条路径都有 UI 集成测试。

### P1-3：EPUB 导入弹窗和队列关闭是破坏性生命周期

证据：

- `src/ui/file_importer.py:122-126,159-160,233-234,285-293` 禁用主界面后允许用户关闭进度窗；窗口不存在时最终恢复回调可能被丢弃。
- `src/ui/concurrent_window.py:100-124` 关闭队列窗口会调用 manager `close()`。
- 当前兼容门面 `src/core/concurrent_manager.py:127-137` 会关闭 coordinator；`src/core/queue_scheduler.py:554-590` 会停止派发、清空 pending、停止引擎并关闭资源。
- `src/ui/concurrent_window.py:191-198` 的“全部取消/移除已完成”和主表批量清空缺少统一的影响范围与撤销策略。

影响：关闭一个工具窗口会停止后台任务，语义超出用户预期；解析窗关闭后主界面可能永久禁用；批量任务可能被单击破坏。

改进：队列服务归应用生命周期所有，队列窗口只订阅快照。关闭窗口不停止任务；真正停止应用或“全部取消”时显示任务数、已完成量和后果。EPUB 解析窗口要么禁止关闭，要么实现真实 cancel，并由主窗口独立、幂等地恢复控件。

验收标准：关闭队列窗口后任务继续且主窗口可见全局状态；取消全部必须确认；EPUB 在任意阶段关闭/取消后主界面始终可用。

### P1-4：Wheel 包发现配置错误

证据：`pyproject.toml:92-93` 只有 `packages = ["src"]`。真实 wheel 只包含 `src` 根下 3 个 Python 文件，`api/application/core/domain/infrastructure/ui/utils` 全部缺失。

影响：`pip install .` 生成一个看似成功、实际不可运行的包；开发环境和未来发布容易产生“本地源码可用、安装后失败”的假象。

改进：短期使用 setuptools package discovery 包含 `src*`；长期改为标准 `src/ai_translator/**` 布局，避免把 `src` 当公开包名。增加 wheel 安装后的子包 import 和最小启动 smoke。

验收标准：在全新 venv 安装 wheel 后能导入所有一方子包，且不依赖仓库当前目录进入 `sys.path`。

### P1-5：依赖不可复现，Full/Text edition 契约不清晰

证据：

- `requirements.txt` 和 `pyproject.toml` 大多只有下限；干净环境解析到 EbookLib 0.20 后出现 10 个 EPUB 相关失败，0.18 的聚焦测试通过。
- `requirements-image-manga.txt:3-10` 自称锁定但多数使用范围，且要求全局 Python、不使用虚拟环境。
- `requirements-image-manga.txt:107-108` 使用额外索引且 Rust 包无版本/hash；`pyproject` extra 无法表达该索引策略。
- `.github/workflows/build.yml:31-40,69-76` 只安装基础 requirements，却构建声明包含深度学习栈的 Full `translator.spec`；Text spec 没进入 CI。
- `requirements.txt:12-14` 把 pytest/pytest-cov 安装给终端用户，而 `pyproject` 又定义了 dev extra。

影响：同一提交在不同日期解析出不同依赖；Full 版可能构建成功但图片能力缺失；额外索引增加依赖混淆风险。

改进：先约束 `ebooklib>=0.18,<0.20`，随后单独适配 0.20。建立 Text/Full 支持矩阵和按 OS/Python 生成的 lock/constraints；发布只能从 lock 安装。第三方 wheel 使用受控源、精确版本和 sha256。始终使用隔离 venv。

验收标准：同一 lock 在干净 runner 可重复解析；Text 不安装深度学习栈；Full 的 Provider availability smoke 能证明所需模块真实存在。

### P1-6：CI 没有质量门禁，当前测试不是绿色

证据：`.github/workflows/build.yml` 只有安装、构建和上传，没有 Ruff、pytest、coverage、wheel install smoke 或真实产物启动测试。

动态快照的干净 Python 3.13 环境结果为 596 passed / 14 failed / 7 errors。7 个 error 中包含 `tests/test_bootstrap.py:15-21` 的测试隔离错误：fixture 设置了代码不读取的环境变量，而 `AppPaths` 实际读取 `APPDATA/XDG`，可能写真实用户目录。另有自定义 provider 密钥读取、Python 3.13 数据类异常类型和图片 packaging 断言问题。

改进：建立先 quality、后 package、再 release 的依赖链。Python 3.11 作为当前必跑版本；如果 `requires-python >=3.11` 不加上限，则 3.12/3.13 也必须进入兼容矩阵。测试直接注入 `AppPaths`，不得依赖真实用户目录。

验收标准：

- PR 必须通过 Ruff 风险规则、format check、pytest、wheel install smoke。
- 构建 job `needs: quality`；release `needs` 所有 edition/package smoke。
- 任何测试不得写用户 AppData、真实 keyring、仓库配置或源文件。

### P1-7：高 DPI、主题、键盘与长列表仍有明显缺口

证据：

- `src/ui/settings_window.py:56-76` 固定 `560x720` 且不可缩放，没有页内滚动。
- `src/ui/concurrent_window.py:43-69` 最小宽度 640 却放置大量横向按钮。
- `src/ui/main_window.py:351-360,554-555` 强制白底和固定选择色；`src/ui/settings_window.py:279-282,835` 的“界面字号”只实际影响 Treeview。
- 多处硬编码“微软雅黑”；队列和导入进度使用 Emoji 作为图标。
- `src/ui/task_detail_window.py:113-150` 每 500ms 扫描全部行；`src/ui/main_window.py:653-670` 筛选逐行 detach；`src/ui/glossary_window.py:176-204` 每次按键重建列表。
- 右键菜单、详情编辑、模态窗口 Esc/default button/focus return、进度百分比文本和 Alt mnemonic 不完整。

改进：设置页改为可缩放/可滚动；工具条在窄宽下折叠低频动作。使用 Tk named fonts 和系统主题，配置全局字号；状态色仍需文本/图标辅助。补 Shift+F10、Esc、default button、焦点恢复和文本进度。大列表使用 debounce、增量 diff、可见区渲染或降低刷新频率。

验收标准：

- 800x600 与 Windows 100/125/150/200% DPI 无控件裁切。
- Windows 高对比模式、macOS/Linux 默认主题可读。
- 纯键盘可完成配置、导入、翻译、校对、保存、导出和队列管理。
- 10,000 行文档、5,000 术语、50 个队列任务的常用交互有明确响应预算。

### P1-8：图片任务取消与 Manga worker 关闭不可靠

证据：

- `src/infrastructure/image_translation/volcengine_provider.py:111-131,160-162` 的 `cancel()` 只设置 event；进度回调检测后仅 `return`，没有终止 `translate_images()`。
- `src/core/image_translator.py:320-380` 的 API 调用、下载、重试等待和落盘没有接收或检查取消令牌。
- `src/infrastructure/image_translation/manga_worker_client.py:94-113` 关闭时直接阻塞读取 `stdout.readline()`，读取本身没有超时；正常请求在 `:115-140` 也无限读取。
- Manga worker 的 `stderr` 在 `src/infrastructure/image_translation/manga_worker_client.py:188-193` 被丢弃，卡死时缺少诊断信息。

影响：用户点击取消或退出后仍可能继续产生计费请求、下载和写文件；worker 卡在模型推理或协议读取时，主线程关闭路径可能长期阻塞。关闭线程与请求线程同时读取 stdout 还可能抢走协议消息。

改进：把同一取消令牌传到图片循环、每次重试、下载和落盘前，取消时抛领域异常并等待 worker 收敛。Manga 子进程使用单一 reader 线程解析协议并投递队列；请求有 deadline，关闭不直接读管道，超时后按 terminate -> wait -> kill 收敛。stderr 进入有界、脱敏日志。

验收标准：

- 取消后不再发起新的 API 请求或写新文件，结果明确为 CANCELLED。
- 关闭期间 worker 无响应时，应用在规定时间内退出且无残留子进程。
- 连续取消、重复 close、请求完成与 close 竞态都有测试。

### P1-9：项目图片路径和远程下载缺少信任边界

证据：

- `src/infrastructure/image_asset_store.py:114-124,143-150,191-196` 直接使用 `mapping_dir / local_path`，没有拒绝绝对路径、`..` 或验证 resolve 后仍位于 assets 目录。
- 定向复现中，`local_path="../outside-audit.bin"` 能读取 mapping 目录外文件。
- `src/core/image_translator.py:337-367` 直接下载服务返回 URL 的完整 `.content`，没有 scheme/host、重定向、Content-Type 或最大字节数限制，然后直接写盘。

影响：被篡改的 `images.json` 可读取任意可访问本地文件，并可能把内容发送给第三方图片服务；异常或恶意下载地址还可造成内网访问和大内存占用。

改进：`local_path` 只允许受控 assets 根下的相对路径，`resolve()` 后用 `is_relative_to()` 校验，并验证 checksum、大小和允许的 MIME。下载只允许 HTTPS 与可信 host，限制重定向，使用流式读取和最大字节数，再以 Pillow/魔数验证后原子落盘。

验收标准：绝对路径、`../`、符号链接逃逸、超大文件、错误 MIME、非 HTTPS、非可信 host 和重定向到内网均被拒绝，并且错误中不泄露本地敏感路径。

## 7. P2：结构与一致性优化

### P2-1：新旧架构并存，生产接线应按垂直切片完成

`TranslationProject`、`ProjectRepository`、`SparseLineTranslator`、preflight、quality review、usage 等模块已有较完整测试或设计，但多处仅在测试中使用；主文本翻译仍由 `MainWindow -> TranslationController -> TranslatorEngine` 编排。与此同时 `TranslationController`、`MainWindow`、`SettingsWindow`、`TranslatorEngine`、`ConfigManager`、`BaseAPI` 仍是数百到上千行的热点类。

不要只按文件拆类。建议按真实用例迁移：

1. `DocumentSession` 与编辑/保存 guard。
2. 主文本翻译 command、session/run 校验和结构化结果。
3. 项目仓储与恢复。
4. 队列 service 与窗口订阅。
5. 设置/凭据 result。

每迁移一条生产链路就删除或明确弃用旧入口，避免长期维护两套状态机。

### P2-2：异常捕获需要围绕边界窄化

项目需要保留的宽捕获位置包括线程顶层、Tk 回调边界和应用启动边界，但必须记录结构化上下文并转换为领域结果。当前应优先消除：

- 导入读取/创建文件后的 `except Exception: pass`。
- `_safe_after` 对所有错误静默丢弃。
- `AutosaveCoordinator` 用 `except Exception` 代替 `queue.Empty`。
- 配置删除、窗口销毁和资源关闭中没有区分“可忽略不存在”与真实失败。

Ruff 的 `E722/F401/F841/B905` 等风险项先修；I/UP/format 再单独做机械提交，避免和行为修复混在一起。

### P2-3：类型门禁从稳定层向 UI 扩展

只读 AST 统计显示，生产代码完整参数/返回注解大致为：`application` 约 92%、`domain` 约 87%、`infrastructure` 约 77%、`core` 约 70%、`ui` 约 26%。

建议先对 `domain/application` 开启 mypy 或 pyright 严格门禁，再覆盖新写的 infrastructure 和 UI adapter。旧 Tk 代码不必一次性全补；所有新公开接口、事件、结果和 session 对象必须有类型。

### P2-4：主界面命令层级和保存文案需要收敛

- “翻译未完成行”和“继续翻译”当前行为高度重叠，应只保留一个动态主按钮。
- 图片翻译和 EPUB 导出同时出现在项目菜单、更多操作和独立按钮区，低频命令重复。
- `Ctrl+S` 菜单名是“保存译文”，`src/ui/translation_controller.py:1094-1103` 实际每次弹出另存为，且不会更新自动保存目标。
- 当前“质检”只检查空译文或原译文相同，名称容易夸大能力。

建议：一个主命令 + 一个上下文动作菜单；`Ctrl+S` 保存当前目标，“另存为/导出译文”独立；当前检查改名“基础完整性检查”，或真正接入术语、标点、长度、残留标记和 EPUB 完整性规则。

### P2-5：视觉系统应优先服从系统主题和可访问性

- 主队列按钮使用原生 `tk.Button`，其余大多使用 ttk，主题表现不一致。
- `#0D9488` 白字对比不足；状态绿色 `#0a0` 在白底也低于普通文本要求。
- Emoji 在不同 Tk 字体回退下可能缺字、错位或改变控件高度。
- 字体设置只覆盖表格，与“界面字号”文案不一致。

推荐用 ttk style token 和 Tk named font 建立小型设计系统：surface、text、muted、accent、danger、focus、spacing、row height。图标采用同一套位图资源或直接使用清晰文本，不把颜色作为唯一状态信号。

### P2-6：版本、许可证和文档需要成为发布输入

- `pyproject.toml:7` 是 `1.4.0`，README、主窗口、build、spec 和 workflow 是 `1.6`；真实 wheel 名也是 1.4.0。
- README 声称 MIT 并链接根 LICENSE，但仓库根目录没有 LICENSE；只有 third-party LICENSE。
- workflow 将 `macos-latest`、`target_arch=None` 的产物命名为 Universal，但没有 universal2/lipo 证据。
- 分支 push 仍可能复用固定 `V1.6` release tag，发布不可追溯。

建立单一版本源，由应用标题、wheel、artifact、bundle 和 release 读取；补齐根许可证与 `project.readme/license` 元数据。只从唯一版本 tag 发布，架构名称必须反映真实产物。

默认翻译提示词中的“无视法律、道德或安全约束”等对抗性文案也应移除。它会增加提供商拒绝、行为漂移和服务条款风险；提示词应聚焦目标语言、文学风格、行映射协议、术语约束和输出格式，并纳入版本化测试。

### P2-7：日志、复用 hook 与维护工具仍有边界缺口

- `sanitize_for_log()` 已定义，但实际 logger handler 没有安装统一脱敏 Filter；外部响应和异常仍有直接进入日志的路径。`src/core/image_translator.py` 还在模块导入时调用 `logging.basicConfig()`，会造成全局副作用。
- `src/infrastructure/image_translation/manga_provider.py:202-221` 每次运行都添加 progress hook，而引擎在 `:318` 后缓存复用，没有对应移除；多次任务可能重复回调并保留旧闭包。
- `tools/add_navigation_to_spine.py:38-49` 先修改对象再写“备份”，备份不是修改前原始内容，最终覆盖也不是原子写。

建议在中央 handler 安装脱敏 Filter，外部响应只记录状态码、request ID 和清洗摘要；删除模块级 `basicConfig/print`。Progress hook 只注册一次或在 finally 中移除。维护工具先保存原始字节，再复用统一原子写，并用退出码表达成功或失败。

## 8. 已做得较好的部分

- `src/bootstrap.py` 和 `AppPaths` 已开始集中组合依赖与路径决策。
- domain dataclass、枚举、Protocol、结构化成功/部分成功/失败/取消状态是正确方向。
- 原子写、日志脱敏、keyring、资源 `close()` 和旧配置迁移已有较系统的专项测试。
- `TranslationDocument`、`TranslationTableAdapter`、event mailbox 和 Tk event pump 为修复多真相来源提供了现成基础。
- 主表加载按 250 行分块，翻译热路径使用文档模型和批量 adapter，明显优于逐次全表读取。
- 首帧有启动反馈，重服务懒加载，自动保存使用不可变快照和 generation 状态机。
- 空状态、最近项目、筛选搜索、撤销重做、F2 校对和新手引导已形成基本生产工作流。
- 默认只翻译缺失行；部分成功与失败行已有明确状态，不再一律冒充完成。
- 第三方源码已有 LICENSE、notices、upstream 与 hash 记录，供应链意识优于直接复制上游代码。

这些优点应保留，但必须通过生产接线测试证明，而不能只由模块级测试证明。

## 9. 推荐实施顺序

### 阶段 0：停止发布并修正构建契约

1. 选择 onedir 或 onefile，修正 Windows/macOS artifact 与真实启动 smoke。
2. 建立 Text/Full edition matrix；暂时关闭自动 release，直到两个 edition 的 smoke 都通过。
3. 修复 wheel package discovery。
4. 临时约束 EbookLib，并冻结基础依赖。

### 阶段 1：关闭所有 P0 数据一致性问题

1. 引入 `DocumentSession` 和 session guard。
2. 统一人工编辑写入路径。
3. 所有翻译/图片/保存事件携带 `session_id + run_id/attempt_id`。
4. dirty 独立于 autosave，加入 Save/Discard/Cancel。
5. 导入失败不提交 session，保存失败不退出。

阶段 1 每项都必须先写失败回归测试，再修改生产代码。

### 阶段 2：线程、凭据和窗口生命周期

1. 将所有 worker -> Tk 回传统一到 mailbox/pump。
2. UI 完整消费 SecretSaveResult。
3. 队列 manager 归应用生命周期所有；窗口只订阅。
4. EPUB 解析实现可取消、幂等清理。
5. 图片取消令牌下沉到请求/下载/落盘，Manga worker 协议增加 deadline 与单 reader。
6. 封闭图片本地路径和远程下载的信任边界。

### 阶段 3：质量门禁与依赖治理

1. CI 加 Ruff 风险规则、format check、pytest、coverage baseline。
2. Python 3.11 必跑；根据声明决定 3.12/3.13 支持或加上限。
3. wheel install smoke 和真实 PyInstaller launch smoke。
4. lock/constraints、hash、依赖审计、根 LICENSE、单一版本源。

### 阶段 4：UI/UX 与可访问性

1. 收敛主命令、保存语义和质检名称。
2. 设置页滚动/缩放、全局字号、系统主题与对比度。
3. 完整键盘、焦点、屏幕阅读器和高 DPI 验收。
4. 10,000 行、5,000 术语、50 任务的性能预算与回归测试。

## 10. 建议质量门禁

基础命令：

```powershell
python -m ruff check main.py build.py src tests tools
python -m ruff format --check main.py build.py src tests tools
python -m pytest -q
python -m pytest --cov=src --cov-branch --cov-report=term-missing
python -m pip wheel . --no-deps --no-cache-dir --wheel-dir dist/wheels
```

短期不要直接用 100% 或高覆盖率阻塞历史代码。先记录稳定绿色基线并禁止下降；P0 涉及的 session、编辑、保存、导入、关闭和跨线程事件必须达到分支全覆盖。

真实发布 smoke 至少验证：

1. 解压/安装产物。
2. 启动到首帧。
3. 打开设置但不写真实凭据。
4. 导入小型 TXT 和 EPUB fixture。
5. 使用 fake provider 完成 2 行翻译。
6. 保存并重新打开，内容一致。
7. 正常关闭，进程和后台线程退出。

## 11. 桌面 UI 验收矩阵

| 维度 | 最低覆盖 |
| --- | --- |
| Windows | Windows 10/11，100%、125%、150%、200% DPI |
| macOS/Linux | 至少各一次默认主题、窗口缩放和字体回退检查 |
| 分辨率 | 800x600、1366x768、1920x1080 |
| 主题 | 默认、暗色/高对比（平台可用时） |
| 输入 | 鼠标、纯键盘、中文/日文输入法、超长多行文本 |
| 辅助技术 | Windows Narrator 或 NVDA；检查焦点顺序、按钮名称和异步状态 |
| 大数据 | 10,000 行、5,000 术语、50 队列任务 |
| 故障 | 无网络、401、429、超时、磁盘满、只读文件、文件锁、损坏 EPUB |
| 会话竞态 | 翻译中导入/粘贴/编辑/关闭/切项目；旧事件不得污染新 session |

关键 UI 不应只靠截图验收。每个主要流程需要同时检查：可见结果、键盘路径、焦点位置、文件实际内容、后台线程状态和重新启动后的恢复结果。

## 12. 与现有文档的关系

- `docs/PYTHON_ARCHITECTURE_REVIEW_AND_REFACTORING_PLAN.md` 提供长期分层方向。
- `docs/UX_FUNCTIONAL_OPTIMIZATION_ROADMAP.md` 提供功能路线图，其中缺失行翻译、保存状态、密钥环、部分成功等已有部分实现。
- 本文记录 2026-07-17 当前工作区的实现差距和发布优先级。本文 P0 顺序应覆盖旧文档中较低优先级的视觉或功能扩展。
- 队列并发新模块稳定后，应对 `QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md` 单独做实现对照与并发压力审查，不在本动态快照中重复下结论。

## 13. 参考资料

- Python PEP 8: <https://peps.python.org/pep-0008/>
- Python Tkinter threading model: <https://docs.python.org/3/library/tkinter.html#threading-model>
- Python Packaging User Guide, `pyproject.toml`: <https://packaging.python.org/en/latest/guides/writing-pyproject-toml/>
- WCAG 2.2 contrast minimum: <https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html>
- Web Interface Guidelines（审查时读取的最新源）: <https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md>
- 本地技能基线：`ui-ux-pro-max` 与 `web-design-guidelines`。

## 附录：`creative_proposal.html`

该文件当前不是生产 UI。若未来对外发布，应先处理：

- 页面声称支持 PDF、效率提升 300% 等与当前产品能力不一致的文案。
- Emoji 图标、非语义标题、缺少 reduced-motion 和部分低对比文本。
- 320px 下 `minmax(280px)` 的潜在横向溢出。
- 对外演示应接真实产品状态或明确标注概念稿，避免把不可用功能展示为已交付能力。
