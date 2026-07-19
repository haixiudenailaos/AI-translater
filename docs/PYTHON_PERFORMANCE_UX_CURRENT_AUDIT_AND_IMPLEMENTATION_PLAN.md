# Python 最佳实践、应用性能与用户体验当前审查及实施计划

> **历史基线提示（2026-07-18）**：本文件记录的是较早代码快照，其中多个 P0/P1 已在当前实现中修复。后续实施请以 `PYTHON_BEST_PRACTICES_PERFORMANCE_UX_OPTIMIZATION_GUIDE.md` 为准；本文件仅保留作问题演进背景。

审查日期：2026-07-18（Asia/Shanghai）  
审查对象：`D:\AI-translater-1.6` 当前工作区  
文档用途：保留较早代码快照的审查背景，不再作为当前实施入口  
当前实施顺序、验收指标和剩余风险请参见 `PYTHON_BEST_PRACTICES_PERFORMANCE_UX_OPTIMIZATION_GUIDE.md`。

## 1. 执行结论

当前项目已经达到“有明确架构和可靠性基础”的水平，明显优于一般的 Tkinter 单体工具，但还不能认定为完全符合 Python 最佳实践或稳定版就绪。

结论分三部分：

| 维度 | 当前结论 | 主要理由 |
| --- | --- | --- |
| Python 工程实践 | 基本符合，仍需收敛 | 已有组合根、分层、Protocol、类型门禁、原子写和 CI；依赖锁、退出结果、资源关闭、复杂模块边界仍有缺口 |
| 应用性能 | 主翻译链路较好，大文件和队列仍有高收益热点 | HTTP 复用、流式合并、分块加载、后台自动保存已完成；导出、队列快照、checkpoint 和部分导入仍存在主线程或 O(N x 批次数) 开销 |
| 用户体验 | 核心闭环基本形成，状态正确性仍有阻断项 | 已有新手引导、撤销、关闭保护、后台任务和错误提示；但关闭放弃、流式 dirty、自动补译三个状态错误会破坏用户信任 |

当前确认的最高优先级问题有 3 个：

1. 用户选择“放弃更改”后，关闭流程仍可能触发自动保存。
2. 流式预览已写入业务模型，但没有可靠进入 dirty、自动保存和关闭保护。
3. 自动查漏补译漏传 `run_id`，成功、失败回调都会因签名不匹配而崩溃。

在这 3 项关闭前，不建议把当前快照作为稳定版发布。除此之外，没有发现需要推倒现有分层或更换 GUI 框架的理由。

## 2. 审查范围与验证边界

### 2.1 范围

- 第一方 Python：`main.py`、`build.py`、`src/**`、`tests/**`、`tools/**`。
- 工程配置：`pyproject.toml`、`requirements*.txt`、PyInstaller spec、GitHub Actions、README。
- 用户链路：启动、配置、导入、翻译、流式进度、停止、补译、队列、保存、导出、关闭和恢复。
- `third_party/manga-image-translator/**` 不评价上游代码风格，只检查本项目的依赖、启动和进程边界。
- `workspace/**`、缓存和构建产物不纳入源码质量结论。

### 2.2 静态基线

- 第一方 `src` 下约 93 个 Python 文件、1016 个函数/方法、137 个类。
- 当前约有 240 处 `except Exception/BaseException` 和 94 个独立 `pass`。很多位于 UI、线程或清理边界，不能机械删除，但需要按风险逐步收窄。
- 最大模块：
  - `src/ui/settings_window.py`：1667 行。
  - `src/ui/main_window.py`：1594 行。
  - `src/core/queue_scheduler.py`：1465 行。
  - `src/ui/translation_controller.py`：1303 行。
  - `src/core/translator.py`：1024 行。
- 最大函数：`EPUBProcessor.import_epub()` 约 498 行，`create_translation_tab()` 约 342 行，`TranslatorEngine._translate()` 约 316 行。

行数本身不是缺陷。真正的问题是这些模块同时承担命令编排、状态、I/O、线程、错误转换和 UI 渲染，使修复状态错误或性能热点时需要修改过多边界。

### 2.3 动态验证

当前机器默认解释器为 CPython 3.10.9，而项目声明 `requires-python >=3.11`，因此本轮动态结果不能替代干净 Python 3.11 门禁：

- `pytest --collect-only` 收集约 920 个测试，3 个模块在收集阶段因 Python 版本和本机依赖不完整报错。
- 配置、路径、原子写、自动保存和文档会话专项：103 passed。
- 性能、启动和事件邮箱专项：94 passed、1 failed；失败是 Python 3.10 对 `frozen + slots` 未声明属性抛出 `TypeError`，项目测试按 3.11 的 `AttributeError` 断言。
- 更大测试子集在 120 秒内未跑完；不能据此声明全量绿色。
- 本机没有安装 Ruff 和 Pyright，未复跑静态门禁。
- Text 开发环境的独立进程 `import main` 约 217-271ms；`-X importtime` 显示模块累计约 156-166ms。Full Edition 未安装完整 Manga 依赖，未量化其冷启动。
- 未执行真实 Windows/macOS 打包产物、NVDA/Narrator、多 DPI、真实大 EPUB 和真实模型端到端测试。

## 3. 已符合最佳实践的部分

以下设计应保留，不建议在后续优化中回退：

