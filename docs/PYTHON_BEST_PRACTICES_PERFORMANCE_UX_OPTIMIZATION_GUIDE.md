# Python 最佳实践、应用性能与用户体验优化指导

审查日期：2026-07-18（Asia/Shanghai）  
审查对象：`D:\AI-translater-1.6` 当前工作区  
文档性质：当前代码基线审查、优化优先级和实施验收指南  
替代关系：本文件替代 `PYTHON_PERFORMANCE_UX_CURRENT_AUDIT_AND_IMPLEMENTATION_PLAN.md` 作为后续实施入口。旧文档中的多个 P0/P1 已被当前代码修复，不应直接照搬。

## 1. 执行结论

项目已经具备较成熟的桌面应用工程基础，不是简单的 Tkinter 脚本：已有组合根、分层、Protocol、原子写入、密钥环、后台任务、事件邮箱、自动保存状态机、队列调度器和较大规模的测试集。

当前结论如下：

| 维度 | 当前判断 | 核心原因 |
| --- | --- | --- |
| Python 工程实践 | 基本符合，但发布治理仍不完整 | 架构、原子写和测试基础较好；结果语义、类型覆盖、依赖锁、平台门禁和异常脱敏仍有确定缺口 |
| 应用性能 | 主文本翻译链路较成熟，队列和 Full 图片链路仍有高风险热点 | UI 线程边界和流式合并已优化；队列锁内 I/O、活动任务上限失效、全量检查点、轮询调度和 Manga stderr 管道仍会造成停顿或卡死 |
| 用户体验 | 基础工作流完整，但仍有违反用户选择和状态机失配的问题 | 新手引导、保存保护、取消入口和错误详情已有基础；关闭取消、翻译中切会话、导入控件恢复、不可重试错误重试等问题会破坏用户信任 |

在以下四项关闭前，不建议将当前快照标记为稳定发布版本：

1. 配置保存失败后选择“取消退出”，应用窗口保留但核心资源已经被关闭。
2. 翻译运行中仍可导入新会话，旧终态被丢弃后 `is_translating` 可能永久卡住，旧请求仍继续消耗额度。
3. API 密钥保存成功但配置 JSON 写入失败时，UI 和关闭流程可能把失败当成成功。
4. Manga worker 的 stderr 读取达到 500 行后停止 drain，子进程可能因管道写满而阻塞到 30 分钟静默超时。

## 2. 审查范围与验证结果

### 2.1 审查范围

- 第一方 Python：`main.py`、`build.py`、`src/**`、`tests/**`、`tools/**`。
- 工程配置：`pyproject.toml`、`requirements*.txt`、PyInstaller spec、GitHub Actions、README。
- 用户链路：启动、配置、导入、翻译、流式预览、查漏、停止、队列、保存、导出、关闭和恢复。
- Full Edition：只检查本项目与 Manga worker 的进程、依赖和 I/O 边界，不评价 `third_party` 上游代码风格。

### 2.2 静态基线

- `src` 下约 96 个 Python 文件，测试目录约 67 个测试模块。
- 最大模块包括：
  - `src/core/queue_scheduler.py`：约 1900 行。
  - `src/ui/main_window.py`：约 1780 行。
  - `src/ui/settings_window.py`：约 1700 行。
  - `src/ui/translation_controller.py`：约 1400 行。
- 第一方源码约有 248 处宽泛 `except Exception/BaseException` 或裸 `except`，以及约 105 个独立 `pass`。很多位于 UI、线程和清理边界，不能机械删除，但最高风险模块尚未被类型门禁覆盖，后续维护成本较高。

### 2.3 动态验证

- 当前解释器：CPython 3.10.9。
- `import main` 五次约 238-248ms，说明导入基线尚可，但首帧前仍可继续延后 `bootstrap` 导入。
- 重点性能/UI 测试：87 passed。
- 配置、关闭分支、设置布局和新手引导测试：103 passed。
- 运行时契约/SBOM 小集合：7 passed。
- 全量收集约 1035 项，存在 3 个收集错误：
  - `tests/test_queue_provider_limiter.py` 使用 Python 3.11 才提供的 `datetime.UTC`，这是确定的 Python 3.10 兼容缺陷。
  - 另外两个错误来自当前机器缺少 `pydantic`，属于本地环境未按项目依赖重建。
