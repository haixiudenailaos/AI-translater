# Python 最佳实践与性能优化审查指导

审查日期：2026-07-20（Asia/Shanghai）
审查基线：Git 提交 `f8d8128`（全量静态/测试结果）及 2026-07-20 工作区增量修复（定向回归）
审查对象：`D:\米哈游项目\AI翻译V1.4\AI-translater-1.4`
文档性质：当前代码基线审查、发布门禁、性能优化顺序与验收标准
历史基线：`docs/PYTHON_PERFORMANCE_UX_CURRENT_AUDIT_AND_IMPLEMENTATION_PLAN.md` 保留为历史记录，不代表当前待办

## 1. 执行结论

**总体判断：项目基本符合现代 Python 桌面应用的工程实践，但尚未形成发布级最佳实践闭环。**

项目已经具备清晰的组合根、领域与应用分层、Protocol/dataclass、原子写入、keyring、日志脱敏、后台任务、事件邮箱、检查点、资源边界和 1100 余项测试。当前主要问题不是“代码完全不可维护”，而是已有能力没有被 CI、类型门禁和发布契约持续证明，同时仍有少数并发正确性问题和大规模数据场景下的性能热点。

| 维度 | 当前判断 | 主要依据 |
| --- | --- | --- |
| 架构与 Python 用法 | 基本符合 | 有组合根、依赖注入、结构化结果、线程边界和持久化抽象 |
| 代码质量门禁 | 基础门禁已恢复 | CI 已有 Python 3.10-3.13 的 Ruff、格式、pytest、coverage 与 wheel smoke；配置、项目仓库和图片请求边界已有阻断型 Pyright，余下全仓报告仍为迁移依据 |
| 测试 | 基础较强，主套件已验证 | 当前隔离运行 1138 passed、39 skipped；3 个 Windows `.png` ACL 用例需在正常 Windows runner 复验 |
| 依赖与打包 | Text 契约已对齐，锁仍未完整 | Windows Text 构建改用 Python 3.10 匹配 hash lock；macOS/Full 仍缺匹配 lock |
| 并发正确性 | 主要内存、仓库与图片关闭事务已收敛 | 配置使用版本化密钥引用，项目/recent index 有进程内补偿事务；图片关闭使用共享 deadline；跨进程恢复仍需修复 |
| 性能 | 中小文档可用，大规模场景仍有明确热点 | Tk 全量 drain、全文复制、检查点写放大、EPUB 多次全包处理 |
| 安全与数据边界 | 基础良好但有缺口 | 项目 ID 路径、图片 endpoint 与 EPUB 缺章中止已校验；多文件代际恢复仍不完整 |

本轮没有发现需要直接定为 P0 的、可稳定复现的任意代码执行或不可恢复数据破坏问题。以下事项仍应在“稳定发布”前关闭：

1. 将 Pyright 从报告升级为覆盖关键模块的阻断门禁。
2. 生成 macOS/Full Edition 与 runner Python 完全匹配的 hash lock，并明确 Full 发布渠道。
3. 为 EPUB、自动保存与检查点实现可崩溃恢复的 generation/commit manifest。

## 2. 审查范围与证据

### 2.1 审查范围

- 第一方代码：`main.py`、`build.py`、`src/**`、`tests/**`、`tools/**`。
- 工程配置：`pyproject.toml`、`requirements*.txt`、PyInstaller spec、GitHub Actions 和 README。
- 关键链路：启动、配置、导入、翻译、流式预览、队列、检查点、图片翻译、EPUB 导出和关闭。
- Full Edition：只审查本项目对 Manga worker 的进程、I/O 和生命周期边界，不评价 `third_party` 上游代码风格。

静态规模约为：

- `src` 下 95 个 Python 文件、25,570 行。
- `tests` 下约 19,229 行，pytest 收集 1148 个测试项。
- 宽泛 `except Exception/BaseException` 或裸捕获约 268 处。
- 最大模块包括 `queue_scheduler.py` 2118 行、`main_window.py` 1839 行、`settings_window.py` 1750 行、`translation_controller.py` 1693 行。

这些数字用于判断风险集中位置，不应机械地转化为“文件行数”或“异常捕获数量”KPI。

### 2.2 动态验证结果

本轮在临时隔离的 CPython 3.13.5 环境执行了静态检查和测试。结果以提交 `f8d8128` 为准：

| 检查 | 结果 | 结论 |
| --- | --- | --- |
| Ruff lint | 通过 | 当前启用的 E/F/I/UP/B/SIM 规则无违规 |
| Ruff format | 176 个文件均已格式化 | 格式基线可直接进入 CI |
| Pyright | 84 个文件，244 errors / 1 warning | 类型配置存在，但尚不能作为绿色门禁 |
| pytest 收集 | 1148 项 | 测试规模和可收集性良好 |
| pytest 完整运行 | 5 failed, 1106 passed, 37 skipped, 40 warnings，64.19 秒 | 1 项为沙箱环境失败，4 项为真实契约漂移 |
| CPython 编译检查 | 通过 | 第一方 Python 文件无语法级编译错误 |
| `import main` 五次 | 493-572ms，中位数 537ms | 可作为同机冷启动优化基线 |