1. `src/bootstrap.py` 已承担组合根职责，路径、密钥、配置和 edition 能力集中组装。
2. `domain/application/infrastructure/ui` 分层和 Protocol 端口已经建立，核心领域对象大量使用不可变 dataclass。
3. `AppPaths` 将资源目录、用户数据、配置、日志和工作区分离，并允许测试注入。
4. 配置写入、项目写入和映射写入使用原子替换；`atomic_file.py` 已包含同目录临时文件、`fsync` 和有限重试。
5. API Key 通过 keyring 存储；远程自定义 API 强制 HTTPS；日志有中央脱敏和轮转。
6. 主翻译使用持久 `httpx.Client`、连接池、分项超时、HTTP/2、有限重试和 Provider 级限流。
7. 流式事件通过不可变事件和 mailbox 回到 Tk 主线程，并合并到约 20Hz；工作线程不直接操作 Tk。
8. 主表、任务详情和术语表已有 250 行分块加载、搜索 debounce、缓存行值和增量更新。
9. 文档采用 `TranslationDocument` 作为业务状态，Treeview 主要作为视图；人工校对行有覆盖保护。
10. 自动保存具备 debounce、最大延迟、generation、single-flight 和后台写入。
11. 大 EPUB 导入和队列批量导出已有后台任务、取消和迟到回调隔离。
12. CI 已有 Ruff、format、Pyright、pytest coverage、wheel smoke，以及 Windows/macOS、Text/Full 构建矩阵。

## 4. 优先级定义

| 优先级 | 定义 |
| --- | --- |
| P0 | 违反明确用户选择、可丢失/错存数据，或核心流程稳定崩溃；发布前必须关闭 |
| P1 | 核心可靠性、性能、恢复或主要体验明显受损，但有绕行路径或只在特定规模/故障下触发 |
| P2 | 主要影响可访问性、长期维护成本、边缘性能和工程一致性 |

工作量标记：S 约半天到 1 天，M 约 2-4 天，L 约 1-2 周。它们只表示相对规模，不是承诺工期。

## 5. P0：发布阻断项

### P0-1 放弃更改仍可能保存

**证据**

- `main.py:88-97` 将 `discard` 和 `proceed` 都送入 `MainWindow.close()`。
- `src/ui/main_window.py:1585-1592` 在 `close()` 中无条件调用 `flush_pending_save()`。
- `src/application/autosave.py:200-202` 会在 `DIRTY` 或 `SAVE_FAILED` 状态启动一轮保存。

**用户影响**

用户明确选择“放弃更改”，debounce 中的编辑仍可能写入目标文件。界面承诺与磁盘结果相反，属于数据语义错误。

**推荐实现（M）**

1. 定义 `CloseDecision`：`SAVE / DISCARD / CANCEL / PROCEED`，不要继续传递裸字符串。
2. 关闭询问前先让自动保存进入 `quiescing`：冻结 generation，阻止新调度，并取得是否有在途保存的快照。
3. `MainWindow.close(decision)` 明确分支：
   - `SAVE`：flush，只有成功才继续关闭。
   - `DISCARD`：取消 debounce，作废未开始 generation，不得调用 flush。
   - `CANCEL`：恢复自动保存调度并返回界面。
   - `PROCEED`：仅在确实 clean 时关闭。
4. 若询问期间已有保存正在写盘，先完成该保存并重新计算 dirty；如仍需提供“放弃”，必须保留最后成功快照并原子恢复，不能假装运行中的写入可取消。
5. 文档保存和配置保存使用两个独立结果，避免“放弃文档更改”顺带跳过无关设置保存。

**必须新增的测试**

- `DIRTY + discard`：保存函数调用次数为 0，磁盘保持旧内容。
- `SAVE_SCHEDULED + discard`：after 被取消，不启动 worker。
- `SAVING + close`：完成当前 generation 后重新判断，不重复保存。
- `SAVE_FAILED + save/discard/cancel` 三分支。
- 无目标路径的剪贴板会话 Save As、Discard、Cancel。

**完成定义**

Save、Discard、Cancel 三种选择在 UI、模型、autosave 状态和磁盘内容上完全一致。

### P0-2 流式预览写入模型但没有可靠 dirty 语义

**证据**

- `src/ui/translation_controller.py:578-601` 的流式分支调用 `_apply_to_document()`，已修改 `TranslationDocument`。
- 只有非流式批次在 `:624-633` 调用 `schedule_save()`。
- 全文终态处理 `:741-782` 没有保存兜底；选中行和查漏也采用“流式先写模型、非流式有变化才保存”的模式，见 `:814-862`、`:1027-1072`。
- 现有兜底并非完全缺失：选中行/查漏仅在 `SUCCEEDED` 终态于 `:880-881`、`:1111-1113` 立即保存，用户显式停止也会在 `:322-323` 保存；它们没有覆盖 `PARTIAL/CANCELLED/FAILED` 或未经过显式停止的终态。
- `src/ui/main_window.py:1225-1237` 的关闭保护检查 session、`_unsaved_edits` 和 autosave 状态，但不检查 `document.dirty_indices`。

**用户影响**

如果流式预览已经写入最终文本，而后续批次完成事件写入相同值，`update_target()` 会返回未变化，批次保存不会被调度。全文模式的各类终态都可能保留这种隐式更改；选中行/查漏的成功终态和显式停止已有保存兜底，但部分成功、取消、失败等路径仍有缺口。此时关闭、切换文件或异常退出，界面可见译文可能没有关闭提示，也没有落盘或恢复点。

