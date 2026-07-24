# 性能与 Python 最佳实践当前审计

审查日期：2026-07-24（Asia/Shanghai）  
审查基线：`5b5e0bb0fbf66cccc24a697e222962311f49ba2e`  
运行环境：Windows，CPython 3.13.5，Ruff 0.6.9，Pyright 1.1.411  
审查范围：`main.py`、`build.py`、`src/**`、`tests/**`、`tools/**`、依赖、CI 和 PyInstaller 配置

本文是当前代码快照的审计结果和整改清单。历史决策背景仍见：

- [`PYTHON_BEST_PRACTICES_PERFORMANCE_UX_OPTIMIZATION_GUIDE.md`](PYTHON_BEST_PRACTICES_PERFORMANCE_UX_OPTIMIZATION_GUIDE.md)
- [`PYTHON_PERFORMANCE_UX_CURRENT_AUDIT_AND_IMPLEMENTATION_PLAN.md`](PYTHON_PERFORMANCE_UX_CURRENT_AUDIT_AND_IMPLEMENTATION_PLAN.md)

## 1. 结论

当前项目已经具备较好的 Python 工程基础，但还不能认定为“达到发布级 Python 最佳实践”。更准确的判断是：

- 功能正确性和回归测试基础较强。
- 翻译并发、连接复用、取消、原子写入和 Tk 线程边界已有系统性设计。
- 中小规模文档的主链路可用。
- 大 EPUB、图片较多的 EPUB、多文件队列和长时间自动保存仍有明确的 I/O、内存和 UI 响应热点。
- 基础 Ruff 规则绿色，但类型门禁、复杂度门禁和真实性能基准没有闭环。

| 维度 | 当前状态 | 结论 |
| --- | --- | --- |
| 架构分层 | 良好 | 已有 composition root、domain/application/infrastructure/ui 分层和依赖注入 |
| 并发与网络 | 较好，但有协调缺口 | 有连接池、全局队列执行器和 ProviderLimiter；429 首次反馈仍被客户端内部重试隐藏 |
| 持久化 | 正确性较强，扩展性不足 | 原子写和 generation/manifest 已实现，但存在全量写放大和 generation 不回收 |
| UI 性能 | 已有合并和分块，预算未接通 | 通用 pump 声明了 8ms 预算，但生产 `_poll()` 没有执行时间预算 |
| 图片链路 | 安全边界较强，内存/磁盘放大明显 | 同时保留二进制和 Base64，且读取优先 Base64 |
| 测试 | 强 | 本机全量 `1029 passed, 41 skipped` |
| 类型安全 | 未达标 | Pyright `195 errors, 1 warning`，CI 仍为非阻断报告 |
| 可维护性 | 中等 | 51 个函数复杂度超阈值，核心协调函数过大 |
| 发布体积 | 可优化 | onefile 产物约 37.5 MiB，存在未被生产代码使用的重依赖 |

## 2. 本次验证结果

### 2.1 动态与基础门禁

| 检查 | 结果 | 判断 |
| --- | --- | --- |
| `ruff check .` | 通过 | 当前启用的 E/F/I/UP/B/SIM 规则绿色 |
| `ruff format --check .` | 163 个文件已格式化 | 格式基线绿色 |
| `pip check` | `No broken requirements found` | 当前虚拟环境依赖一致 |
| `pytest -q` | `1029 passed, 41 skipped in 65.20s` | 功能回归基础强 |
| 性能专项 | `80 passed, 10 skipped in 2.79s` | 已有性能不变量测试可用 |
| Pyright | `195 errors, 1 warning` | 不能作为绿色门禁 |
| 额外复杂度/性能规则 | 160 项 | 核心函数拆分和热路径治理仍不足 |

源码规模：`src` 下 83 个 Python 文件、约 23,680 行；存在 215 处 `except Exception`、78 个独立 `pass`，没有裸 `except:`。

### 2.2 现有队列 benchmark 不能作为基线

执行：

```powershell
.\.venv\Scripts\python.exe tools\benchmark_queue_translation.py `
  --scenario all --tasks 3 --lines 100