后续实施验证：R0 契约回归 38 项通过；配置事务 68 项通过；项目仓库事务 49 项通过；图片 endpoint/取消/信任边界 89 项通过；图片关闭 deadline 回归 31 项通过；EPUB 缺章中止与队列取消回归 37 项通过。使用锁定的 Ruff 0.6.9 执行全仓 lint 通过。全仓格式检查当前仅被工作区已有的 `src/core/image_translator.py` 和 `tests/test_image_fixes.py` 格式差异阻断。最近一次完整 suite 基线（排除 3 个受控 Windows 环境中稳定复现的 `.png` 文件访问拒绝用例）为 1138 passed、39 skipped。

Pytest 的 5 项失败应这样解释：

- 1 项因受控环境无权访问真实 `%APPDATA%`，属于运行环境失败，不作为产品回归。
- 1 项要求 CI 存在 Python 3.10-3.13 测试矩阵，但当前工作流不存在该矩阵。
- 2 项仍要求 Text spec 使用 onedir，而 `translator_text.spec` 已改为 onefile。
- 1 项 Manga hook 测试要求相对源路径，而实现返回绝对路径。

`-X importtime` 显示 `src.bootstrap` 累计约 314ms、`main` 累计约 502ms。累计导入时间不能简单相加，但足以说明组合根和其传递依赖是启动优化的首要观察点。该结果依赖硬件和磁盘状态，只用于同机前后对比。

### 2.3 静态检查解释

Ruff 通过不等于“已全面符合 Python 最佳实践”。当前 Ruff 配置主动忽略了部分历史规则，Pyright 也关闭了多项 unknown/missing 诊断。正确做法是保留现有绿色 lint 基线，并逐步收紧高风险模块，而不是一次性开启所有规则后产生大量无行动价值的告警。

## 3. 已符合最佳实践的部分

以下能力已经存在，优化时应保留：

1. [`src/bootstrap.py`](../src/bootstrap.py) 承担组合根职责，集中组装路径、配置、能力和主要服务。
2. `domain/application/infrastructure/ui` 的依赖方向基本清晰，核心对象大量使用 dataclass、枚举、Protocol 和结构化结果。
3. [`src/app_paths.py`](../src/app_paths.py) 将资源目录、用户数据、配置、日志和测试注入分离。
4. 配置、项目、映射和图片资产使用同目录临时文件、`fsync`、`os.replace` 与有限重试完成原子发布；POSIX 发布后额外同步父目录，目录持久化失败会向调用方传播。
5. API 密钥通过 keyring 保存；日志已有集中脱敏和 traceback 脱敏；远程下载包含 SSRF、重定向和路径边界防护。
6. 文本 API 使用持久客户端、连接池、分项超时、有限重试和 Provider 级限流。
7. Tk 工作线程不直接更新控件，流式翻译和后台结果通过 mailbox/event pump 回到主线程。
8. 队列主列表已经消费轻量不可变快照，`max_active_tasks`、Future 完成唤醒和异步 finalizer 已实现。
9. 检查点已经具备 debounce、single-flight 和终态保存；自动保存具备 generation 与迟到结果隔离。
10. EPUB 导入已有 ZIP 成员数、解压大小、压缩比、路径穿越限制和协作式取消检查点。
11. Manga worker 的 stderr 在达到日志保留上限后仍会持续 drain，不再因管道写满而死锁。
12. 关闭取消、会话替换、保存结果语义、RUN 终态清理、损坏项目隔离等旧问题已有回归测试。

这意味着后续工作应以增量修复和测量为主，不需要更换 GUI 框架或重写整个项目。

## 4. 优先级和实施原则

| 级别 | 含义 |
| --- | --- |
| R0 | 发布门禁或发布契约问题；不一定是运行时 P0，但未关闭前不能证明产物可靠 |
| P1 | 并发正确性、数据边界、资源生命周期或大规模高频性能问题；下一阶段主线 |
| P2 | 长期维护、低频性能、包布局和进一步收紧的工程治理 |

实施时遵守以下原则：

- 先修正确性，再做并发和缓存优化。
- 先建立可重复基线，再宣称性能提升。
- Tk 主线程只承担短小、可预测的状态转换和渲染。
- 锁内只修改内存状态，不执行磁盘、网络、`join` 或资源关闭。
- 所有设置都必须真实影响生产路径；否则隐藏设置或明确标记为实验项。
- 不通过关闭 Pyright 规则、跳过测试或放宽断言制造“绿色”。