**推荐实现（M）**

优先采用“预览不进入业务模型”的契约：

1. 流式事件只写 `StreamingPreviewStore` 和 Treeview 临时显示，不调用 `TranslationDocument.update_target()`。
2. `BATCH_COMPLETED` 只提交已确认成功的完整行到 `TranslationDocument`，随后一次性标记 dirty 和调度保存。
3. `RUN_CANCELLED/RUN_FAILED` 清除未提交预览，保留之前已完成批次。
4. 预览必须携带 `run_id + batch_id + document/session_id`，旧会话事件不能渲染到新文档。
5. 如果坚持让预览进入模型，则第一次实际变化时必须立即进入 dirty/close guard，并明确取消时是否保留部分行；不建议混用“预览”和“最终数据”语义。

**必须新增的测试**

- 全文模式在 `SUCCEEDED/PARTIAL/CANCELLED/FAILED` 下，流式值与最终值相同仍进入明确的保存或未保存状态。
- 选中行/查漏覆盖成功、部分成功、取消和失败；成功与显式 Stop 的现有兜底不得重复保存，其余终态不能遗漏 dirty。
- 流式中关闭、停止、导入新文件，分别验证 Save/Discard/Cancel。
- 取消时只保留已完成批次，不保留半行预览。
- 旧 run/session 的迟到流式事件被丢弃。
- 自动保存关闭时，流式完成仍进入关闭保护。

**完成定义**

任何用户可见的最终译文都处于“已保存、明确未保存、可恢复预览”三种状态之一，不能出现第四种隐式状态。

### P0-3 自动查漏补译漏传 run_id

**证据**

- `src/ui/translation_controller.py:977-984` 创建 `run_id` 后没有保存或传给 `_translate_missing_worker()`。
- `:991-995` 直接传 `_publish_progress_event`、`_publish_terminal_event`，但其签名在 `:444`、`:487` 要求 `run_id`。
- 异常路径 `:996-999` 又调用缺少 `run_id` 的 `_publish_run_failed()`。

**用户影响**

自动查漏一旦真正发出回调会产生 `TypeError`；异常处理再次产生 `TypeError`，终态事件无法发布，按钮可能一直停在忙碌状态，剩余空行不会补译。

**推荐实现（S）**

1. `run_id = self._new_run_id("missing")` 后作为 worker 参数传入。
2. 与全文、选中行保持同一模式：回调使用闭包显式绑定 `run_id`。
3. 让三个模式共用一个 `_run_translation_worker(content, run_id)`，只由 mode 决定渲染，减少签名再次漂移的机会。
4. worker 的所有退出路径只发布一个结构化终态；`finally` 如用于兜底也不得操作 Tk。按钮、临时字段和 busy 状态必须由 Tk 主线程的统一 reducer 恢复。

**必须新增的测试**

- 查漏成功、部分成功、失败、取消均发布正确 run 的终态。
- worker 自身抛异常时只出现一个结构化失败事件。
- 旧查漏 run 的迟到事件不能覆盖新任务。

**完成定义**

自动查漏所有终态都能恢复按钮、清理临时字段，并在达到轮次上限时给出可重试状态。

## 6. P1：Python 工程与可靠性

### ENG-1 退出时配置保存结果被丢弃（S）

**证据**：`src/config/config_manager.py:571-576` 调用三个有返回值的保存方法但不汇总；`main.py:99-104` 不检查结果，异常时仍销毁窗口。

**改动**

- 新建 `ConfigSaveResult`，分别记录 API metadata、密钥、应用设置、术语表的状态和错误。
- `save_config()` 返回聚合结果；失败时允许“重试 / 不保存退出 / 取消退出”。
- 关闭资源和保存数据分阶段执行，不用一个宽捕获覆盖全部阶段。
- 错误详情写脱敏日志，界面显示可操作摘要。

**验收**：模拟磁盘满、只读目录、keyring session-only/failed；窗口不会静默退出，用户选择与最终持久化一致。

### ENG-2 API 预设隐式导入和密钥三态语义错误（S）

**证据**

- `src/ui/settings_window.py:1579` 使用 `tk.simpledialog`，却没有显式导入该子模块。当前正常主窗口路径会先导入 `filedialog`，而 CPython 的 `filedialog` 恰好间接注册 `simpledialog`，因此通常可用；但直接导入/测试 `settings_window` 或改变导入顺序时该属性不存在，属于依赖标准库内部导入副作用的脆弱契约。
- `src/config/config_manager.py:679` 将 `SESSION_ONLY` 当普通成功；设置界面随后一律提示“已保存”。

**改动**

- 显式 `from tkinter import simpledialog`，并设置 `parent`。
- 预设保存返回 `PERSISTED / SESSION_ONLY / FAILED`，与 API 设置主流程复用同一结果类型。
- `SESSION_ONLY` 必须提示“仅当前会话有效，重启后需重新输入”，不能写成永久保存成功。

**验收**：三种密钥后端状态均有 UI 和测试；新增、覆盖、取消预设不修改无关配置。

### ENG-3 依赖、Python 版本和发布物不可复现（M-L）

**证据**