- 当前环境还安装了与项目约束不一致的 `EbookLib 0.20`、`httpx 0.24.1`，且 `pip check` 不通过。因此本轮不能宣称全量测试绿色，正式验证必须使用干净虚拟环境和锁定依赖。
- 本机未安装 Ruff 和 Pyright，未复跑本地静态门禁；CI 配置存在，但当前 Python 3.10 测试收集问题会直接阻断质量任务。
- 队列测试在当前 Windows 受控环境中暴露出两个问题：测试硬编码 POSIX `/tmp`，写入失败后检查点重试又在全局调度锁内等待，导致后续任务在 8 秒内未继续派发。这同时证明了平台测试缺口和生产环境 I/O 故障时的全队列停顿风险。

## 3. 已符合最佳实践的部分

以下设计应保留，不建议在优化时回退：

1. `src/bootstrap.py` 已承担组合根职责，路径、配置、密钥和 edition 能力集中组装。
2. `domain/application/infrastructure/ui` 的依赖方向基本清晰，核心业务对象大量使用不可变 dataclass、Protocol 和结构化结果。
3. `AppPaths` 分离资源、用户配置、工作区和日志，并支持测试注入。
4. 配置、项目和映射写入使用同目录临时文件、`fsync`、`os.replace` 和 Windows 有限重试。
5. API Key 通过 keyring 管理；远程自定义 API 强制 HTTPS；图片下载已有 SSRF、重定向和路径边界限制。
6. 主翻译使用持久 `httpx.Client`、连接池、分项超时、HTTP/2、有限重试和 Provider 级限流。
7. 翻译工作线程不直接操作 Tk；流式事件通过 mailbox 回主线程并按批次合并。
8. 主表、详情表和术语表已有分块加载、缓存和增量更新，大列表体验明显优于直接全量写 Treeview。
9. `TranslationDocument` 已成为业务状态来源，人工编辑保护和流式预览提交语义基本正确。
10. 自动保存具备 debounce、generation、single-flight、后台 I/O 和失败状态。
11. TXT/EPUB 主窗口导入和 EPUB 导出已经后台化，并有迟到回调隔离。
12. CI 已包含 Ruff、格式化、Pyright、pytest、wheel smoke、SBOM、产物校验和 Windows/macOS 构建。

## 4. 优先级定义

| 优先级 | 定义 |
| --- | --- |
| P0 | 违反用户明确选择、导致核心状态永久失配、可能持续计费、稳定卡死或把失败当成功；发布前必须关闭 |
| P1 | 主要性能、可靠性、安全边界或高频体验明显受损；应作为下一阶段主线 |
| P2 | 平台一致性、长期维护、低频性能和产品完整性问题 |

## 5. P0：发布阻断项

### P0-1 关闭取消发生在不可逆 teardown 之后

**证据**

- `main.py:118-128` 先调用 `MainWindow.close()`。
- `main.py:130-133` 之后才保存配置，并允许用户取消关闭。
- `src/ui/main_window.py:1681-1780` 已标记 `_closed`，停止翻译、关闭事件泵、队列、API、图片 Provider、导入器和 autosave。

**影响**

配置保存失败后用户选择“取消，返回应用”，`root.destroy()` 虽然没有执行，但窗口已经失去主要服务。界面仍存在，却无法可靠继续翻译、导入或保存。

**推荐实现**

采用两阶段关闭：

1. `prepare_close()`：阻止新命令、完成文档 Save/Discard 决策、保存配置，但不关闭服务。
2. 用户仍可取消时，只能停留在此阶段。
3. `commit_close()`：一旦进入不可逆资源释放，不再提供“返回应用”；只允许“重试清理/强制退出”。
4. 用 `CloseDecision` 枚举替代裸字符串，并让关闭结果显式返回成功、取消或强制退出。

**必须新增测试**

- 配置保存失败 + Cancel：`MainWindow.close()` 调用次数为 0，应用仍可启动一次翻译命令。
- 配置保存失败 + Retry 成功：只执行一次 teardown。
- teardown 某阶段失败：仍按总 deadline 继续清理，不重新进入可交互状态。