## 5. R0：发布门禁

### R0-1 恢复 CI 质量任务

已在 [`.github/workflows/build.yml`](../.github/workflows/build.yml) 增加 `quality` 矩阵：Python 3.10-3.13 运行 Ruff、格式、编译与 pytest，Python 3.11 额外保存 coverage 并输出 Pyright 报告；`wheel-smoke` 会在仓库外安装 wheel 后导入 `src.bootstrap`。README 已同步为 Text-only 构建事实。

建议拆分为：

| Job | 平台/版本 | 必须执行 |
| --- | --- | --- |
| `quality` | Ubuntu，Python 3.10-3.13 | Ruff、format、Pyright、pytest、coverage、`pip check` |
| `platform-smoke` | Windows/macOS，主支持 Python | 路径、keyring fallback、Tk 关键测试和启动 smoke |
| `wheel-smoke` | 干净环境 | 构建 wheel、安装 wheel、从仓库外目录导入/启动 |
| `build-text` | 明确支持的平台 | 使用与 runner Python 完全匹配的 hash lock 构建 |
| `build-full` | 仅明确支持的平台 | Manga worker health check、模型外置策略和产物校验 |
| `release` | tag | 汇总已通过的产物、校验和、SBOM 和发布说明 |

剩余验收：

- 将 `quality` 和 `wheel-smoke` 设为分支保护 required checks。
- Pyright 已阻断 `config_manager`、`project_repository` 与 `image_translator`；后续逐步扩大到 domain/application/queue。
- 删除仓库跟踪的 `test_results*.txt`，以 CI JUnit、coverage 与 benchmark artifact 代替。

### R0-2 统一 Python、依赖锁和产物契约

Windows Text runner 已改为 Python 3.10，以匹配 `requirements-text-win-py310.lock.txt`；`pip-tools` 已进入开发依赖，Text onefile、Full onedir、README 和契约测试也已对齐。macOS 仍直接安装浮动 `requirements.txt`，Full Edition 尚未进入 CI，因此发布锁仍不完整。

同时存在三套互相冲突的事实：

- workflow 只构建 Text onefile；
- README 仍描述 Text/Full onedir zip 与 tag 发布；
- 测试仍要求两个 spec 都是 onedir。

必须先作产品决策，再修改代码和测试：

1. 明确 Text/Full 各自支持的平台、Python 版本、onefile/onedir 形式和发布渠道。
2. 每个发布组合生成对应 lock，文件名包含 edition、平台、架构和 Python minor。
3. CI 生成环境与 lock 的 Python minor 完全一致，并使用 `--require-hashes`。
4. `pyproject.toml` 保留抽象依赖，lock 只负责可复现安装；不得让两者长期手工漂移。
5. README、spec、契约测试和 workflow 在同一个变更中更新。

### R0-3 当前测试红线已修复

以下真实契约漂移已关闭：

- CI 质量矩阵声明 Python 3.10-3.13；
- Text spec 明确 onefile，Full spec 保持 onedir；
- hook data 验证真实存在的绝对源路径和受控目标路径；
- README、spec、工作流与契约测试描述相同产物。

剩余的 3 个 `.png` 文件访问拒绝只在当前受控 Windows pytest 路径复现，且正常文件读写对照并不一致；不得通过弱化资产安全断言制造全绿，应在 CI Windows runner 复验。

### R0-4 建立可持续的类型门禁

CI 现已阻断 `src/config/config_manager.py`、`src/infrastructure/project_repository.py` 与 `src/core/image_translator.py` 的类型回归；全仓报告仍有历史错误，主要集中在：

- `src/application/translator_provider.py`
- `src/application/autosave.py`
- `src/core/epub_processor.py`
- `src/domain/project.py`
- `src/application/image_translation_service.py`

推荐顺序：

1. 先修 Protocol 返回值、`Optional` 缩窄、回调签名和 Future 类型。
2. 清理 `concurrent.futures.Future` 与 `asyncio.Future` 的错误混用。
3. 让 domain/application 核心子集真正零错误，再逐步扩展到 infrastructure/core。
4. UI 边界允许第三方 Tk 类型不完整，但业务对象不得退化为无约束 `Any`。
5. 迁移期间可保留一个非阻断的全仓报告，但阻断子集必须持续扩大且不能回退。

禁止通过继续关闭 unknown/missing 规则或批量添加 `type: ignore` 来伪造完成。

## 6. P1：正确性、安全与生命周期

### P1-1 已实现：配置与密钥版本化快照

[`src/config/config_manager.py`](../src/config/config_manager.py) 现在先保存唯一版本化 secret reference，再原子发布引用它的 JSON，最后发布内存快照。JSON 写失败会删除未发布的引用；旧 JSON 继续指向旧密钥。`update_api_provider_config()`、provider 读取和 API/app/glossary 公共快照也已使用锁与深拷贝。