- `pyproject.toml:9` 声明 `>=3.11`，包含未验证的 3.12/3.13。
- `requirements-image-manga.txt:9-10` 明确上游只支持 `<3.12`，CI 只跑 3.11。
- 图片依赖主要使用范围版本；额外索引的 `rusty-manga-image-translator` 没有精确版本和 hash。
- CI 先安装 `.[dev]`，再重复安装 `requirements.txt`，存在双源漂移。
- `src/core/image_utils.py:47-55` 动态使用 `cairosvg`，但项目依赖没有声明它。

**改动**

1. 先明确支持矩阵：
   - 如果整个应用只支持 3.11，设 `requires-python = ">=3.11,<3.12"`。
   - 如果 Text 支持 3.12/3.13 而 Full 只支持 3.11，必须拆 edition 安装契约和 CI 矩阵。
2. 让 `pyproject.toml` 成为抽象依赖唯一来源；按 OS/edition 生成带 hash 的 constraints/lock。
3. 发布 CI 使用锁文件和 `--require-hashes`，额外索引包固定版本与 hash。
4. 将 `cairosvg` 加入明确 extra/Full lock，或在图片预检中把 SVG 转换标记为不可用，不能只写日志后跳过。
5. GitHub Actions 固定到提交 SHA；生成依赖清单、checksum 和 SBOM。
6. 源码入口在 Python 版本不支持时立即给出中文错误，不要运行到队列或测试阶段才失败。

**验收**：相同提交在干净 runner 重建两次，依赖版本与产物 checksum 可解释；3.11 Text/Full 安装和 smoke 通过，未支持版本在安装或启动前失败。

### ENG-4 HTTP 客户端生命周期竞态（M）

**证据**

- `src/api/base_api.py:174-230` 的 `_get_client()`、重建、取消关闭没有对客户端指针做完整原子交换。
- 主翻译可在 `src/core/translator.py:498` 并发发请求。

**改动**

- 为客户端指针增加独立 lifecycle lock；双检创建、原子 swap，旧客户端在锁外关闭。
- request lease 与 cancel generation 绑定，取消后旧请求不能复用新客户端。
- 关闭时先拒绝新 lease，再等待在途请求到有界 deadline；超时必须留下可诊断结果。

**验收**：高并发下反复“开始 -> 取消 -> 立即开始”无 closed-client、重复建连风暴或旧请求污染新 run；关闭时间有上界且结束后无活动请求。

### ENG-5 类型、异常和模块边界未覆盖最高风险代码（L，渐进实施）

**证据**

- Pyright 严格范围主要是 `domain/application`，CI 未覆盖 `api/core/config/ui` 的复杂并发路径。
- 当前约 240 个宽异常捕获。
- `queue_scheduler.py:330,791` 调用翻译引擎私有方法；`concurrent_window.py:514` 读取 manager 私有 EPUB 处理器；`settings_window.py:1366` 调用配置私有方法。
- `image_translation_service.py:121` 的兼容分支仍从 application 导入 infrastructure。
- `queue_scheduler.py:1427-1429` 持普通锁调用再次获取同一锁的 `get_task_data()`；该未使用路径一旦接入会自死锁。

**改动顺序**

1. 先修潜伏死锁，不要把 `Lock` 换成 `RLock` 掩盖边界问题。
2. 定义公开端口：`TranslationBatchExecutor`、`QueueTaskReader`、`ExportService`、`DefaultPromptProvider`。
3. 让生产组合根强制注入依赖，删除 application/config 中的 infrastructure fallback。
4. Pyright 先让全部 `src` 进入 basic，再按 `api -> core queue -> config -> 新 UI adapter` 扩大 strict。
5. Ruff 渐进启用 `BLE/TRY/S/RUF/PERF`；只对新增或已迁移模块设零基线。
6. 捕获 `queue.Empty`、`OSError`、`JSONDecodeError`、`httpx` 异常等具体类型；UI/线程顶层允许宽捕获，但必须转换为结构化结果并记录上下文。

**建议的垂直拆分**

| 当前模块 | 首先提取的边界 |
| --- | --- |
| `translation_controller.py` | `TranslationRunCoordinator`、`TranslationEventPresenter`、`ExportCommand` |
| `queue_scheduler.py` | `BatchExecutor`、`CheckpointPolicy`、只读 `QueueProjection` |
| `settings_window.py` | API、翻译、图片三个 panel + 独立 preset service |
| `epub_processor.py` | import orchestration、archive reader、mapping writer |
| `base_api.py` | client lifecycle、retry policy、stream decoder、metrics |

每次只迁移一条真实生产路径，迁移完成后删除旧入口，避免形成第三套兼容架构。

### ENG-6 Python 包布局和 wheel 入口不标准（P2/L，建议放到 2.0）

**证据**：`pyproject.toml:96-99` 实际安装名为 `src` 的顶层包；没有 `[project.gui-scripts]`；wheel smoke 只验证 `import src`。

**改动**

- 长期迁移为 `src/ai_translater/` 或新的稳定包名，配置 `package-dir`。
- 将 composition root 放入包内，增加 `__main__.py` 和 `[project.gui-scripts]`。
- wheel smoke 在隔离目录真正启动诊断入口，而不是只 import 泛化包名。
- 版本由一个源生成 pyproject、spec、workflow artifact 和 macOS plist，移除散落的 `1.6`。

这项不应挤占 P0/P1，也不应与当前性能修复放在同一提交。