```

工具虽然退出码为 0，但在 Windows 使用了 `/bench/task_*.txt`。该路径被解析为 `\bench\...`，检查点保存持续触发 `PermissionError`，每个任务最多重试 5 次并进入保存失败终态。因此输出的 8-14 秒耗时包含错误重试等待，不能用于比较队列吞吐。

必须先把 benchmark 改为 `TemporaryDirectory` 或显式可写工作目录，并让保存失败导致场景失败，而不是仍返回成功结果。

### 2.3 映射保存微基准

本机用 10,000 条映射、约 3.08 MiB 的 `content_mapping.json` 连续保存 10 次：

| 指标 | 结果 |
| --- | ---: |
| 10 次保存耗时 | 1.183 秒 |
| generation 目录 | 11 个 |
| generation JSON | 13 个 |
| 当前 content 文件 | 3.08 MiB |
| 临时工作区总量 | 36.07 MiB |
| 相对当前 content 放大 | 11.7 倍 |

这不是理论推测：当前代码每次更新都会创建新 generation、发布 manifest、再写顶层兼容副本，而不可达的旧 generation 没有回收。

## 3. 性能整改优先级

### P1-PERF-1：修复 EPUB 映射全量写放大和 generation 泄漏

证据：

- [`mapping_repository.py`](../src/infrastructure/mapping_repository.py) 的 `publish_mapping_file_update()` 每次创建新的 UUID generation。
- 同一函数发布 manifest 后还会写顶层兼容副本。
- `save_translations()` 每次读取完整 JSON、按 `line_number` 排序、更新全量对象并完整序列化。
- [`autosave.py`](../src/application/autosave.py) 最长约 10 秒就可能触发一次 EPUB 全量保存。
- [`queue_checkpoint.py`](../src/core/queue_checkpoint.py) 默认 debounce 仅 0.5 秒。
- 仓库中没有对 `.mapping_generations` 的可达性扫描或回收逻辑。

影响：

- 保存成本为 O(N log N) 排序 + O(N) 序列化 + 多次完整写盘和 `fsync`。
- 长任务会持续增长工作区，不随活动数据大小稳定。
- 大映射会增加 SSD 写入、保存线程占用和关闭等待。
- 磁盘接近满时，原本用于可靠性的检查点会反过来增加保存失败概率。

推荐实现，按风险分两步：

1. 短期修复
   - 用 `line_number - 1` 直接定位译文，替代每次 `sorted(...)`，将更新从 O(N log N) 降为 O(N)。
   - manifest 发布后做 mark-and-sweep，只保留当前 manifest 实际引用的 generation 目录；若需要回滚，再显式保存一份 previous manifest。
   - 将顶层兼容副本改为兼容开关或迁移阶段行为，新版本内部不得依赖它。
   - 记录每次保存的 JSON 字节数、`fsync` 次数、generation 数和耗时。
2. 中期修复
   - 运行期只追加译文变更 journal，或把行状态放入 SQLite WAL。
   - 暂停、完成、显式保存和导出时再压实为兼容 JSON/TXT。
   - immutable 原文与 mutable 译文分开持久化，避免 TXT checkpoint 每次重复序列化整份原文。

验收：

- 10 次相同规模保存后，不可达 generation 数为 0。
- 运行期写入字节不再近似“映射大小 x 保存次数 x 2”。
- 故障注入仍只能恢复到上一完整代际或当前完整代际。
- 10,000 行保存的 p95、写入字节和 `fsync` 数进入 benchmark artifact。

### P1-PERF-2：消除图片二进制、Base64 和映射副本的叠加放大

证据：

- [`image_asset_store.py`](../src/infrastructure/image_asset_store.py) 的 `save_image_binary()` 写入二进制资产后，仍把同一原图编码到 `base64_data`。
- `images.json` 又同时写入 generation 和顶层兼容副本。
- `load_image_bytes()` 优先解码 `base64_data`，只有 Base64 缺失才读取二进制资产。
- [`epub_processor.py`](../src/core/epub_processor.py) 的未变化快速路径会完整解析 `content_mapping.json` 和含 Base64 的 `images.json`，只为读取少量缓存元数据。
- [`atomic_file.py`](../src/infrastructure/atomic_file.py) 先 `json.dumps()` 生成完整字符串，再 `.encode()` 生成完整 bytes，大 JSON 会产生额外峰值分配。

仅按文件内容计算，正常导入至少包含：二进制资产 1x + generation Base64 约 1.33x + 顶层 Base64 约 1.33x，即约 3.67x；源 EPUB 本身尚未计入。

推荐实现：

- 健康文件系统默认只保存二进制资产和 checksum。
- 写入后读回校验失败时，才创建扩展名不触发图片过滤器的受控 fallback blob；不要用 Base64 JSON 承担二进制仓库职责。
- 新读取路径先读本地资产并校验 checksum，再回退旧 Base64；旧 schema 保持只读迁移兼容。
- 把 `source_file_size`、mtime、内容 hash、图片 schema 和图片数量移到小型 manifest/index，缓存命中不再解析全量映射。
- 大 JSON 的原子写改为临时文件上 `json.dump()` 流式写入、flush、`fsync`、replace，避免同时持有完整 str 和 bytes。

验收：

- 正常文件系统下，工作区新增图片数据不超过原始图片总量的约 1.1 倍加小型元数据。
- fallback 只在读回校验失败的 fixture 中创建。
- 缓存命中不读取或解析完整 `images.json`。
- 50 张 10 MiB 图片记录导入时间、峰值 RSS、工作区字节和 Base64 编解码次数。

### P1-PERF-3：让共享 ProviderLimiter 立即看到首次 429/超时

证据：

- [`base_api.py`](../src/api/base_api.py) 的 `translate_stream()` 在客户端内部循环重试 408/429/5xx，并在各自线程内等待退避。
- [`queue_scheduler.py`](../src/core/queue_scheduler.py) 的 `execute_batch_job()` 只有在客户端重试耗尽并抛出 `TranslationRequestError` 后，才调用 `limiter.record_rate_limited()`。
- 每个队列任务有独立 API 实例和本地 `_rate_limit_pressure`，其他任务看不到第一次 429。

影响：多个并发任务可能同时收到 429、各自睡眠并重试；Provider 级 AIMD 和 cooldown 介入过晚，会形成重试同步和无效占槽。

推荐实现：

- 最小改动：BaseAPI 暴露 `on_retryable_response(status, retry_after)` hook，首次 429/超时立即通知共享 limiter。
- 完整改动：由队列 coordinator 持有批次重试计数、重新排队和 cooldown；BaseAPI 只执行单次网络尝试。主编辑器可继续复用同一重试服务，不维护第二套策略。
- 为退避加入 jitter，确保多个请求不会同一时刻再次冲击 Provider。
- `BatchOutcome.retry_count` 必须反映真实次数；当前生产路径始终写 0，应修复或删除该字段。

验收：4 个并发请求同时收到首次 429 后，全局 limiter 立即降并发且只形成一个 Provider cooldown；总请求数、重试数和等待时间可观测。

### P1-PERF-4：真正接通 Tk 单帧预算和回调合并

证据：

- [`ui_callback_mailbox.py`](../src/ui/ui_callback_mailbox.py) 已定义 `max_frame_ms`、`_deferred_callbacks`、帧耗时指标、`_schedule_backlog_poll()` 和 `_schedule_idle_poll()`。
- 生产 `_poll()` 仍直接执行本轮最多 64 个 callback，没有检查时间预算，也没有更新上述指标或使用自适应调度。
- [`concurrent_window.py`](../src/ui/concurrent_window.py) 的 `_on_task_update()` 对每个 kick 使用两个普通 `submit()`，没有使用已有 `submit_keyed()`。
- `TkTranslationEventPump` 会在一轮内处理全部 terminal 和 stream 事件，没有单帧时间预算。

推荐实现：

- 每执行一个 callback 后检查 `perf_counter()`；达到 8ms 即把剩余 callback 放回 `_deferred_callbacks`。
- 有积压使用 `after_idle`，空闲使用 250ms；不要无论状态都固定 50ms。
- 队列 tree、metrics、title、progress 统一使用稳定 key 合并，只保留最新刷新。
- 终态和错误保持 FIFO，但按帧预算分批渲染。
- 将通用 UI pump 尽量提升到 root 级共享，减少多个窗口的常驻轮询。

验收：10,000 callback burst 下 Tk heartbeat p99 小于 50ms、最大停顿小于 100ms；积压最终清空，终态事件不丢失。

### P1-PERF-5：清理 onefile 中未使用的重依赖

证据：

- 生产代码没有使用 `aiohttp` 或官方 `openai` SDK；OpenAI-compatible 实现直接继承自 BaseAPI/httpx。
- `pydantic` 和 `pydantic-core` 当前只因未使用的 `openai` 依赖进入环境。
- `requests` 只用于生成图片下载，项目已有 httpx。
- `translator.spec` 仍把 `aiohttp`、`openai` 和 `requests` 列为 hidden imports。
- 当前 onefile 产物为 39,315,229 bytes，约 37.5 MiB。

本虚拟环境中相关包的未压缩目录约为：OpenAI 10.55 MiB、Pydantic 3.50 MiB、pydantic-core 5.36 MiB、aiohttp 2.56 MiB、requests/urllib3 约 1.28 MiB。最终产物节省量必须用重建前后实测，不能直接相加。

推荐实现：

- 用持久 `httpx.Client`/受控 transport 替代图片下载的 `requests.get()`。
- 删除未使用的 `aiohttp`、`openai`、`pydantic` 直接依赖及 spec hidden imports。
- 重新生成 Windows hash lock，执行 SBOM、wheel smoke 和 Windows/macOS 产物 smoke。

验收：记录 onefile 大小、冷启动中位数/p95 和首次图片下载；功能测试及信任边界测试保持绿色。

### P2-PERF-6：批量命令和导出不要复制所有任务全文

证据：

- `ConcurrentTranslationManager.get_task()` 会复制完整 source/target。
- `get_all_tasks()` 对所有任务执行上述复制。
- 取消全部、删除完成任务、批量导出和全部图片翻译都调用 `get_all_tasks()`；其中多数只需要 id、状态、路径和类型。
- `tasks` 兼容属性在字典推导式的条件和值中调用两次 `get_task()`，同一任务可能复制两次全文。

推荐：列表命令只使用轻量 `QueueSnapshot`；导出 worker 按 task id 逐个读取最小导出描述，不预先复制所有正文；兼容属性用一次查询结果或删除不可达 API。

### P2-PERF-7：大 TXT 编码检测只采样，不对全文件运行 chardet

[`file_handler.py`](../src/utils/file_handler.py) 先完整读入 bytes，再对全部内容执行 `chardet.detect()`，随后再解码为 str。建议优先 BOM/UTF-8 strict，无法判断时只对固定上限样本做检测，再流式/分块解码。100 MiB fixture 应记录检测 CPU 和峰值 RSS。

### P2-PERF-8：共享缓存和 Provider 传输层

- SmartCache 是每个 API 实例独立的 1,000 条 LRU；没有全局字节预算和同 key single-flight。
- 队列每任务各有客户端，相同 Provider 的任务不能复用跨任务 TLS 连接和缓存。

先共享缓存，再评估共享 transport。共享 transport 前必须把“取消任务 = 关闭整个 client”改成请求级取消并加入引用计数，否则会破坏任务隔离。缓存 key 必须包含 provider、endpoint、model、temperature、prompt schema/version、glossary version 和规范化输入。

### P2-PERF-9：减少 EPUB 重复遍历和缓存命中全量解析

`import_epub()` 分别遍历 manifest、CSS、正文和图片，并在图片阶段再次 `list(book.get_items())`。可以在一次 item 索引中收集 manifest/CSS/image/document 引用，正文仍按 spine 顺序处理。优先级低于写放大和 Base64，因为这些遍历本身通常是线性的。

### 配置层面的立即建议

当前仓库 `config/app_config.json` 使用 `batch_lines=10`、`translation_concurrency=1`。这不是代码缺陷，但意味着主翻译主动串行。完成 429 协调和真实 benchmark 后，可在固定模型/固定数据集上比较并发 1、2、4；只在吞吐提高且 429、超时、费用和对齐错误不恶化时提高默认值。不要直接把线程上限从 1 调到 8。

## 4. Python 最佳实践与错误清单

### 4.1 已经做得好的部分

1. `main.py` 和 `bootstrap.py` 已形成清晰组合根，基础依赖可注入和替换。
2. domain/application 层使用 dataclass、Enum、Protocol 和结构化结果，方向正确。
3. Tk 工作线程通过 mailbox/event pump 回主线程，避免直接跨线程操作控件。
4. 队列只有一个全局请求 executor，没有两层线程池乘法；finalizer 与请求执行器分离。
5. 文本 HTTP 客户端持久化并配置连接池、分项 timeout、取消和显式 close。
6. 原子文件写入使用同目录临时文件、flush、`fsync`、replace 和 Windows 有界重试。
7. EPUB、图片下载、路径、密钥和日志脱敏已有较好的信任边界。
8. 大列表已有分块加载、值缓存、搜索 debounce 和流式事件合并。
9. 测试数量和关键状态机覆盖明显高于普通 Tkinter 工具项目。

### 4.2 Pyright：195 errors，1 warning

按规则分类：

| 规则 | 数量 |
| --- | ---: |
| `reportArgumentType` | 63 |
| `reportAttributeAccessIssue` | 38 |
| `reportUnknownMemberType` | 24 |
| `reportUnknownVariableType` | 18 |
| `reportUnknownArgumentType` | 15 |
| `reportUnknownParameterType` | 10 |
| `reportMissingParameterType` | 6 |
| `reportCallIssue` | 5 |
| `reportMissingTypeArgument` | 4 |
| `reportGeneralTypeIssues` | 3 |
| `reportOptionalMemberAccess` | 3 |
| `reportAssignmentType` | 3 |
| `reportReturnType` | 2 |
| 其他 | 3 errors + 1 warning |

按文件分类：

| 文件 | 错误数 | 建议处理方式 |
| --- | ---: | --- |
| `application/translator_provider.py` | 28 | 为 engine 建 Protocol，定义 batch result/回调类型 |
| `application/autosave.py` | 26 | 为 file handler、EPUB port、Tk schedule/cancel 建 Protocol |
| `core/epub_processor.py` | 25 | 修正 Path/str、ebooklib 适配类型和映射 payload 类型 |
| `domain/project.py` | 23 | JSON 输入先验证/窄化，再构造强类型对象 |
| `ui/main_window.py` | 18 | 缩窄 Optional，修正 Tk 构造和 menu index 类型 |
| `core/translator.py` | 15 | 为 API、callback、batch data 定义明确协议 |
| `application/error_handling.py` | 10 | 为 context/extra 使用 `Mapping[str, object]` |
| `api/base_api.py` | 8 | Optional 默认参数写成 `T | None`，补全 callback 签名 |
| 其余 20 个文件 | 42 | 按 Optional、第三方 stub 和边界适配分类修复 |

其余文件计数：`file_importer.py` 4、`exporter.py` 4、`image_rewriter.py` 4、`document_session.py` 3、`settings_window.py` 3、`image_utils.py` 3、`form_validation.py` 3、`concurrent_window.py` 2、`image_translation_handler.py` 2、`queue_provider.py` 2、`queue_checkpoint.py` 2、`table_editor.py` 2、`ports.py` 2，以及 8 个文件各 1 条。

优先修复可能对应真实运行缺陷的告警：

- `file_importer.py:281`：成功结果中的 `target_path` 仍被类型系统视为 Optional。
- `file_importer.py:596,599`：`mapping_dir` 可能为 None 时调用 `relative_to()`。
- `table_editor.py:121`：`editing_column` 可能为 None 时用于列表索引。
- `main_window.py:1228,1230,1239`：menu index 可能为 None。
- `concurrent_manager.py:179`：声明返回非 Optional，但推导式可能保留 None，且重复调用 `get_task()`。
- `domain/project.py`：未经结构验证的 JSON 容器直接转换为 list/set/dict，类型错误也提示了损坏数据边界不够集中。
- `autosave.py`：调度回调返回值被假定为 str，但注解是 object，导致保存取消句柄契约不明确。

唯一 warning 是 `image_translator.py:21` 的 `requests` source 无法解析；清理 requests 依赖后可一并消失。

修复顺序：domain JSON 边界 -> application Protocol -> core translator/API -> persistence -> UI Optional/Tk adapter。禁止批量添加 `Any` 或 `type: ignore`。

### 4.3 复杂度与函数规模

额外运行 `C901/PLR0911/PLR0912/PLR0913/PLR0915/PERF` 得到 160 项：

| 规则 | 数量 |
| --- | ---: |
| C901 复杂函数 | 51 |
| 分支过多 | 29 |
| 参数过多 | 26 |
| 语句过多 | 26 |
| 循环内 try/except | 13 |
| return 过多 | 10 |
| 其他 PERF | 5 |

最高风险函数：

- `EPUBProcessor.import_epub()`：复杂度 91、约 89 个分支、336 条语句。
- `TranslatorEngine._translate()`：复杂度 45、176 条语句。
- `TranslatorEngine._translate_batch()`：复杂度 37、145 条语句。
- `BaseAPI.translate_stream()`：复杂度 28、80 条语句。
- `export_epub()`：复杂度 26、89 条语句。
- `MainWindow.close()`：复杂度 24、84 条语句。

拆分应围绕可测试职责，而不是机械按行数切文件：

- EPUB：cache validator、metadata collector、content extractor、image extractor、mapping publisher。
- 翻译：batch planner、request executor、stream parser、alignment parser、result reducer。
- API：request builder、retry observer、SSE decoder、metrics recorder。
- 关闭：decision reducer、persistence phase、resource phase、final UI phase。

### 4.4 异常处理

215 处 `except Exception` 中，worker 顶层、UI 事件边界和资源清理边界可以保留宽捕获，但以下位置不应静默：

- 映射/项目/配置持久化。
- EPUB 章节、manifest、spine 和图片解析。
- Provider 协议解析和 SSE 解码。
- 密钥保存与版本发布。

建议：

- 数据边界捕获具体异常并 `raise ... from exc`。
- 可跳过的单项失败记录 item id、阶段和 correlation id。
- 清理失败至少 debug/warning，不使用无信息 `pass`。
- UI 边界将内部异常转换为结构化、脱敏且可操作的用户错误。

### 4.5 CI 门禁仍不完整

- Pyright 使用 `continue-on-error: true`，195 个错误不会阻止合并。
- coverage 会生成报告，但没有冻结当前基线或设置 `--cov-fail-under`。
- Linux 质量矩阵较完整，Windows/macOS 构建 smoke 仍主要验证进程存活，没有验证“主窗口已交互、正常关闭、无残留线程”。
- macOS 仍安装浮动 `requirements.txt`，没有平台/Python 对应 hash lock。

建议先让 `src/domain` 和 `src/application` 真正零错误并阻断，再逐步扩大；coverage 先记录当前 branch baseline 并禁止下降，不要直接设置任意高比例。

## 5. 推荐实施批次

| 批次 | 内容 | 主要文件 | 风险 |
| --- | --- | --- | --- |
| A | 修复 benchmark 路径、失败退出和指标输出 | `tools/benchmark_queue_translation.py` | 低 |
| B | generation GC、O(N) 行号更新、兼容副本开关、写入指标 | `mapping_repository.py`、相关测试 | 中高 |
| C | 图片 binary-first、条件 fallback、轻量 cache manifest | `image_asset_store.py`、`epub_processor.py` | 高 |
| D | 首次 429 通知共享 limiter，补真实 retry_count | `base_api.py`、`queue_scheduler.py`、`queue_provider.py` | 高 |
| E | 接通 Tk 8ms 预算和 keyed refresh | UI mailbox/pump、`concurrent_window.py` | 中 |
| F | 删除未用依赖并重建 lock/spec/产物 | pyproject、requirements、spec、lock | 中 |
| G | 类型修复第一批：domain/application | `domain/**`、`application/**`、CI | 中 |
| H | 核心函数按职责拆分 | EPUB、translator、API、close | 高，放在行为回归稳定后 |

每个批次独立提交，性能变更必须附同机前后数据；不要把持久化格式、并发重试、依赖清理和大规模重构混入同一提交。

## 6. 建议新增的性能门禁

| 场景 | 固定规模 | 指标 |
| --- | --- | --- |
| Mapping 保存 | 10,000/100,000 行，连续 100 次 | wall/CPU、写入字节、fsync、generation 数、RSS |
| UI burst | 10,000 callback + terminal event | Tk heartbeat p50/p95/p99/max、积压年龄 |
| 队列 | 50 任务 x 10,000 行 | 吞吐、429、retry、checkpoint bytes、关闭耗时 |
| 大 TXT | 100 MiB/100,000 行 | 编码检测、导入时间、主线程停顿、RSS |
| 大 EPUB | 200 章/500 图 | 阶段耗时、RSS、工作区放大、取消延迟 |
| 图片 | 50 x 10 MiB | Base64 次数、磁盘放大、RSS、API 请求数 |
| 打包启动 | Windows/macOS onefile | 产物大小、首帧、可交互、正常关闭 |

统一规则：固定 Python minor、依赖 lock、fixture hash 和机器电源模式；warm-up 1 次、正式运行至少 5 次，报告中位数和 p95；benchmark 内部错误必须让进程非零退出。

## 7. 完成定义

满足以下条件后，才能把项目评价提升为“达到当前规模下的 Python 最佳实践”：

1. 基础 Ruff、格式、pytest、pip check 持续绿色。
2. domain/application/core 关键链路 Pyright 为零并成为阻断门禁。
3. mapping generation 有可达性回收，连续保存的磁盘占用稳定。
4. 图片默认路径不再同时保存 raw + 两份 Base64 JSON。
5. ProviderLimiter 在首次 429/超时时立即协调所有任务。
6. Tk 单帧预算真实生效，10,000 事件 burst 满足 heartbeat 预算。
7. 队列、大 TXT、大 EPUB、图片和打包启动都有可重复的前后基准。
8. 未使用依赖从 pyproject、requirements、lock 和 spec 同时删除。
9. 核心持久化、协议和路径边界不再静默吞掉未知异常。
10. README、CI、Python 支持范围、lock、spec 和真实产物保持一致。

## 8. 复现命令

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\pyright.exe
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q

.\.venv\Scripts\ruff.exe check src main.py build.py `
  --select C901,PLR0911,PLR0912,PLR0913,PLR0915,PERF `
  --statistics
```

Pyright 的完整逐条错误应以当前命令输出为事实来源；本文保存分类、优先级和高风险实例，避免错误行号在修复过程中迅速过期。
# 实施更新（2026-07-24，审计后续批次）

本节记录审计后的实际落地结果；下文保留首次审计时的证据与基线，避免将
历史问题误写成当前状态。

## 当前结论

- A--F、P2-6、P2-7、P2-9 已完成并保留了针对性回归测试：benchmark 使用
  可写临时目录并在保存失败时失败退出；映射 generation 可回收、保存按行号
  直接定位；图片路径保持 binary-first；首个 429/超时会通知共享限流器；Tk
  邮箱按帧预算/键合并刷新；未使用运行时依赖已从依赖定义与 Windows lock 中移除。
- 大 JSON 原子写现在使用 `json.dump()` 直接写入同目录临时文件，避免同时保留
  完整 JSON 字符串和字节串。
- domain、application、core、infrastructure 与 UI 的 Pyright 已全部收敛：
  `pyright` 当前为 **0 errors, 0 warnings**。CI 的 Python 3.12 类型检查已改为
  全仓阻断门禁，不再保留允许失败的全仓报告。
- EPUB 现在在导入时只建立一次 item 索引；缓存命中只读取轻量 manifest，避免为
  少量元数据解析完整 `images.json`。第三方库（ebooklib、BeautifulSoup、Pillow）
  的不完整 stub 被收敛在 EPUB/图片适配边界，持久化和领域层不再依赖未验证对象。
- 翻译器会将 Provider 返回的预算、并发建议与指标缩窄为受控数值；非数值返回将
  回退到当前安全值，避免错误值进入调度器。

## Windows 图片锁的处理

Windows 上偶发出现“刚刚原子替换的图片不能立刻读回”的 `PermissionError`。
读取路径已有有界指数退避；若重试后仍是 `PermissionError`，写入路径记录 warning、
保留已成功原子发布的二进制条目，并由后续读取路径继续校验。不会因此固化一份
`base64_data` 副本。真实写入失败、非权限 I/O 失败或 checksum 不一致仍使用受控
fallback，保证损坏数据不会被当作有效资产。这样既避免了 Base64 的长期磁盘/内存
放大，也保留了明确的故障可观测性。

已验证的本地命令（本轮）：

```powershell
.\.venv\Scripts\pyright.exe
# 0 errors, 0 warnings, 0 informations

.\.venv\Scripts\python.exe -m pytest `
  tests\test_image_asset_store.py `
  tests\test_epub_item_index_performance.py `
  tests\test_epub_import_integrity.py -q
# 11 passed

# Complete regression (workspace-local temporary directory)
# 1169 passed, 43 skipped in 93.62s
```

## 尚需持续治理的结构风险

`EPUBProcessor.import_epub()`、`TranslatorEngine._translate()`、
`TranslatorEngine._translate_batch()`、`BaseAPI.translate_stream()` 与
`MainWindow.close()` 仍是大规模协调函数。当前已把文档顺序、段落提取、映射持久化、
图片重写、运行上下文和第三方库边界抽出并用回归测试保护；但这些协调函数的分支数
仍高于严格复杂度规则。后续拆分必须以可独立验证的职责（缓存验证、内容提取、图片
提取、批计划、单批执行、结果归并、UI 终态处理）推进，不能用关闭 Ruff 规则、扩大
`Any` 或批量 `type: ignore` 取代重构。