回归覆盖 JSON 写失败后的重启、并发 provider 切换与嵌套快照隔离。后续应按同一代际模型处理多文件 app/glossary 保存，见 P1-8。

### P1-2 已实现进程内项目/recent index 补偿事务

[`src/infrastructure/project_repository.py`](../src/infrastructure/project_repository.py) 现已限制项目 ID 为 `^[0-9a-f]{16}$`，并通过 `resolve()` 与直接子路径校验保护项目文件、检查点和隔离文件。save/delete 在同一仓库锁内暂存旧文件、更新 recent index；索引写失败会恢复项目/检查点并向调用方暴露失败。

路径穿越、多线程保存和 recent index 故障注入均有回归。进程崩溃与多进程竞争仍需要 journal/文件锁或 SQLite，见 P1-8。

### P1-3 已修复：Future 绑定派发时的 Provider limiter

[`src/core/queue_scheduler.py`](../src/core/queue_scheduler.py) 已使用不可变 `_InFlightBatch` 保存派发时的 limiter，并在提交失败、成功、异常、取消和关闭路径配对释放。同一 Provider 运行时切换后的 in-flight Future 不再依赖当前 UI 的 limiter。

保持回归：两个 Provider 交错派发时，各自 `in_flight <= configured_max`；全部完成/取消后两者均归零；executor 提交失败也不得泄漏槽位。

### P1-4 队列和图片关闭 deadline 已实现

队列调度器已经使用单一绝对 deadline，将等待、检查点关闭和执行器关闭限制在剩余时间内。图片翻译关闭也遵循同一原则：所有 Provider 先接收取消，再等待 worker 的剩余预算，最后才以剩余预算释放资源。不得在后续重构中恢复“每个资源各自等待完整超时”的实现。

保持回归：100 个异常队列任务不会产生 `N x timeout` 关闭时间；所有新资源的 `join`/`flush`/`close` 必须接收剩余时间，而非重新分配完整超时。

### P1-5 已实现：EPUB 导入不能静默丢章节

[`src/core/epub_processor.py`](../src/core/epub_processor.py) 现在将正文节点和章节解析异常转换为 `EpubImportPartialError`，在写入三个映射文件前中止本次导入。不会再把“仍有部分章节”当成成功，也不会覆盖上一次完整 mapping。队列 `add_task()` 已把取消回调传给 `import_epub()`；窗口取消时，已在执行的 EPUB 会在 archive、chapter、image 和 write 安全点退出，并不会被报告为普通导入失败。

回归覆盖第二章节解析失败时三份既有 mapping 字节保持不变，以及队列取消回调向 EPUB 处理器传递；当前相关套件为 37 passed。允许用户接受部分导入的审计清单尚未实现；在此之前，默认中止是唯一允许的语义。跨文件崩溃恢复仍由 P1-8 处理。

### P1-6 已实现：图片翻译关闭和 endpoint 校验统一

图片 UI 现使用单一绝对 deadline：先取消所有 Provider，等待活动 worker 直到剩余时间耗尽，再向 Provider 传递剩余预算释放资源。worker 未退出或从自身线程调用关闭时，代码会保留 Provider、模型和 event loop，不会并发卸载资源。Manga worker 的 terminate/kill 和 reader 线程回收共享该预算；本地 runtime 只有在拥有它的线程退出后才关闭 event loop。火山 endpoint 已复用文本 API 的 HTTPS/loopback 校验，拒绝远程 HTTP、URL 用户信息、查询参数和锚点，并在创建 OpenAI/httpx 客户端前失败。

回归覆盖关闭顺序、worker 超时不释放资源、worker terminate/kill 预算和 runtime 线程仍存活时不关闭 loop；当前定向套件为 31 passed。后续保持中央 `sanitize_for_log()`、关闭中回调和恶意 endpoint 覆盖，避免回退到弱化脱敏或竞态 teardown。

### P1-7 已收敛：recent index 单进程并发更新

`_touch_recent()` 与 save/delete 已在同一个仓库事务锁内执行，索引写失败不再吞掉，并有保存/删除补偿与并发保存回归。

若未来允许多进程同时运行，则使用文件锁、SQLite，或把 recent index 设计成可由项目文件重建的派生数据。

### P1-8 已实现：EPUB 映射的可恢复业务代际

单个 JSON/TXT 的 `fsync + replace` 不能保证一组文件同时可见。EPUB 导入会连续写多个映射文件，自动保存和队列检查点也会先后写译文、映射和项目状态；进程在中间退出时可能留下互不匹配的代际。