### ENG-7 旧 BatchProcessor 兼容路径待删除或补齐关闭语义（P2/S-M）

**证据**

- `src/api/base_api.py:136-150` 明确主文本翻译走 `translate_stream_enhanced()`，旧 `translate_batch()` 才会延迟创建 `BatchProcessor`；仓库内未发现该 BaseAPI 方法的生产调用。
- `src/core/batch_processor.py:27-48` 使用 `shutdown(wait=False)`，运行中的 handler 没有协作式取消和有界等待。

**改动**

1. 先确认该旧 API 是否属于仍需支持的外部兼容契约；如果不是，删除 `BaseAPI.translate_batch()`、延迟构造和对应配置，避免维护不可达并发路径。
2. 如果必须保留，加入取消令牌和显式生命周期：拒绝新任务、取消 pending、等待运行任务到 deadline，并报告未退出任务。
3. 不要依赖 `ThreadPoolExecutor` 的进程退出清理，也不要把它与当前生产 HTTP 客户端竞态放在同一优先级。

**验收**：删除方案需证明内部调用和兼容测试已迁移；保留方案需覆盖运行中关闭、重复关闭、handler 卡住和 deadline 超时。

## 7. P1：应用性能

### PERF-1 单本 EPUB 导出阻塞 Tk，ZIP 后处理全量驻留（M-L）

**证据**

- `src/ui/translation_controller.py:1230` 在 UI 回调中同步保存映射和调用导出。
- `src/infrastructure/exporter.py:104,143,233` 执行源文件哈希、EPUB 重读、DOM 解析和压缩写出。
- `exporter.py:270` 附近的 ZIP 后处理同时保留条目列表和新的 `BytesIO`。

**改动**

- 新建 application 层 `ExportJob`；主线程只选择路径、进入 busy、消费进度和显示终态。
- 提供阶段进度：校验、保存映射、重写章节、写图片、压缩、校验、原子替换。
- 支持取消；取消只删除临时输出，不能破坏已有目标文件。
- ZIP 后处理改为临时文件间逐条流式复制，不把整个 EPUB 放进内存。
- 导出完成后做最小完整性验证，再原子替换目标。

**验收预算**

- 200 章/500 图 fixture 导出期间 Tk 心跳最大停顿不超过 100ms。
- 取消后 2 秒内进入已取消终态，目标旧文件完整。
- 额外 Python 内存不随 EPUB 总大小线性翻倍；记录峰值 RSS 和最大单条目大小。

### PERF-2 主窗口 TXT 和队列批量导入仍有同步重 I/O（M）

**证据**

- `src/ui/concurrent_window.py:381-399` 在主线程逐个调用 `manager.add_task()`。
- `src/core/concurrent_manager.py:149` 同步解析 EPUB/读取 TXT。
- `src/ui/file_importer.py:229-243` 的 TXT 路径同步整文件读取、编码检测和目标文件创建。

**改动**

- TXT/EPUB 统一使用有界 `ImportJobQueue`；worker 只做磁盘和解析，主线程原子提交 session/任务。
- 批量导入显示当前文件、完成数、失败数、取消和“仅重试失败项”。
- 大 TXT 使用增量解码/行迭代；目标文件只在导入验证成功后原子创建。
- EPUB 解析已有取消令牌，应把 TXT 和队列入口复用同一 command，而不是再建新线程模型。

**验收预算**：100MB TXT、50 个文件、200 章 EPUB 导入时 Tk 心跳持续；取消有界完成；失败导入不替换旧 session。

### PERF-3 队列 UI 每 500ms 复制所有任务全文（M）

**证据**

- `src/ui/concurrent_window.py:293-348` 已读 `QueueSnapshot`，仍调用 `get_all_tasks()`。
- `src/core/queue_scheduler.py:1407-1424` 为每个任务复制 `source_lines/target_lines`。
- `src/ui/task_detail_window.py:154-200` 每 500ms 分别获取状态和整份译文。

**改动**

- 列表页只消费轻量 `QueueListSnapshot`：task id、文件名、状态、进度、错误摘要、版本号、在途数。
- 详情页首次取得 immutable source tuple，后续只按 `target_version` 获取 changed rows/delta。
- UI 只更新值发生变化的 task/row；无变化轮询不创建全文列表。
- 快照发布与 UI 读取都记录 generation，避免跨锁读取不一致。

**验收预算**：50 个任务、每个 10,000 行，空闲 60 秒内全文列表复制次数为 0；主线程刷新 p95 小于 16ms、最大值小于 50ms。

### PERF-4 每批完成都在调度锁内复制整本译文（M）

**证据**

- `src/core/queue_scheduler.py:1243-1259` 每次 checkpoint 标记立即复制 `target_lines`。
- EPUB 保存闭包在 `:1261-1269` 再复制一次。
- 磁盘保存虽 debounce/single-flight，但内存快照创建没有 debounce。

**改动**

- outcome 只更新 generation 和 dirty range；debounce 真正到期时再在短锁内创建一次不可变快照。
- 移除 EPUB 的第二次复制。
- 更大规模时可使用分块 immutable vector 或增量 journal；先用单次延迟快照验证收益，不要过早引入复杂存储。

**验收预算**：10,000 行、500 个批次时，全文快照次数接近实际保存次数，而不是批次数；锁持有 p99 有明确上限。

### PERF-5 Full Edition 用真实深度学习包导入判断能力（S-M）

**证据**