### P0-2 翻译中切换会话会留下永久 busy 状态和继续计费的旧请求

**证据**

- `src/ui/translation_controller.py:220-224` 只禁用翻译/继续按钮，没有禁用导入入口。
- `src/ui/file_importer.py:199-233` 导入前没有 active-run guard。
- `src/ui/main_window.py:590-593` 新文档加载时仅调用 `invalidate_session()`。
- `src/ui/translation_controller.py:174-187` 只使 `run_id` 失效，不停止 worker，也不重置 `is_translating`。
- `src/ui/translation_controller.py:694-696` 旧终态随后被丢弃；`814-819` 又持续拒绝新任务。

**影响**

新文件看似已打开，但应用可能永久显示“翻译任务正在运行”，旧 API 请求继续执行并可能产生费用，旧任务结果又不会提交到任何会话。

**推荐实现**

- 所有会话替换命令先进入统一 guard：`IDLE -> CANCELLING -> IDLE -> REPLACING`。
- 运行中导入时提供“停止当前翻译并切换 / 取消导入”。
- 停止必须等待结构化终态或达到短超时；无论终态是否迟到，都由主线程 reducer 原子清理 `is_translating`、按钮、run_id、preview 和 pending result。
- 不允许单独调用 `invalidate_session()` 来代替取消任务。

**必须新增测试**

- 全文、选中行、查漏三种模式运行中导入。
- 用户取消切换、确认停止后切换、停止超时三条路径。
- 旧 worker 最终返回时不污染新文档，且 pending result 不泄漏。

### P0-3 API 配置文件写入失败被当成成功

**证据**

- `src/domain/secret.py:70-72` 的 `SecretSaveResult.failed` 只检查密钥状态，不检查 `config_saved`。
- `src/domain/secret.py:85-92` 的 `__bool__` 又正确检查了 `config_saved`，两套成功语义不一致。
- `src/ui/settings_window.py:1547-1556` 和 `1614-1631` 使用 `.failed` 分支。
- `src/domain/secret.py:116-122` 的 `ConfigSaveResult.failed` 也只读取 `api.failed`。

**影响**

密钥成功进入 keyring，但 provider、模型或 endpoint JSON 写入失败时，设置窗口仍可能提示成功并关闭；重启后配置回退。退出流程也可能不阻断，造成“本次可用、下次丢失”的隐蔽故障。

**推荐实现**

- 定义唯一成功契约：`succeeded = secret_status != FAILED and config_saved`。
- `.failed` 必须等价于 `not succeeded`；`ConfigSaveResult.failed` 使用 `not bool(api)` 或 `api.failed` 的修正语义。
- 火山密钥、文本 API 和 app config 先完整校验，再事务式提交；不能回滚时，必须逐项报告“已保存/未保存”。
- 删除密钥时只有“确实不存在”可视为成功，keyring 后端异常必须返回失败并回读验证。

### P0-4 Manga worker stderr 停止读取可导致子进程管道死锁

**证据**

- `src/infrastructure/image_translation/manga_worker_client.py:271-298` 使用 `stderr=subprocess.PIPE`。
- `src/infrastructure/image_translation/manga_worker.py:140-143` 将第三方 stdout 重定向到 stderr。
- `src/infrastructure/image_translation/manga_worker_client.py:424-450` 读取 500 行后直接 `break`。
- 父进程的静默 deadline 可达 1800 秒，见 `manga_worker_client.py:192-205`。

**影响**

第三方继续输出时，Windows pipe 写满会阻塞子进程，图片翻译看似“随机卡死”，直到最长 30 分钟后才被静默超时终止。

**推荐实现**

- 达到日志上限后只停止记录，不能停止 drain；继续逐行读取并丢弃。
- 使用固定大小环形缓冲保留最后 N 行诊断，而不是限制读取总行数。
- 增加 worker heartbeat 和阶段 deadline，区分“仍有日志但无协议进度”与“进程完全静默”。

**必须新增测试**

- fake worker 连续输出 1 万行 stderr 后仍能返回 JSON response。
- 日志实际写入仍受行数/字节数限制。
- 超时后 pipe、stdin/stdout/stderr 和进程句柄全部释放。

## 6. P1：应用性能优化