EPUB mapping 已通过 [`src/infrastructure/mapping_repository.py`](../src/infrastructure/mapping_repository.py) 实现 generation/manifest 协议：完整导入先将 `content_mapping.json`、`images.json` 和 `format_info.json` 写入 `.mapping_generations/<uuid>/`，最后原子发布唯一的 `mapping_manifest.json`。读取端统一使用 `resolve_mapping_file()`；manifest 存在时，缺失成员或损坏 manifest 会显式报错，绝不回退并混读顶层兼容副本。

译文保存和旧图片资产迁移使用 `publish_mapping_file_update()` 只发布变更成员，并保留其余已发布成员的引用。导出、图片服务、两个图片 Provider 和 UI 预检查均已迁移到该解析接口；顶层 JSON 仅保留给旧版本集成的最佳努力兼容副本，不能再作为新代码的事实来源。

故障注入覆盖完整 generation 成员写入失败时 manifest 仍指向上一完整版本，以及删除顶层 `images.json` 后的旧图片迁移仍能读取并发布新 generation。当前映射与图片定向回归为 `96 passed`。

修复要求：

- 每个关联文件携带同一 generation，写完后用原子 commit manifest 发布完整代际；EPUB 导入可先写入 staging 目录后一次切换。
- 恢复只接受完整的 commit manifest：不完整新代际必须回退到上一完整代际或明确报告可恢复错误，不能悄悄混合读取。
- 记录写入阶段、generation、字节数和故障原因，为清理与支持诊断提供证据。

验收：在每个写入边界注入崩溃/异常，恢复结果只能是上一完整代际或新完整代际；已确认批次不得丢失，也不得将错配译文发布给用户。

边界：该协议解决进程崩溃时的可见性和混读问题，但尚未为多个进程同时写入同一 mapping 目录提供串行化。若产品允许该场景，必须再引入跨进程文件锁、SQLite WAL 或 append-only journal，避免两个 writer 基于同一旧 manifest 发布而造成最后写入覆盖。

## 7. P1/P2：性能优化点

### PERF-1 Tk 事件邮箱增加单帧预算

当前通用 callback 队列和 translation terminal deque 没有容量上限，`drain()` 每轮取出全部事件，pump 也在一轮内执行全部回调。多个窗口各自以固定 50ms 轮询。已有 keyed coalescing 是正确方向，但不能约束普通回调和突发终态。

推荐实现：

- root 级统一 pump，而不是每个组件独立常驻一个 pump。
- 每帧最多处理 64 项或 8ms，任一先到即让出 Tk 主线程。
- 有积压时用 `after_idle`/短间隔继续；空闲时退让到 250-500ms。
- 进度、窗口尺寸等 keyed 状态保留最新值；终态、用户命令和错误保持 FIFO，不得丢弃。
- 暴露 pending、coalesced、discarded、oldest-age 和单帧耗时指标。

验收：1 万 callback burst 下 Tk heartbeat p99 不超过 50ms，最大停顿不超过 100ms。

### PERF-2 大文档翻译前复用版本化快照

点击翻译后，Tk 线程会：

1. 从 `TranslationDocument` 多次生成原文/译文列表。
2. 构造完整 `TranslationProject`。
3. 在 preflight 中再次扫描。
4. 在 controller 中重新取列表、查找缺失行并 `join`。
5. worker 内再做分割和批次规划。

100MB 或 10 万行文档会产生重复 O(N) 扫描和内存峰值。

推荐引入不可变 `DocumentSnapshot(version, source_lines, target_lines, pending_indices, edit_flags)`。同一 version 的预检、费用估算和任务启动复用同一快照；token 估算和批次规划放到后台，并用 version 检查迟到结果。

验收：100MB/10 万行 fixture 记录点击到 worker 启动时间、Tk 连续阻塞时间和峰值 RSS；Tk 单次不可中断工作低于 50ms。

### PERF-3 任务详情页只消费增量数据

队列主列表已使用轻量快照，但详情页每 500ms 分别刷新状态和译文，两次 `get_task()` 都会复制完整原文与译文，随后仍全行比较目标文本。

推荐接口：

- 打开窗口时只取一次不可变原文。
- 状态栏读取 `QueueTaskSnapshot`。
- Coordinator 发布 `generation + changed_ranges` 或按区间读取目标行。
- 详情窗口只更新变化区间；关闭窗口后停止订阅。

验收：10 万行任务空闲刷新不再创建 O(N) 列表，单行变化只读取并渲染该区间。

### PERF-4 降低检查点全量写放大

延迟到 debounce 后再构造快照已经实现，但到期后仍复制任务全量状态。TXT checkpoint 同时重写完整译文和项目 JSON，项目保存还更新 recent index；EPUB mapping 也会完整加载、排序和序列化。

推荐分两步：

1. 短期记录 generation、changed indices、写入字节、`fsync` 次数和耗时；recent index 不随每个 checkpoint 重写。
2. 中期使用 SQLite WAL 或 append-only journal 增量保存译文、失败行和人工编辑；只在暂停、终态、显式导出时生成兼容 TXT/JSON。