- `main.py:46-70` 在 Tk `after_idle` 回调里同步初始化。
- `src/domain/edition.py:61-80` 真实执行 `import manga_translator`。
- 上游 `manga_translator/__init__.py` 继续导入主模块；Full spec 将其加入导入路径。

**改动**

- Text/Full spec 在构建时写入静态 edition marker 或环境资源，由 `EditionCapabilities` 读取。
- 运行时能力验证分两层：edition 声明是同步轻量操作；模型/依赖健康检查在首次使用或设置页后台执行。
- 为启动阶段加低频计时：first paint、context ready、main window interactive、first API ready。

**初始预算**：Text 首帧小于 500ms、可交互小于 1.5s；Full 首帧不因 torch/cv2 import 停住。预算需在目标机器实测后冻结。

### PERF-6 低优先级热点

| 热点 | 当前证据 | 建议 |
| --- | --- | --- |
| UI callback mailbox 无界且每轮排空 | `ui_callback_mailbox.py:45-78,147-167` | 每轮按回调数或 8-12ms 时间预算 drain；对 refresh/title/progress 做 keyed coalescing |
| `SmartCache.max_memory_size` 实为条目数 | `smart_cache.py:14-81` | 改名或按估算字节限制；用 `OrderedDict`/成熟 LRU 做 O(1) 淘汰 |
| 搜索/筛选对全部 Treeview 行操作 | `main_window.py:786-851` | 模型侧搜索、只变更可见性差异，并对大量 Tcl 调用分块 |
| 图片内部多次 Base64 编解码 | `image_asset_store.py:164`、`image_utils.py:17`、`image_translator.py:697` | 内部传 bytes，只在 HTTP JSON 边界编码一次 |
| EPUB 重开重复解析稳定源文件 | `epub_processor.py:129,293` | 用 size/mtime + hash 验证 mapping，命中后直接加载已有映射 |
| 队列每任务各有 engine/client/heartbeat | `queue_scheduler.py:787` | 在取消隔离不变的前提下，按 ProviderRuntimeKey 评估共享传输层；必须先压测再实施 |

## 8. P1/P2：用户体验

### UX-1 所有翻译入口必须单飞，避免重复付费请求（S-M）

**证据**

- `src/ui/main_window.py:557-558` 全局绑定 F5/F6。
- `_run_primary_action()` 和 `TranslationController.start_translation()` 没有入口级 busy 幂等保护。
- 按钮虽会禁用，但快捷键和菜单仍可直接调用命令。

**改动**

- 建立单一 `TranslationCommand` 和 `RunState(IDLE/STARTING/RUNNING/CANCELLING/TERMINAL)`。
- 按钮、菜单、快捷键全部调用同一 `can_execute/execute`。
- `STARTING/RUNNING/CANCELLING` 再次触发只聚焦状态区，不创建线程或请求。
- F6 若保留，应只代表明确的“恢复暂停任务”；当前“继续翻译”和“翻译未完成行”语义接近，建议合并。

**验收**：连续按 F5/F6 20 次只产生一个 run id、一个 worker 和一组请求；取消未完成前不能启动下一 run。

### UX-2 TXT 队列需要跨重启续传（M）

**证据**

- `src/core/concurrent_manager.py:164` 导入 TXT 时目标列始终从空列表开始。
- checkpoint 实际只写入 `_译文.txt`，见 `queue_scheduler.py:1273`，没有源指纹、完成状态或人工编辑元数据。
- `src/infrastructure/project_repository.py:40-190` 和 `src/domain/project.py:127-171` 已有路径 + SHA-256 稳定 ID、译文、人工编辑/失败行、原子保存和检查点模型，但尚未接入生产队列。

**改动**

- 将现有 `ProjectRepository/TranslationProject` 接入队列，作为 TXT 恢复的唯一状态源；`_译文.txt` 只作为导出或一次性旧格式迁移来源。
- 持久化源文件 SHA-256、原文/译文、人工编辑、失败行、模型快照和显式 `completed_indices`（或等价行状态）；不能用“译文非空”推断完成，因为空字符串可能是有效结果。
- 加入 TXT 任务时按路径 + fingerprint 查找项目；指纹变化时给出“新任务 / 尝试映射 / 放弃旧进度”选择，并为 schema version 提供迁移策略。
- 已完成和人工编辑行不再请求；队列恢复结果显示项目来源、恢复行数、失败行和配置差异。

**验收**：重启后重新加入同一 TXT，不重复请求已完成行，不覆盖人工译文；内容变化但行数相同不会误复用；有效空译文与未完成行可区分；损坏或旧版元数据有明确恢复/迁移结果。

### UX-3 队列错误要显示原因和恢复动作（S-M）

**证据**

- 任务模型已有 `error_message`，但队列表格和详情主要只显示状态、进度。
- `src/application/error_handling.py:30` 已有错误分类器，却没有接入生产 UI。
- 多个 UI 边界仍直接显示原始 `{e}`。

**改动**

- 所有异常先转换为 `ActionableError(category, safe_message, recommended_action, retryable, correlation_id)`。
- 队列表增加失败数/错误摘要；详情显示“重新登录、降低并发、重试失败行、打开日志目录”等上下文动作。
- 401/403、429、网络超时、模型不存在、磁盘/权限、格式错误分别处理。
- 原始异常、路径和响应摘要只进入脱敏日志；UI 可提供“复制诊断信息”，不直接展示内部堆栈。