### PERF-1 禁止在队列全局锁内做 I/O、等待和资源关闭

**证据**

- `src/core/queue_scheduler.py:1355` 持有 `self._lock` 应用结果。
- 暂停确认在 `1446` 调用最长 3 秒的 `flush_blocking()`。
- 任务完成在 `1468` 调用最长 5 秒的 `flush_blocking()`。
- 删除任务在 `1266-1281` 的锁内执行 flush、checkpoint close 和 engine close。
- 终态清理在 `1506-1518` 的锁内关闭引擎；SiliconFlow close 会 join 心跳线程。

**影响**

一个慢磁盘、只读目录、网络盘或 keyring/HTTP 关闭异常，会同时阻塞命令、派发、完成结果应用和 UI 快照。多任务结束时停顿会串行累加。

**目标结构**

```text
RUNNING/PARTIAL
      |
      | 锁内：摘取快照、记录 generation、切换状态
      v
FINALIZING
      |
      | 锁外：flush / close / repository save
      v
COMPLETED / PARTIAL / ERROR
```

- 锁内只做内存状态转换和引用 detach。
- finalizer 使用独立 executor/future；完成后通过 coordinator event 回填终态。
- 所有关闭操作使用一个绝对总 deadline，不能对每个任务重新给 3-5 秒。
- 建立测试断言：持锁区不得调用 `flush_blocking`、文件 I/O、`join`、HTTP client close。

### PERF-2 `queue_max_active_tasks` 设置目前没有生效

**证据**

- `QueuePolicy.max_active_tasks` 仅定义于 `src/core/queue_scheduler.py:130`。
- `_cmd_start_all()` 在 `1049-1062` 对全部任务调用 prepare。
- 每个 prepare 都会创建引擎、HTTP client、心跳线程并规划全部批次；仓库中没有用 `max_active_tasks` 限制激活数量的逻辑。

**影响**

用户界面提供了一个实际无效的性能控制。大队列会创建 O(总任务数) 的连接池、心跳线程和 BatchJob，增加首请求延迟和内存占用。

**推荐实现**

- 区分 `PENDING` 与 `ACTIVE`；只允许最多 `max_active_tasks` 个任务 prepare。
- 活跃任务进入终态/暂停后，按公平顺序惰性激活下一个。
- Provider runtime 可进一步按运行时 key 共享连接与心跳，而任务只持逻辑会话。
- 增加 100 个任务、`max_active_tasks=4` 的测试：引擎实例、心跳线程和已规划批次数量均不得超过活动窗口。

### PERF-3 检查点 debounce 减少了写盘次数，但没有减少每批全量复制

**证据**

- 每批结果在 `queue_scheduler.py:1432-1433` 调度检查点。
- `1619-1644` 在全局锁内复制整篇 target、failed、manual、completed。
- `1654-1674` 构造保存函数时再次复制。
- TXT 保存又全量 join 并写译文和项目 JSON；EPUB 保存全量更新映射。

**影响**

大文档、小批次和多任务组合下产生明显内存抖动、锁竞争和 SSD 写放大。API 越快、缓存命中越多，应用自身开销越突出。

**推荐实现**

- 每批只记录 `generation + changed_indices`，不立即构造全量保存闭包。
- debounce 真正到期时，才在短持锁区抓取一次版本化快照。
- 中期使用 journal/SQLite 持久化增量；兼容 `_译文.txt` 只在显式保存、暂停、退出或终态生成。
- 项目恢复仓储应避免每次保存重复持久化原文全量。

### PERF-4 调度器需要事件驱动唤醒，不能固定 100ms 轮询 Future

**证据**

- 主循环在 `queue_scheduler.py:954-982` 每轮等待。
- `_wait_for_events()` 在 `984-999` 先等待命令事件 100ms，再用零超时检查 Future。

**影响**

Future 完成不会唤醒调度器，下一波派发额外等待 0-100ms。远程慢 API 时不明显，但缓存、本地模型、Mock 或低延迟 endpoint 会被人为限制吞吐。

**推荐实现**

- Future `add_done_callback()` 只设置统一 wake event。
- command、future、limiter cooldown 使用同一个 `Condition/Event`。
- 无事件时指数退让，活跃时立即调度；快批次 dispatch gap 的 p95 目标小于 20ms。