验收：50 个任务、每个 1 万行的固定基准中，运行期写入字节和 `fsync` 数量相较当前基线显著下降，崩溃恢复仍不丢已确认批次。

### PERF-5 EPUB 导出改为逐章流水线

当前导出会先保留全书章节解析结果和双 DOM，之后逐章改写；写包后还会再次重写 ZIP，最后 `testzip()` 全量读取并校验 CRC。大书会同时放大 RSS、CPU 和磁盘 I/O，取消也主要发生在阶段边界。

推荐实现：

- 逐章解析、校验、改写并释放 DOM。
- 在首次写包前完成 spine/nav 属性处理，避免第二次全包重写。
- 若库接口不支持流式写，至少限制同时驻留的 DOM 数量。
- 将取消检查下传到章节、图片和压缩循环。
- 完整 CRC 校验保留在发布/高可靠模式；交互导出可校验结构、关键成员和最终文件可读性，并允许用户选择完整验证。

验收：200 章/500 图 fixture 的峰值 RSS 比当前下降至少 30%，不再发生第二次全包重写。

### PERF-6 图片链路减少 Base64 和重复落盘

在线图片路径中，已经编码的数据仍会解码验证、重新编码，并再次解码计算原图大小。图片资产的 checksum 命中路径现已直接复用已有文件和元数据，不再执行原子写或 `fsync`；端到端 Base64 往返仍有优化空间。

推荐内部始终传递 `bytes + mime_type`，只在 HTTP JSON 边界编码一次 Base64；保存层在 checksum、大小和目标文件均命中时直接返回现有元数据。后者已实现并有回归，前者仍需基准测量后逐段迁移。

验收：50 张 10MB 图片记录 CPU、分配量和峰值 RSS；去重命中时写入字节和 `fsync` 次数均为 0。

### PERF-7 缓存增加全局字节预算和 single-flight

[`src/core/smart_cache.py`](../src/core/smart_cache.py) 是每个 API 实例自己的内存 LRU，只限制条目数，不限制字节数，也没有同 key single-flight。队列中多个实例遇到相同文本时仍可能同时请求 Provider，既浪费时间也可能重复计费。

推荐：

- 以 provider、endpoint、model、prompt version、glossary version 和规范化输入构造完整 key。
- 加入总字节、单项字节和 TTL 上限。
- 同 key 并发只允许一个 owner 请求，其他调用等待相同 Future。
- 失败、取消和超时不能写入成功缓存，等待者必须收到同一结构化终态。
- 若做跨进程持久缓存，先解决加密、容量、迁移和用户清除语义。

### PERF-8 长单行流式预览限频

长响应尚未出现换行时，每个 chunk 都可能重新 `join` 未完成行并复制完整 preview，容易退化为近似 O(N²)。将普通预览限制到约 20Hz，只在换行、批次终态和运行终态立即发布；维护增量 buffer，避免每个 chunk 重建全部字符串。

验收：1MB 无换行流式响应的 CPU 时间随输入近似线性，预览事件数量有明确上限。

### PERF-9 启动导入按功能延迟

`import main` 中位数约 537ms，`src.bootstrap` 是最大累计路径。用 `-X importtime` 和同机重复基准确认后，将仅设置窗口、Full Edition、EPUB 导出或图片翻译使用的重依赖延迟到第一次使用。

不要为了数字把核心错误推迟到用户操作时才暴露。启动必需依赖仍应尽早验证，并通过启动状态明确报告。

建议目标：同机冷启动导入中位数下降至少 25%，且首次打开延迟功能的额外等待有状态反馈。

### PERF-10 清理“有设置、无行为”的参数

`queue_adaptive_concurrency`、`max_batch_input_tokens` 和 `min_batch_input_tokens` 已进入配置/Policy，但生产调度路径没有完整消费这些值。可见但无效的设置比没有设置更危险，因为用户会基于错误假设调整性能。

应为每项建立“配置变化 -> 运行时行为变化”契约测试。短期无法实现时，从 UI 隐藏并保留迁移兼容；实现后在队列快照中显示实际生效值。

### PERF-11 补齐队列导入取消和 EPUB 快速复用

队列批量导入已经后台化，但取消只阻止提交新文件，不能中止已经在线程池中解析的大 EPUB。将 cancel token 传入 `ConcurrentTranslationManager.add_task()` 和 `EPUBProcessor.import_epub()`。

EPUB mapping 快速复用目前以 size+mtime 命中，虽然项目数据已保存 SHA-256，但该快速路径没有复核内容哈希。建议采用“size+mtime 快速拒绝，内容哈希最终确认”，或保存更强的文件标识并提供可配置的信任策略。

## 8. P2：工程维护