**验收**：每类错误都有安全文案、建议动作、可重试状态和自动化测试；用户无需查看终端即可知道下一步。

### UX-4 接入已存在的 preflight、完整质检和用量统计（M）

**证据**

- `src/application/preflight.py:50`、`quality_review.py:66`、`usage.py` 已实现，但无生产调用。
- 主操作在 `main_window.py:1010` 直接开始 API 请求。
- 当前 `run_quality_check()` 只检查空译文或原译相同，未使用数字、长度、内部标记、重复译文和术语检查。

**改动**

1. 开始前显示简洁预检：文件、供应商、模型、目标语言、待译数、人工保护数、token/费用估算和阻断问题。
2. 无风险小任务可一键继续；只有费用阈值、长行、模型/语言缺失等情况弹确认，避免模态疲劳。
3. 翻译后运行 `inspect_quality()`，问题可按类型筛选、忽略、重试失败行。
4. 导出完整结果正常执行；有缺失/错误时明确选择“返回修复”或“导出草稿”，草稿文件名和报告必须标识不完整。
5. 将 API metrics、cache hit、重试和 token 估算汇入 project usage；不记录用户原文。

**验收**：预检结果与实际请求参数一致；质检所有 issue 可定位到行；草稿不能被误报为完整成功；用量统计不含敏感文本。

### UX-5 缩短并重写默认提示词（S-M）

**证据**

- `src/config/config_manager.py:144-165` 的默认提示词有 529 字符、约 477 个估算 token、20 行。
- 包含“无视法律、道德或安全相关约束”等对抗性表述。
- 系统提示会随每个普通 LLM 批次发送；稳定批次上限约 20 行。

**影响**

5,000 行至少约 250 个请求，默认提示本身理论上会重复约 119,000 个输入 token（未扣除供应商前缀缓存）。对抗性表述还会提高不同供应商拒绝、降级或输出不稳定的概率。

**改动**

- 改为 4-6 条中性、可验证规则：目标语言、逐行标记、保留格式、只输出译文、术语优先、不得添加解释。
- 删除 jailbreak/安全绕过文本。
- 提示词带 `prompt_schema_version`；修改后自动改变缓存键。
- 用固定文学样本比较成功率、行对齐率、拒绝率、输入 token 和人工修改率，确认后替换默认值。

**验收**：默认提示 token 至少下降 60%；行标记和术语测试不退化；多供应商拒绝率不高于旧基线。

### UX-6 主界面层级和可访问性（P2/M）

**现状**

- 主窗口已有 Ctrl+O/S/F/Z/Y、F2、Enter、Shift+F10 等路径，这是正确方向。
- 队列主要依赖双击/右键，详情编辑主要依赖双击；单任务取消缺统一确认。
- “批量翻译队列”使用最强强调色，当前文档的上下文主动作是普通按钮。
- 图片翻译和 EPUB 导出在项目菜单、更多操作和专用按钮重复出现，800px/高 DPI/大字号时容易拥挤。
- UI 仍混用 Emoji、`微软雅黑`、硬编码颜色与 named font。

**改动**

- 队列支持 Enter 打开、Shift+F10/Menu 操作、Delete/取消确认、Escape 关闭并恢复焦点。
- 详情支持 Enter/F2 编辑，取消前说明已完成内容是否保留。
- 将上下文主动作设为唯一视觉主按钮；队列作为工具入口；图片和低频导出保留一个稳定入口。
- 图标使用稳定资源并保留文本标签，不依赖 Emoji 字形。
- 用 Tk named fonts 和 theme token 覆盖全局字体、surface、text、muted、accent、danger、focus。
- 真实验证 Windows 100/125/150/200% DPI、800x600/1366x768、字号 8/10/18、NVDA/Narrator。

**验收**：核心流程可纯键盘完成；焦点可见且关闭后回到发起控件；状态不只靠颜色；200% DPI 下无控件裁切。

## 9. 推荐实施顺序

### 阶段 0：状态正确性，必须先完成

1. P0-1 关闭 Save/Discard/Cancel 状态机。
2. P0-2 流式 preview 与最终 commit 分离。
3. P0-3 自动查漏 run_id 与统一 worker。
4. UX-1 入口单飞，防止重复请求。

建议拆成 4 个独立提交，每个提交都先加失败测试再改生产代码。

### 阶段 1：退出、恢复和错误闭环

1. ENG-1 配置保存聚合结果。
2. ENG-2 预设与密钥三态。
3. UX-2 TXT 队列恢复。
4. UX-3 ActionableError 接入队列和主流程。
5. ENG-4 HTTP 客户端有界关闭。

### 阶段 2：大文件和队列性能

1. 先建立固定性能 fixture 和阶段计时。
2. PERF-1 单本 EPUB 后台流式导出。
3. PERF-2 统一后台导入。
4. PERF-3 轻量队列 projection/delta。
5. PERF-4 checkpoint 延迟快照。
6. PERF-5 静态 edition marker。

### 阶段 3：产品体验与成本

1. UX-5 默认提示词精简并 A/B 基准。
2. UX-4 接入 preflight、quality、usage、草稿导出。
3. UX-6 键盘、焦点、DPI 和屏幕阅读器。
4. PERF-6 中经基准证明有收益的项目。

### 阶段 4：工程治理