### PERF-5 主翻译进度聚合应从按批次扫描改为增量计数

`src/core/translator.py:433-464` 和 `583-591` 在高频进度事件中重复扫描或求和所有批次，批次数增加后接近 O(B²)。维护 `completed_lines` 标量、每批已计入值和 `next_unfinished_batch` 指针，可将热点降为 O(1) 或 O(log B)。

### PERF-6 队列批量导入必须后台化

- `src/ui/concurrent_window.py:413-431` 在 Tk 主线程逐个调用 `manager.add_task()`。
- `src/core/concurrent_manager.py:203-233` 同步读取 TXT/解析 EPUB。
- `queue_scheduler.py:721-779` 还会同步计算 SHA-256 和读取恢复项目。

建议使用 1-2 个 import worker，提供逐文件进度、取消和失败列表；解析成功后再在 coordinator 中原子注册。多个大 EPUB 不应冻结队列窗口。

### PERF-7 UI 事件泵需要统一预算和空闲退让

- `TranslationEventMailbox` 只合并 STREAM，`BATCH_COMPLETED` 进入无界 terminal deque。
- `TkTranslationEventPump` 每轮一次性渲染全部 terminal。
- 主窗口常驻 translation、file importer、image handler 三个 50ms pump，设置和队列窗口还会增加 pump。

建议：

- 每帧使用数量或耗时预算，例如最多 64 个事件或 8ms，余量留到下一帧。
- 相邻 batch completed 可合并为连续 row-update block，但 RUN 终态必须在之前的 batch commit 后处理。
- 多个 mailbox 复用一个 root 级 pump；空闲时将轮询间隔提高到 250-500ms，有事件时恢复 16-50ms。

### PERF-8 手动保存和对照文件导出也应避免大文件同步阻塞

`src/ui/translation_controller.py:1252-1321` 和 `1323-1348` 会在 Tk 线程构造整篇字符串并同步写盘。EPUB 导出已后台化，TXT/对照导出应复用相同 Job + mailbox 模式，并提供进度、取消和原子发布。

## 7. P1：用户体验优化

### UX-1 导入 busy 状态恢复会覆盖真实控件状态

- `src/ui/file_importer.py:488-489` 创建进度对话框后递归禁用 root 子控件，进度窗本身也是子项，取消按钮可能被禁用。
- `579-610` 结束后无条件把所有 Button 设为 normal、所有 Combobox 设为 readonly。

这会错误启用停止按钮、非 EPUB 导出、Text Edition Manga 入口或其他本应禁用的命令。

建议建立单一 `AppUiState`/command policy：控件状态由 `session_kind + api_ready + run_state + import_state + edition + image_busy` 推导。后台操作只修改状态，不直接递归改 widget。短期至少保存并精确恢复原状态、排除进度对话框，并在结束后调用统一 `refresh_action_state()`。

### UX-2 EPUB 取消需要下传到真正的解析循环

`file_importer.py:506-521` 只在整个 `_build_epub_import_result()` 前后检查取消；`src/core/epub_processor.py:129` 的 `import_epub()` 无 cancel/progress 参数。

建议：

- token 下传到 ZIP 检查、spine 遍历、DOM 解析、图片提取、映射写入等循环。
- 每处理一章/若干图片检查一次，不需要每个节点检查。
- 取消是独立 `CANCELLED` 结果，不得在 `file_importer.py:623-625` 显示为“导入错误”。
- 取消按钮点击后 100ms 内更新状态；在下一个安全检查点退出。

### UX-3 自动查漏必须尊重错误分类和可重试性

- 全文 PARTIAL 在 `translation_controller.py:867-881` 无条件启动查漏。
- 查漏 FAILED 在 `1241-1246` 无条件 1 秒后重试，最多两轮。
- 队列已有 `application/error_handling.py` 的 category/retryable，但主翻译结果未复用。

只对网络瞬断、429、暂时性 5xx 做带 jitter 的退避；401/403、余额不足、模型不存在、配置无效和本地权限错误应立即停止并展示动作按钮或明确下一步。

### UX-4 运行终态必须清理整个 run 的流式状态