### 8.1 异常处理

约 268 处宽泛异常捕获中，不少位于 UI 回调、清理和插件边界，不能机械删除。按风险处理：

1. 持久化、密钥、路径和协议解析：捕获具体异常，返回结构化失败，禁止静默 `pass`。
2. 线程和资源清理：允许 best-effort，但必须记录资源名、阶段和 correlation ID。
3. UI 回调边界：阻止异常击穿事件循环，同时将安全摘要呈现给用户。
4. EPUB 章节级处理：区分可跳过内容错误和必须中止的结构/编程错误。

### 8.2 拆分超大协调模块

按职责和可测试边界拆分，不以行数为唯一目标：

- `queue_scheduler.py`：registry、dispatcher、limiter ownership、finalizer、checkpoint adapter、snapshot publisher。
- `main_window.py`：layout、command policy、document workflow、preflight、save/close coordinator。
- `settings_window.py`：provider form、image settings、queue settings、persistence transaction。
- `translation_controller.py`：run state machine、event reducer、translation commands、export commands。

拆分后的对象应通过 Protocol 注入，状态转换应能在无 Tk、无网络、无磁盘的单元测试中运行。

### 8.3 包布局作为 2.0 迁移项

当前发布的顶层包名是通用的 `src`，且没有标准 GUI entry point。长期迁移为标准 src-layout：

```text
src/
  ai_translater/
    application/
    domain/
    infrastructure/
    ui/
```

并在 `[project.scripts]` 或 GUI scripts 中声明入口。该迁移会影响导入、PyInstaller、测试和恢复数据路径，不应与本轮 P1 修复混在一个发布中。

### 8.4 删除静态测试结果文件

仓库跟踪的 `test_results.txt` 到 `test_results4.txt` 只记录旧的“22 failed / 879 passed”，没有提交、环境或失败详情，已经误导当前状态。删除这些文件，由 CI 保存 JUnit、coverage、benchmark JSON 和日志 artifact。

## 9. 推荐实施顺序

### 阶段 0：建立可信基线

1. 决定支持矩阵和 Text/Full 产物形式。
2. 修复 4 个真实契约测试失败。
3. 恢复 CI quality job，保存测试和覆盖率 artifact。
4. 生成与 runner 完全匹配的依赖锁。
5. 将本文动态结果保存为初始基线，不把沙箱权限失败混入产品失败。

### 阶段 1：并发和数据边界

1. 配置与密钥原子快照。
2. 项目保存/删除与 recent index 的可观察事务结果。
3. EPUB、自动保存和检查点的 generation/commit manifest 恢复策略。
4. 为所有可见队列设置补齐“配置变化 -> 运行行为变化”的契约测试。

### 阶段 2：高收益交互性能

1. root 级有预算事件泵。
2. 版本化 `DocumentSnapshot` 和后台预检/批次规划。
3. 任务详情轻量状态 + changed ranges。
4. 长单行流式预览限频。

### 阶段 3：I/O、内存和 API 成本

1. checkpoint journal/SQLite WAL。
2. EPUB 逐章导出并移除二次全包重写。
3. 图片 bytes 内部通路和命中免写。
4. 全局字节预算缓存与同 key single-flight。
5. EPUB 快速复用内容哈希确认。

### 阶段 4：类型与结构治理

1. 按模块消除 244 个 Pyright 错误。
2. 收紧 unknown/missing 诊断。
3. 拆分四个超大协调模块。
4. 规划 `ai_translater` 标准包布局和 GUI entry point。

## 10. 性能基准与验收

没有固定数据集、采样方式和基线 artifact 时，不应声称“性能已经优化”。建议建立以下五组基准：

| 场景 | 规模 | 主要指标 |
| --- | --- | --- |
| 队列 | 50 个任务 x 10,000 行 | 吞吐、limiter 正确性、checkpoint bytes/fsync、关闭耗时 |
| UI burst | 10,000 个 callback/terminal event | Tk heartbeat p50/p95/p99、最大停顿、积压年龄 |
| 大 TXT | 100MB 或 100,000 行 | 点击到 worker、主线程最长阻塞、RSS、快照复制次数 |
| 大 EPUB | 200 章、500 图 | 导入/导出分阶段耗时、RSS、ZIP 读写字节、取消延迟 |
| 图片 | 50 张 x 10MB | Base64 CPU/分配、峰值 RSS、命中写入字节、Provider 调用数 |

统一测量规则：

- 固定 Python minor、依赖锁、操作系统、CPU 电源模式和数据集。
- 每个场景至少 warm-up 1 次、正式运行 5 次，报告中位数和 p95。
- 同时记录 wall time、CPU time、峰值 RSS、磁盘字节、`fsync` 次数和请求数。
- 基准 JSON 包含 Git SHA、环境和参数，作为 CI artifact 保存。
- 性能 PR 必须提供前后对比；功能测试绿色是性能改动的前置条件。