1. 依赖锁、hash、SBOM 和 Python/edition 支持矩阵。
2. Pyright/Ruff 渐进扩面。
3. ENG-7 删除旧 BatchProcessor 兼容路径；如必须保留，再补齐有界关闭。
4. 按垂直用例拆热点模块并删除兼容 fallback。
5. 2.0 再处理标准 `src` 布局、GUI script 和包名迁移。

## 10. 测试与性能门禁

### 10.1 干净 Python 3.11 基线

Windows 示例：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check main.py build.py src tests tools
.\.venv\Scripts\python.exe -m ruff format --check main.py build.py src tests tools
.\.venv\Scripts\python.exe -m pyright src/domain src/application
.\.venv\Scripts\python.exe -m pytest -q
```

完成当前 P0 后，必须新增并单独门禁：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_main_window_dirty_state.py `
  tests/test_autosave_coordinator.py `
  tests/test_translation_stream_ui.py `
  tests/test_translation_event_mailbox.py
```

不要立刻设置任意高覆盖率。先记录当前 branch coverage，并禁止下降；P0 状态机、保存/关闭、run-id 和错误分支要求分支全覆盖。

### 10.2 固定性能场景

| 场景 | 建议规模 | 核心指标 |
| --- | --- | --- |
| Text 冷启动 | 干净配置、已有配置 | first paint、interactive、线程数、RSS |
| Full 冷启动 | 无模型/有模型缓存 | 同上，额外记录重型 import |
| 大 TXT | 100MB / 100,000 行 | 导入时间、最大 Tk 停顿、RSS |
| 大 EPUB | 200 章 / 500 图 | 导入、导出、取消、RSS、临时文件 |
| 队列 | 50 任务 x 10,000 行 | UI refresh、全文复制次数、GC、锁持有 |
| 长翻译 | 5,000 行 | TTFT、吞吐、重试、缓存、提示 token、费用 |
| 故障 | 401/429/超时/断流/磁盘满 | 终态、恢复动作、残留线程和请求 |

每个性能 PR 记录修改前/后、机器、OS、Python、fixture hash。没有基线数据时，不要仅凭行数或直觉宣布性能提升。

### 10.3 发布产物门禁

1. Windows 10/11 Text/Full：解压、显示首帧、fake 翻译、保存、重开、正常退出。
2. macOS Text/Full：`plutil`、真实入口、首帧、保存、退出；正式分发还需签名/公证策略。
3. Text 版不得导入 Manga；Full 版至少完成 provider health 和一张最小图片任务。
4. smoke 不能只判断“进程 15 秒未退出”；应由测试钩子报告 `main_window_interactive` 后再正常关闭。
5. 凭据不得进入 artifact、日志、配置样例、崩溃信息或诊断包。

## 11. 关键回归矩阵

| 主题 | 必测组合 |
| --- | --- |
| 关闭 | clean/dirty/scheduled/saving/failed x Save/Discard/Cancel |
| 流式 | full/selected/missing x success/partial/cancel/fail x close/replace |
| 会话 | 翻译中导入、粘贴、关闭、切项目；旧事件不得污染新 session |
| 队列 | TXT/EPUB 重启恢复、人工编辑保护、取消、失败重试、checkpoint 失败 |
| 导出 | 完整/部分/空译文、磁盘满、目标锁定、取消、旧文件保留 |
| 网络 | 401、403、408、429 + Retry-After、5xx、断流、取消后立即重启 |
| 图片 | SVG 依赖缺失、超大图、损坏图、路径越界、worker deadline/退出 |
| 可访问性 | 纯键盘、焦点恢复、200% DPI、大字号、NVDA/Narrator |

## 12. 不建议采用的做法

- 不要在关闭流程中继续使用模糊的布尔值或裸字符串组合状态。
- 不要用 `RLock` 掩盖递归取锁；先重画只读查询边界。
- 不要为了减少文件行数先做机械拆分；先提取可测试的 command/port/reducer。
- 不要把 Tk 调用移入 worker，也不要在 Tk 主线程等待无 deadline 的 Future/线程/子进程。
- 不要盲目增加并发；当前优先消除重复请求、复制和资源竞态。
- 不要缓存缺少 provider、model、temperature、prompt、glossary、normalization version 的翻译结果。
- 不要把流式半成品同时当作预览和已提交业务数据。
- 不要在没有固定 fixture 和前后数据时做微优化或更换 GUI 框架。

## 13. 总完成定义

当以下条件同时满足时，可把项目结论提升为“符合当前规模下的 Python、性能与桌面 UX 最佳实践”：

1. 3 个 P0 有自动化分支回归，Save/Discard/Cancel 与磁盘结果一致。
2. 干净 Python 3.11 的 Ruff、format、Pyright 和完整 pytest 全绿。
3. Text/Full 的 Python 与依赖契约明确、可复现，并有真实产物 smoke。
4. 大 TXT/EPUB 和 50 任务队列达到已冻结的 Tk 停顿、内存和取消预算。
5. 所有长任务都有 busy、进度、取消、失败和恢复终态。
6. preflight、完整质检、用量和草稿导出进入真实生产流程。
7. 关闭后无活动 HTTP 请求、ThreadPoolExecutor、Manga worker、Tk after 或保存线程。
8. 核心流程可纯键盘完成，并通过目标 DPI 和至少一种 Windows 屏幕阅读器实测。