`src/ui/translation_event_mailbox.py:43-56` 的 RUN 终态只按 `(run_id, batch_start)` 清理一个批次。多批次 worker 异常时，其他批次的 STREAM 仍会在 RUN_FAILED 后被 pump 渲染，形成失败后重新出现的幽灵预览。

RUN_COMPLETED/RUN_FAILED/RUN_CANCELLED 应清除该 `run_id` 的全部 stream 状态；controller 在处理终态后应立刻使 run 失效并清理 `_pending_results`。新增多批次失败/取消测试。

### UX-5 队列窗口关闭后需要持续可见的后台状态

当前注入 manager 的队列窗口关闭后任务继续运行，但没有托盘或其他持续状态。用户可能误以为任务已停止，产生意外 API 消耗。

可选方案：

1. 关闭窗口时询问“继续后台运行 / 取消任务 / 返回”。
2. 若继续后台运行，主窗口显示持续的队列状态和“重新打开队列”入口。
3. 仓库中的 `install_tray_dependencies.bat` 声称存在托盘功能但实际没有对应实现和测试，应删除过时脚本，或完整实现托盘生命周期，不能保留半成品承诺。

### UX-6 主编辑器应复用队列的可操作错误体系

主翻译仍直接显示原始 `error_message`，而队列已经支持安全文案、建议动作和 correlation ID。两条链路应共用 `ActionableError`，并保持日志详情与 UI 摘要分离。

### UX-7 修复默认配置迁移和 README 契约漂移

- `config/app_config.json:6` 仍包含冗长、带“突破限制/无视约束”措辞的旧提示词。
- 开发环境首次迁移会复制资源目录 config，见 `src/app_paths.py:250-254`。
- `ConfigManager.load_app_config()` 浅合并后会把缺失 schema 的旧 prompt 与新的 schema version 混合，无法识别为待迁移默认值。
- `README.md:62-74` 的 API 配置示例仍是旧的嵌套 provider schema，与当前扁平 `provider/model_name/base_url` 契约不一致。

建议只在旧 prompt 与已知历史默认值完全相等时迁移到新短提示词，绝不覆盖用户自定义 prompt；删除打包 spec 中无用途的真实 `app_config.json/glossary.json`，只保留 sample；README 示例由配置 schema 测试生成或校验。

### UX-8 平台键盘与滚轮交互需与支持声明一致

README 声明 Windows/macOS/Linux，但主快捷键只绑定 Control，设置滚轮主要处理 `<MouseWheel>`。应补充 macOS Command、Linux X11 Button-4/5，并在三平台关键 UI 测试中验证。

## 8. P1/P2：Python 工程与安全治理

### ENG-1 修复 Python 3.10 质量矩阵

`pyproject.toml:10` 和 CI 都声明支持 3.10，但 `tests/test_queue_provider_limiter.py:14` 导入 `datetime.UTC`。改用 `datetime.timezone.utc`，并要求 3.10 收集和测试全绿后再保留该支持声明。

### ENG-2 生成可复现的分平台/edition 依赖锁

- `requirements-image-manga.txt:18-25` 已明确承认尚无 hash lock。
- `118-119` 从额外索引安装无版本约束的 Rust wheel。
- 发布 CI 直接解析浮动依赖。

建议：

- `pyproject.toml` 作为抽象依赖唯一来源。
- 生成 Text/Full、Windows/macOS、Python 版本对应的带 hash lock。
- 发布使用 `--require-hashes`；自定义 wheel 使用固定版本、直接 URL 和 SHA-256。
- CI 不要先安装 `.[dev]` 再重复安装 `requirements.txt`，避免双源解析漂移。

### ENG-3 异常 traceback 也必须脱敏

`src/utils/logger.py:27-36` 只处理 `record.getMessage()`，Formatter 后续追加的 traceback 不会经过 sanitizer。应提供自定义 Formatter 覆盖 `formatException()`，清理 `exc_text`，并让 manifest/provider 统一复用中央 `sanitize_for_log()`，避免复制多套不一致正则。

### ENG-4 外部 EPUB 和远程 SVG 增加资源/主动内容边界