## 11. 推荐验证命令

在全新虚拟环境中执行，不复用全局 site-packages：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip check

.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\python.exe -m compileall -q main.py build.py src tests tools
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest --cov=src --cov-branch --cov-report=term-missing --cov-report=xml
```

发布构建还应执行：

1. 使用与 CI Python minor、平台和 edition 完全一致的 `--require-hashes` lock 安装。
2. `python -m build --wheel` 后在第二个干净环境安装 wheel。
3. 从仓库目录外执行 import/entry-point smoke，防止源码路径掩盖漏包。
4. 对 PyInstaller 产物执行启动、关键资源、版本、edition 能力和关闭 smoke。
5. Full Edition 额外执行 Manga worker health check；不下载模型的 smoke 与含模型集成测试分开。

## 12. 旧问题状态对照

旧指导文档中的以下结论已经过期，不应继续作为未完成 P0/P1：

| 旧问题 | 当前状态 | 当前证据/剩余边界 |
| --- | --- | --- |
| 关闭取消发生在不可逆 teardown 后 | 已修复 | `tests/test_config_save_result.py` 有关闭取消回归 |
| 翻译中替换会话永久 busy | 已修复 | `tests/test_translation_single_flight.py` 覆盖会话替换 |
| 密钥保存语义把失败当成功 | 已修复 | 结构化保存结果已有测试；本轮新问题是并发快照一致性 |
| Manga stderr 500 行后停止 drain | 已修复 | `tests/test_image_cancel_p1_8.py` 覆盖持续 drain |
| `max_active_tasks` 不生效 | 已修复 | Coordinator 已按活动窗口惰性 prepare |
| Future 完成只能等待固定轮询 | 已修复 | Future callback 会设置统一 wake event |
| Provider 切换后 limiter 释放错配 | 已修复 | `_InFlightBatch` 固定持有派发时 limiter；定向回归覆盖切换、取消和 submit 失败 |
| 队列关闭按任务数累加超时 | 已修复 | Coordinator 与图片翻译关闭均使用单一绝对 deadline；图片 worker 未退出时不并发释放 Provider/loop |
| debounce 前构造全量 checkpoint | 已修复 | 到期后才构造快照；全量写放大仍是性能待办 |
| 终态在全局锁内 flush/close | 已修复 | finalizer executor 已移出主要锁区 |
| traceback 未脱敏 | 已修复 | `tests/test_log_sanitizer.py` 已覆盖 traceback |
| EPUB 没有资源限制和取消 | 已修复 | 有 ZIP 上限和章节/图片安全点；队列在途导入取消已下传到处理器 |
| 损坏项目静默覆盖与路径穿越 | 已修复 | 损坏文件隔离、项目 ID 正则和 resolve 后直接子路径校验均有回归；recent index 事务仍待补齐 |
| 队列批量导入阻塞 Tk | 已修复 | 已使用后台 executor；在途 EPUB 解析可由取消令牌中止 |

新增待办应以本文第 5-8 节为准。修复某个问题后，应同时更新状态表、回归测试和对应验收指标，避免指导文档再次变成过期快照。

## 13. 不建议采用的做法

- 不要为了 Pyright 绿色关闭更多规则或批量添加 `Any`/`type: ignore`。
- 不要用提高线程数掩盖全量复制、锁竞争和写放大。
- 不要把所有宽泛异常捕获机械替换；先处理数据、密钥、协议和线程生命周期边界。
- 不要引入缓存而忽略 key 完整性、容量、取消和失败传播。
- 不要在 UI 中保留生产代码不消费的性能设置。
- 不要在当前 P1 修复期间同时迁移 GUI 框架或顶层包名。
- 不要以一次本机运行替代 CI 支持矩阵，也不要把 mock 性能测试当作真实 RSS/I/O 基准。

## 14. 完成定义

当以下条件全部满足时，才可判断项目形成发布级 Python 最佳实践闭环：

1. CI 的 lint、format、类型、测试、coverage、wheel smoke 和打包检查是 required checks。
2. 声明支持的 Python/平台/edition 组合都有匹配 lock 和可重复测试证据。
3. pytest 全绿；环境限制被明确隔离，不与产品失败混淆。
4. Pyright 阻断范围覆盖关键领域、配置、队列、Provider 和持久化路径，并持续扩大。
5. 配置密钥、项目保存/删除、recent index、图片关闭和 endpoint 有并发/故障回归测试；已修复的 limiter 与队列 deadline 回归持续保留。
6. Tk 主线程和全局锁内不存在不可控 I/O 或无界工作。
7. 五组固定性能基准有同机前后数据，性能改动满足本文验收指标。
8. README、workflow、spec、依赖锁、测试和实际发布产物描述一致。