- EPUB 导入前检查 ZIP 成员数、单成员解压大小、总解压大小、压缩比和路径穿越，防止压缩炸弹与内存耗尽。
- 远程 SVG 不应仅凭 XML 前缀视为可信图片。优先安全栅格化；若保留 SVG，必须移除脚本、事件属性、`foreignObject` 和外部引用。

### ENG-5 损坏恢复文件不得静默当作不存在并覆盖

`ProjectRepository.load()` 应区分 `NotFound` 与 `Corrupt`。损坏项目先重命名隔离并提供恢复提示，再决定是否新建，不能直接返回 None 后覆盖同 ID 文件。

### ENG-6 扩大类型和平台门禁

- `pyproject.toml:174` 不包含 `src/core`、`src/api`、`src/config`。
- CI `build.yml:54` 又显式只运行 domain/application。
- 测试只在 Ubuntu 执行；Windows/macOS 仅做进程存活 smoke。

建议分步执行：

1. CI 直接运行无路径参数的 `python -m pyright`。
2. 将 core/api/config 纳入 basic，优先覆盖 queue scheduler、API lifecycle、secret/result 和 file importer。
3. Python 3.11 在 Windows、macOS、Ubuntu 跑关键路径测试；Full 产物增加 Manga worker health check。
4. 覆盖率先建立当前基线和“不下降”门禁，再逐步设置 `--cov-fail-under`。

### ENG-7 拆分上帝对象，但不需要更换 GUI 框架

优先按职责拆分：

- `queue_scheduler.py`：registry、state machine、dispatcher、finalizer、snapshot publisher。
- `main_window.py`：layout、command state、document workflow、save/close coordinator。
- `settings_window.py`：provider form、image settings、queue settings、persistence transaction。
- `translation_controller.py`：run state machine、event reducer、commands、export jobs。

拆分目标不是缩短文件本身，而是让状态转换、I/O 和渲染边界可以独立测试。现阶段没有证据要求从 Tkinter 迁移到其他 GUI 框架。

### ENG-8 包布局作为 2.0 迁移项

当前真正的顶层包名是通用的 `src`，`setuptools` 也发布 `src*`。长期建议改为标准 src-layout：`src/ai_translater/...`，增加 `[project.scripts]` 或 GUI entry point，并用 wheel 安装测试保证仓库路径不会掩盖安装包。该项不应与当前 P0/P1 修复混在同一发布中。

## 9. 推荐实施顺序

### 阶段 0：发布阻断修复

1. 两阶段关闭，配置取消不得发生在 teardown 后。
2. 会话替换与翻译 run 使用统一状态机。
3. 统一 `SecretSaveResult` 成功语义并修复删除密钥结果。
4. Manga stderr 始终 drain。
5. 为以上四项补回归测试。

### 阶段 1：队列性能和可靠性

1. 引入 `FINALIZING`，所有 flush/close 移出全局锁。
2. 真正执行 `max_active_tasks`，惰性创建引擎和批次计划。
3. checkpoint 延迟取快照并减少全量写放大。
4. Future 完成事件驱动调度。
5. 队列文件导入后台化。

### 阶段 2：取消、错误和 UI 状态

1. 用集中 command policy 替代递归启用/禁用控件。
2. EPUB 协作取消和独立 CANCELLED 终态。
3. 主翻译接入 ActionableError，查漏按 retryable 决策。
4. RUN 终态清理全部 stream/pending 状态。
5. 队列后台运行保持持续可见。

### 阶段 3：发布治理

1. 修复 Python 3.10 测试并建立 Text/Full 明确支持矩阵。
2. 生成分平台 hash lock，固定额外索引包。
3. 扩大 Pyright 和三平台关键测试。
4. 修复 traceback/manifest 脱敏、EPUB 资源上限和 SVG 主动内容。
5. 修正 README、默认 prompt 和打包资源。

### 阶段 4：长期结构治理

1. 拆分四个超大协调模块。
2. 迁移标准包布局和 GUI entry point。
3. 评估持久化翻译缓存和 SQLite/Journal 项目存储。

## 10. 性能与体验验收指标

| 场景 | 建议指标 |
| --- | --- |
| Tk 主线程 | 常规命令不执行磁盘/网络 I/O；单次事件处理预算不超过 8-16ms |
| 队列锁 | p99 持锁时间小于 5ms；持锁调用图中不存在 flush、join、文件写或 HTTP close |
| 调度唤醒 | 快批次完成到下一批派发的 p95 小于 20ms |
| 活动任务 | `max_active_tasks=4` 时引擎、心跳和已 prepare 任务不超过 4 |
| 检查点 | 连续批次在 debounce 窗口内只构造一次全量快照；终态必须完成最后一次保存 |
| UI 邮箱 | 每帧按数量/时间有界；1 万个快速事件不会产生超过 50ms 的单帧停顿 |
| EPUB 取消 | 点击后 100ms 内显示取消中；在下一个章节/图片安全点退出；不显示错误弹窗 |
| Manga worker | 超过 1 万行 stderr 仍可完成协议响应；日志存储有界但 pipe 持续 drain |
| 关闭 | 用户仍可取消时没有资源被永久关闭；不可逆关闭使用一个全局 deadline |
| 发布 | 干净环境 `pip check` 通过，Text 支持矩阵零收集错误，Full worker health check 通过 |

## 11. 建议新增的测试文件/场景

- `tests/test_close_transaction.py`
  - 配置失败取消后服务仍可用。
  - commit close 幂等且只执行一次。
- `tests/test_session_switch_while_running.py`
  - 三种翻译模式运行中切换会话。
  - 旧终态、旧 stream、pending result 全部清理。
- `tests/test_secret_result_semantics.py`
  - `config_saved=False` 时 `.failed is True`、`bool(result) is False`。
  - keyring 删除异常不假装成功。
- `tests/test_manga_worker_stderr_drain.py`
  - 大量 stderr 不阻塞 response。
- `tests/test_queue_finalizer.py`
  - 慢/失败 checkpoint 不阻塞其他任务派发和命令。
  - finalizer 使用总 deadline。
- `tests/test_queue_active_window.py`
  - 100 个任务只激活配置数量。
- `tests/test_queue_import_background.py`
  - 大 EPUB 导入期间 Tk 心跳继续运行，可取消。
- `tests/test_epub_resource_limits.py`
  - 成员数、解压大小、压缩比和路径穿越。
- `tests/test_translation_terminal_cleanup.py`
  - 多批次 RUN_FAILED/RUN_CANCELLED 清除整个 run 的 stream。
- `tests/test_cross_platform_shortcuts.py`
  - Windows/Linux/macOS 快捷键映射。

## 12. 推荐验证命令

在全新虚拟环境执行，不复用当前机器的全局 site-packages：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check main.py build.py src tests tools
.\.venv\Scripts\python.exe -m ruff format --check main.py build.py src tests tools
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\python.exe -m pytest -q --cov=src --cov-report=term-missing
```

Full Edition 还需在对应平台锁文件环境中执行：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-image-manga.lock.txt
.\.venv\Scripts\python.exe -m pytest -q tests/test_image_translation_runtime.py tests/test_manga_worker_stderr_drain.py
```

## 13. 不建议采用的做法

- 不要在修复 P0/P1 时同时更换 GUI 框架或重写全项目。
- 不要通过提高线程数掩盖锁内 I/O；这只会放大连接、内存和写盘压力。
- 不要把所有 `except Exception` 机械替换，先按“数据持久化、线程生命周期、网络边界、用户命令”收窄最高风险路径。
- 不要让 UI 控件各自维护状态；应由单一应用状态推导 command availability。
- 不要把浮动 `requirements.txt` 当发布锁文件。
- 不要在取消路径显示通用错误，也不要在资源已经关闭后提供“返回应用”。

## 14. 总完成定义

完成本指导的核心目标，不是代码行数减少，而是满足以下条件：

1. 用户选择、内存状态、磁盘状态和后台任务状态一致。
2. Tk 主线程不承担不可控 I/O，队列全局锁不包围阻塞操作。
3. 配置、密钥、项目和检查点不会把部分失败伪装成成功。
4. 取消、失败和切换会话都有单一结构化终态，旧事件无法污染新状态。
5. `max_active_tasks`、并发、重试和超时等设置真实影响运行时行为。
6. Text/Full、Windows/macOS/Linux 和 Python 版本支持矩阵有可重复的依赖与测试证据。

