# 队列翻译并发优化实施方案

## 1. 文档目的

本文只针对文本/EPUB 的“批量翻译队列”，目标是让队列真正利用多个 API 在途请求缩短总耗时，同时保持：

- 全局并发有硬上限，不因任务数增加而失控；
- 行号映射、失败索引和输出顺序保持正确；
- 暂停、取消、重试不会被旧线程或旧回调覆盖；
- 429、超时和 Provider 限额对整个队列统一生效；
- 队列结束、移除任务或关闭窗口后不残留客户端和线程；
- 性能提升能够用固定基准复现，而不是只看 UI 体感。

本文基于 2026-07-17 的当前工作区代码。实现时应以当时的最新行号为准。

## 2. 执行结论

当前队列的主要问题不是 HTTP 不够快，而是没有形成足够的可并发工作：

1. 多文件任务严格串行。管理器默认 `max_concurrent=1`，并且只要存在任一 `running` 任务，就不再启动下一个文件。
2. 并发只存在于单个文件内部。队列默认按 80 行、16,000 input token 组大批次，许多文件最终只有 1 个请求，默认并发 2 没有第二个批次可执行。
3. 设置页保存的是交互翻译参数 `translation_concurrency`，队列读取的是隐藏参数 `queue_translation_concurrency`，用户调节“并发请求数”对队列无效。
4. 每个文件创建独立引擎、客户端和局部限流状态。直接开放文件并发后会形成“文件并发 x 文件内并发”，429 压力也无法跨文件协调。

推荐目标不是继续调大某个数字，而是引入一个应用级请求调度器：

```text
多个文件任务
    -> 每个任务拆成有序 BatchJob
    -> 一个全局公平调度器
    -> 一个 Provider 级并发/速率限制器
    -> 一个共享请求执行池
    -> BatchOutcome 回到单一协调线程
    -> 按原索引写回、检查点保存、发布 UI 快照
```

初始参数建议：全局在途请求默认 2、硬上限 4；多个任务并存时每任务先分配 1 个槽位，其他任务没有可运行批次时，空闲槽位才借给单个大任务。

## 3. 当前实现与证据

### 3.1 实际调用链

```text
ConcurrentWindow._start_all()
  -> ConcurrentTranslationManager.start_all()
  -> _start_next_pending()
  -> start_task() 为一个文件创建 TranslatorEngine 和线程
  -> _run_task() 获取文件级 Semaphore
  -> TranslatorEngine.translate_bulk_mode()
  -> _translate() 为该文件创建 ThreadPoolExecutor
  -> BaseAPI.translate_stream_enhanced()
  -> 批次回调直接修改 TranslationTask
  -> 整个文件结束后保存
  -> _start_next_pending() 启动下一个文件
```

### 3.2 文件级队列严格串行

- `src/ui/concurrent_window.py:36` 创建管理器时没有传并发参数。
- `src/core/concurrent_manager.py:39` 的 `max_concurrent` 默认值为 1。
- `src/core/concurrent_manager.py:173` 的 `start_all()` 只调用一次 `_start_next_pending()`。
- `src/core/concurrent_manager.py:238` 只要发现任一任务为 `running` 就直接返回，随后只取第一个 `pending`。

因此队列中有 10 个小文件时，行为仍是 1 个完成后再开始下一个。

### 3.3 大批次让内部并发经常空转

- `src/config/translation_profile.py:21`：队列默认批次上限为 80 行。
- `src/config/translation_profile.py:23`：队列默认 input token 预算为 16,000。
- `src/config/translation_profile.py:25`：队列默认并发为 2。
- `src/core/translator.py:177`：只有 `translate_bulk_mode()` 才读取这些队列参数。
- `tests/test_translation_performance.py:161`：当前测试明确验证 45 行队列内容只生成 1 个大请求。

若一个文件只生成一个 Batch，`ThreadPoolExecutor(max_workers=2)` 也只能运行一个请求。

粗略耗时模型如下，其中 `L` 是平均单请求耗时，`C` 是全局请求并发：

```text
当前多个单批文件：T ~= 文件数 x L
目标多个单批文件：T ~= ceil(文件数 / C) x L
```

在没有限流、请求时长相近的理想场景下，`C=2` 才应接近 2 倍吞吐。

API 配置中的 `batch_config.max_workers` 不控制这条主路径。`BaseAPI.translate_stream_enhanced()` 直接执行请求，队列不会经过旧 `BatchProcessor`；调大该字段不能提升队列吞吐，也不应把它当作队列并发配置。

### 3.4 设置页与队列配置错位

- `src/ui/settings_window.py:262` 展示的“并发请求数”读取 `translation_concurrency`。
- `src/ui/settings_window.py:831` 保存的也是 `translation_concurrency`。
- `src/core/translator.py:198` 队列实际读取 `queue_translation_concurrency`。
- `queue_batch_lines` 和 `queue_batch_max_input_tokens` 同样没有 UI 入口。

这会造成“设置已经生效”的错误反馈。配置接线必须在性能调优前修复。

### 3.5 直接打开文件并发会放大资源和限流问题

- `src/core/concurrent_manager.py:125` 每次启动任务都创建新的 `TranslatorEngine`。
- `src/core/concurrent_manager.py:366` 任务结束只释放 Semaphore，没有关闭 Engine。
- `src/core/concurrent_manager.py:210` 移除任务时直接丢弃 Engine 引用，也没有关闭。
- `src/api/siliconflow_api.py:39` 每个 SiliconFlow API 实例还持有心跳线程。
- `src/api/base_api.py:396` 的 429 压力和推荐并发属于单个 API 实例。

长队列会重复支付客户端、连接和线程成本。若文件并发为 2、单文件并发也为 2，实际可能出现 4 个请求；若以后把两个值都调到 4，则会变成 16，而不是期望的 4。

### 3.6 现有状态和取消语义不足以承受真正并发

- `src/core/concurrent_manager.py:113` 在获得 Semaphore 前就把任务标记为 `running`。
- 等待槽位的线程会显示“翻译中 0%”，但并没有发出请求。
- 等待期间调用取消会执行 `engine.stop()`；真正进入 `_translate()` 后又会先 `reset()`，旧取消可能被清除。
- 任务没有 `attempt_id`。取消后立即重启时，旧线程迟到的进度或终态可能覆盖新运行。
- UI 点击暂停后立即显示 `paused`，但已经在途的请求仍可能完成并写回。
- 详情页只禁止编辑 `running` 任务；在途请求迟到时可能覆盖暂停后的人工编辑。
- `remove_task()` 只禁止移除 `running`，允许移除 `paused`；但暂停 Worker 仍可能持有文件级 Semaphore 并等待 `pause_event`。Engine 引用被删除后，该槽位可能再也无法恢复或释放。

### 3.7 队列窗口仍有跨线程 Tk 调用

- `src/ui/concurrent_window.py:135` 的 Manager 回调可能在工作线程中调用 `winfo_exists()` 和 `after()`。
- 同一窗口已经在 `src/ui/concurrent_window.py:129` 每 500 ms 轮询刷新，二者重复。
- 同步 UI 回调还可能阻塞批次结果线程，延迟补充下一批请求。

主编辑器已有事件邮箱和 Tk 事件泵，队列应复用同一原则：工作线程只发布不可变事件，Tk 主线程只消费快照。

### 3.8 中间结果缺少可靠检查点

批次完成时只更新内存中的 `target_lines`，通常要到整个文件完成后才保存。窗口关闭、进程退出或任务取消时，已成功的批次可能没有可靠落盘，下一次需要重复请求。

## 4. 目标与非目标

### 4.1 必须达到的目标

- 多个单批文件能够共享全局请求槽位并行执行。
- 单个大文件在没有其他任务竞争时能够借用全部空闲槽位。
- 任意时刻实际 HTTP 在途请求不超过一个明确的全局上限。
- 提高任务数不会把并发乘法放大。
- 429、`Retry-After`、RPM 和 TPM 限制作用于同一 Provider 运行时，而不是单个文件。
- 乱序完成不改变最终行顺序和失败索引。
- 等待槽位的任务可以零请求取消。
- 暂停确认后不再有旧批次写入；重启后旧 `attempt_id` 事件被忽略。
- 批次成功后生成可恢复检查点。
- 同一路径或同一 EPUB project 不会被两个活动任务并发写入。
- UI 线程和工作线程边界清晰。

### 4.2 本轮非目标

- 不把整个 Tk 应用改写成 `asyncio`。
- 不通过无上限线程或无上限 Future 队列换取短期跑分。
- 不同时重写图片翻译队列。图片任务的 GPU、OCR 和生成限额不同，应使用独立资源池。
- 不承诺并发越高越快。Provider 排队、token 生成速度和账号限额可能让 4 路慢于 2 路。
- 不用缓存掩盖调度问题。缓存属于单独优化项。

## 5. 推荐的目标架构

### 5.1 线程与所有权

```text
Tk 主线程
  | commands                         ^ immutable snapshots/events
  v                                  |
QueueTranslationCoordinator ----------
  - task registry
  - state machine
  - batch planner
  - fair ready ring
  - result apply/checkpoint scheduling
             |
             v
ProviderLimiter + global ThreadPoolExecutor(max_workers=hard_cap)
             |
             v
ProviderRuntime / persistent httpx.Client
```

所有权规则：

- Tk 主线程只发送命令和渲染 `QueueTaskSnapshot`。
- Coordinator 是任务状态、批次状态和写回顺序的唯一修改者。
- Worker 只接收不可变 `BatchJob`，返回不可变 `BatchOutcome`，不修改 `TranslationTask`，不调用 Tk。
- ProviderLimiter 是并发、速率和 cooldown 的唯一 owner。
- ProviderRuntime 管理客户端生命周期；配置实际变化时才替换。
- 保存线程只接收带 generation 的不可变快照。

### 5.2 建议的数据结构

```python
from dataclasses import dataclass
from enum import Enum


class QueueTaskState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    max_in_flight_requests: int = 2
    hard_request_cap: int = 4
    max_active_tasks: int = 4
    per_task_soft_limit: int = 1
    target_batch_input_tokens: int = 6000
    max_batch_input_tokens: int = 16000
    max_batch_lines: int = 80
    adaptive_concurrency: bool = True


@dataclass(frozen=True, slots=True)
class BatchJob:
    task_id: str
    attempt_id: str
    batch_id: int
    source_indices: tuple[int, ...]
    source_lines: tuple[str, ...]
    estimated_input_tokens: int
    run_context: "TranslationRunContext"


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    task_id: str
    attempt_id: str
    batch_id: int
    source_indices: tuple[int, ...]
    translated_lines: tuple[str, ...]
    failed_relative_indices: tuple[int, ...]
    retry_count: int
    rate_limited: bool
    retry_after_seconds: float | None
    request_seconds: float
    error_message: str | None = None
```

必要约束：

- `attempt_id` 每次开始/重试任务时重新生成。
- Coordinator 收到结果后先比较当前 `attempt_id`，不匹配就丢弃。
- `source_indices` 是最终写回依据，不依赖 Future 完成顺序。
- API Key 不进入 Job、日志、指标或 `repr`。

### 5.3 ProviderRuntime 的键

建议用以下非敏感字段区分运行时：

```text
(provider, normalized_base_url, model_name, credential_reference, config_version)
```

不要把明文 API Key 放入键或日志。

需要特别处理取消：当前 `BaseAPI.cancel_requests()` 会关闭当前客户端。如果客户端被多个任务共享，取消一个任务不能再调用这个全局方法，否则会中断其他任务。目标实现应把取消改成请求级 `cancel_event` 或请求句柄；在完成该改造前，先保持每任务客户端，但必须在终态关闭。

## 6. 全局公平调度策略

### 6.1 核心规则

1. 全局实际在途请求数不超过 `ProviderLimiter.current_limit`。
2. 第一轮按任务轮询，每个可运行任务最多取得 1 个槽位。
3. 若仍有空闲槽位且其他任务没有可运行批次，单个大任务可以借用剩余槽位。
4. 暂停请求后不再派发新批次；已有批次收割完成后才确认 `PAUSED`。
5. 取消时立即删除尚未提交的 Job，并使在途请求看到该任务的取消令牌。
6. 只保留有限数量的待执行 Future；建议最多为全局硬上限，不预提交整个书的所有批次。

这是一种 work-conserving 的公平策略：多文件时避免大文件占满所有槽位，只有一个文件时又不会把其他槽位闲置。

### 6.2 调度伪代码

```python
while not closed:
    drain_commands()
    drain_completed_outcomes()
    apply_outcomes_and_schedule_checkpoints()

    capacity = limiter.current_limit - len(in_flight)

    # 第一轮：每个 ready task 先取得一个机会。
    for task in ready_ring.round_robin():
        if capacity == 0:
            break
        if task.can_dispatch() and task.in_flight < policy.per_task_soft_limit:
            submit(task.pop_next_batch())
            capacity -= 1

    # 第二轮：没有其他竞争者时，把空闲槽位借给仍有工作的任务。
    while capacity > 0:
        task = ready_ring.next_borrower()
        if task is None:
            break
        submit(task.pop_next_batch())
        capacity -= 1

    wait_for_command_or_completion()
```

第一版使用 round-robin 即可。后续只有在批次 token 大小仍差异明显时，再考虑按估算 token 成本做 deficit round-robin，不要一开始引入复杂优先队列。

### 6.3 为什么不保留两层独立线程池

“每文件一个线程池 + Manager 文件并发”存在以下问题：

- 总并发是两个配置值的乘积；
- 多个线程池无法公平分配全局槽位；
- 每个 API 实例独立判断 429，容易同时重试；
- 暂停/取消需要跨两级队列清理 Future；
- 长队列创建过多线程和客户端。

最终结构只保留一个请求执行池。文件任务是调度维度，不再各自拥有线程池。

## 7. 批次规划优化

### 7.1 将 80 行/16k token 改成硬上限，而不是默认目标

现有大批次减少了请求数量，但也会：

- 让小文件只有一个请求，无法并发；
- 拉长首次可见结果时间；
- 增大行对齐失败和输出截断的影响范围；
- 让一个慢请求占用槽位很久，公平性变差。

建议区分目标和硬上限：

| 参数 | 初始建议 | 说明 |
| --- | ---: | --- |
| `target_batch_input_tokens` | 4,000-6,000 | 调度和延迟目标，需用真实 API 调优 |
| `max_batch_input_tokens` | 16,000 | 上下文安全硬上限，继续受模型窗口约束 |
| `max_batch_lines` | 80 | 防止极短行无限聚合的硬上限 |
| `min_batch_input_tokens` | 1,000-2,000 | 避免为了凑并发制造大量过小请求 |

BatchPlanner 仍必须计入：system prompt、术语表、行号标记、输出 reserve 和模型 context window。当前规划主要约束输入预算；实现时还应估算输出预算，并解析 SSE 的 `finish_reason`/usage。若 `finish_reason=length`、输出截断或格式校验失败，应二分该批次，只重试失败子集。

### 7.2 维持足够的 ready work

规划目标是通常至少准备 `2 x current_limit` 个大小相近的批次，但不能为了达到数量突破 `min_batch_input_tokens` 或章节/语义边界。

参考策略：

```python
desired_waves = 2
desired_batches = max(1, current_limit * desired_waves)
adaptive_target = ceil(total_estimated_tokens / desired_batches)
target = clamp(
    adaptive_target,
    min_batch_input_tokens,
    configured_target_batch_input_tokens,
)
```

多个小文件本身就能提供 ready work，不需要把每个小文件强拆成多个请求。只有单个大文件时，才需要保证 Planner 能产生足够批次供全局池使用。

### 7.3 根据错误类型调整正确的参数

- 429/RPM 压力：降低提交速率或并发，不先缩小批次。
- context length、输出截断：降低 token 硬预算。
- 行号对齐/空译文上升：降低批次目标大小，并只重试失败行。
- TTFT P95 上升但 429 不变：检查 Provider 排队和请求大小。
- 请求数翻倍但 token/s 不变：回退并发，说明 Provider 端已经饱和。

不要在一次提交中同时改批次大小、并发和提示协议，否则无法判断回归来源。

## 8. 全局限流与自适应并发

### 8.1 限流作用域

Limiter 应按 ProviderRuntime 键共享，至少控制：

- 最大在途请求；
- 可选 RPM token bucket；
- 可选 TPM token bucket；
- `Retry-After` 对应的全局 `blocked_until`；
- 429/超时/成功窗口；
- 当前自适应并发值。

等待 Limiter 时不得持有任务注册表锁、进度锁或 Tk 相关锁。

### 8.2 推荐的 AIMD 策略

初始值：

```text
configured_max = 2
hard_cap = 4
current_limit = min(2, configured_max)
```

降级：

- 任一请求收到 429：立即设置 `current_limit = max(1, floor(current_limit / 2))`；
- 存在 `Retry-After`：整个 Provider 在该时间前不提交新请求；
- 无 `Retry-After`：进入 15-30 秒 cooldown，并使用指数退避加 10%-20% jitter；
- 已在途请求允许结束，不通过关闭共享 Client 粗暴取消。

`Retry-After` 需要同时支持秒数和 HTTP-date。退避和 jitter 可复用 `src/application/error_handling.py` 中已有的 RetryPolicy 思路，避免维护第二套不一致实现。

恢复：

- cooldown 结束后保持当前值；
- 连续 20 个成功请求，且无 429、超时率低、P95 未明显恶化时，增加 1；
- 每个恢复窗口最多增加 1，不能一次跳回上限。

`src/api/base_api.py` 当前已有局部 `_rate_limit_pressure`，可保留为请求层指标，但调度决策应上移到共享 Limiter。最好让 API 层在首次收到 429 时立即调用 Limiter，而不是等所有内部重试结束后才上报。

### 8.3 避免重试风暴

多个请求同时 429 时，不能让每个 Worker 各自计算相同退避后同时醒来。Limiter 应统一维护 `blocked_until`，Worker 只上报结果，由调度器在 cooldown 后按新并发逐步补充请求。

## 9. 状态机、暂停、取消与重试

### 9.1 状态转换

```text
PENDING -> READY -> RUNNING -> COMPLETED
                   |    |
                   |    +-> PARTIAL / ERROR
                   |
                   +-> PAUSE_REQUESTED -> PAUSED -> READY
                   |
                   +-> CANCELLED
```

规则：

- `READY` 表示已计划、正在等请求槽位；不要提前显示 `RUNNING`。
- 首个 Job 真正提交后才进入 `RUNNING`。
- 点击暂停先进入 `PAUSE_REQUESTED`，停止新提交；`in_flight == 0` 后才进入 `PAUSED`。
- 只有 `PAUSED` 才允许用户编辑，避免在途结果覆盖。
- `CANCELLED` 后旧 attempt 的所有 Outcome 都被丢弃。
- `PARTIAL` 重试只为失败或仍为空的 source index 建 Job，不重复成功行。

### 9.2 取消级别

建议区分：

- 软暂停：不提交新请求，等待在途批次完成并保存。
- 软取消：删除未提交 Job，在途批次完成后丢弃或按产品规则保存已确认结果。
- 强取消：请求级关闭当前 Response/stream，但不能关闭共享 Client。

第一版可以先可靠实现软暂停和软取消，再做强取消。UI 文案必须与真实语义一致。

### 9.3 防止旧回调覆盖

Coordinator 应对每个命令和 Outcome 检查：

```python
if outcome.attempt_id != task.current_attempt_id:
    return  # stale outcome
```

仅比较 `task_id` 不够，因为同一任务可以取消后重新开始。

## 10. 结果写回与检查点

### 10.1 单线程应用结果

Worker 不直接修改 `target_lines`。Coordinator 收到 BatchOutcome 后：

1. 校验 `attempt_id`；
2. 校验返回长度和相对失败索引；
3. 通过 `source_indices` 写回固定位置；
4. 成功译文非空时才覆盖；
5. 更新完成数、失败数和指标；
6. 生成不可变保存快照；
7. 发布不可变 UI 快照。

这样即使 Batch 2 比 Batch 1 先完成，输出仍保持源文档顺序。

### 10.2 合并检查点保存

每个成功 Batch 都应把任务标记为 dirty，但不要让多个 Worker 并发写文件。建议：

- 每任务最多一个保存 Future；
- 500-1000 ms debounce；
- 保存进行中又有新结果时只提升 generation，完成后再补一次最新快照；
- 使用现有原子写能力；
- 取消/关闭前触发最后一次关键保存并设置合理超时；
- 保存失败保留 dirty 和错误状态，不能假装任务已完整完成。

Coordinator 还应按规范化源路径或 EPUB project ID 做去重/独占写锁。同一输入被重复添加时，可以拒绝第二个活动任务或复用已有任务，但不能让两个任务同时写同一个 `_译文.txt` 或 mapping 目录。

TXT 和 EPUB 可以复用相同的 generation 协调模式，但具体 Repository 保持独立。

## 11. UI 与配置改造

### 11.1 配置命名

建议把用户真正关心的参数明确为：

```json
{
  "queue_max_in_flight_requests": 2,
  "queue_max_active_tasks": 4,
  "queue_target_batch_input_tokens": 6000,
  "queue_max_batch_input_tokens": 16000,
  "queue_max_batch_lines": 80,
  "queue_adaptive_concurrency": true,
  "queue_rpm_limit": 0,
  "queue_tpm_limit": 0
}
```

兼容迁移：

| 旧字段 | 新字段 |
| --- | --- |
| `queue_translation_concurrency` | `queue_max_in_flight_requests` |
| `queue_batch_max_input_tokens` | `queue_max_batch_input_tokens` |
| `queue_batch_lines` | `queue_max_batch_lines` |

`translation_concurrency` 继续只代表主编辑器交互翻译，设置页必须明确区分“交互翻译并发”和“队列全局 API 并发”。

### 11.2 推荐 UI 信息

队列窗口至少展示：

- 任务状态：等待、运行、暂停中、已暂停、重试等待、保存中、完成、部分完成、错误；
- 已完成行/总行数，而不只显示四舍五入百分比；
- 当前活动请求数/全局上限；
- 最近 30-60 秒吞吐（行/分钟或 token/秒）；
- 429/重试数量和 cooldown 剩余时间；
- 基于最近完成批次的 ETA，样本不足时不显示虚假 ETA。

队列批次可以继续不向表格发送逐 token 内容，但不能长时间只显示 0%。单批长请求至少显示“等待首字/生成中/重试等待/保存中”和已耗时。

### 11.3 UI 线程边界

二选一即可：

1. 复用主编辑器的事件邮箱和 Tk 事件泵，以 `task_id` 合并高频进度；或
2. 保留 200-500 ms 主线程轮询，但 Manager 只返回不可变 Snapshot，并删除工作线程中的 Tk 回调。

队列场景不需要同时保留两套刷新机制。

## 12. 分阶段实施计划

### 阶段 0：基线与回归护栏

新增：

- `tests/test_queue_scheduler.py`
- `tests/test_queue_lifecycle.py`
- 可选 `tools/benchmark_queue_translation.py`

性能基准应优先使用本地可配置 SSE Fake HTTP 服务，而不只是 mock Python 方法。这样可以覆盖 `httpx` 连接池、流读取、`Retry-After`、超时和断流；公网 Provider 只做非门禁 smoke。

先记录：

- 总耗时、首个文件完成时间；
- 实际最大在途请求；
- 每请求 input tokens、TTFT、总时长；
- retries、429、超时、行对齐失败；
- Engine/Client 创建数、任务结束后活动线程数。

不要先改默认值再补基线。

### 阶段 1：低风险过渡方案

目标是先让多个小文件获得收益，同时不进行大规模抽象重写：

1. 修复设置页接线，明确队列参数。
2. 把 `_start_next_pending()` 改成 `_fill_available_task_slots()`，最多启动 2 个文件任务。
3. 多任务并存时暂时把单任务内部并发限制为 1，避免 `2 x 2`；只有一个任务时保留内部并发 2。
4. Engine 在完成、取消、错误、移除和重启覆盖前都必须 `close()`。
5. 增加 `attempt_id`，等待槽位期间可取消，取得槽位后再标记 running。
6. 删除工作线程 Tk 调用，统一用主线程轮询 Snapshot。

限制：最后只剩一个原本以内部并发 1 启动的任务时，可能不能动态借用第二个槽位。这是过渡方案的已知尾部利用率问题，不应长期保留。

### 阶段 2：共享 BatchJob 调度器

1. 从 `TranslatorEngine._translate()` 抽出纯 BatchPlanner。
2. 从 `_translate_batch()` 抽出接收 Job/返回 Outcome 的 Worker。
3. Manager 改为一个 Coordinator 和一个全局 Executor。
4. 实现 round-robin 第一轮和空闲槽位借用。
5. Worker 不再直接修改任务或调用回调。
6. 删除每任务 ThreadPoolExecutor 和文件级 Semaphore。

完成本阶段后，单大文件和多小文件都能使用同一全局并发预算。

### 阶段 3：共享 ProviderLimiter 与 ProviderRuntime

1. 先把取消改为请求级，避免关闭共享 Client。
2. 引入 ProviderRuntime 键和持久 Client。
3. 把 429、`Retry-After`、RPM/TPM 和 AIMD 上移到共享 Limiter。
4. 配置变化时停止派发、等待/取消旧请求，再替换 Runtime。
5. 删除每文件局部并发推荐作为最终调度决策的职责。

### 阶段 4：检查点、指标和 UI

1. 批次成功后 generation/debounce 原子保存。
2. 发布任务 Snapshot 和聚合指标。
3. 展示活动槽位、吞吐、重试等待和 ETA。
4. 用真实 Provider 做 1/2/4 并发对照，决定默认值，不凭经验把默认值升到 4。

## 13. 必须新增的测试

### 13.1 调度与公平性

- `test_start_all_fills_global_request_slots`
- `test_global_request_limit_is_not_multiplied_by_task_count`
- `test_multiple_single_batch_files_run_concurrently`
- `test_short_task_is_not_starved_by_long_task`
- `test_single_long_task_borrows_idle_slots`
- `test_no_more_than_hard_cap_futures_are_submitted`
- `test_out_of_order_batches_write_back_in_source_order`

Fake API 使用 Barrier 和受锁保护的 `active/max_active` 计数，优先断言并发事实，不只依赖脆弱的墙钟时间。

### 13.2 限流与重试

- `test_any_task_429_reduces_shared_provider_limit`
- `test_retry_after_blocks_new_requests_globally`
- `test_success_window_recovers_concurrency_one_step_at_a_time`
- `test_concurrent_429_does_not_create_retry_stampede`
- `test_rpm_and_tpm_waits_are_cancelable`

### 13.3 生命周期与状态机

- `test_waiting_task_can_cancel_without_api_call`
- `test_pause_stops_new_dispatch_and_confirms_after_inflight_drains`
- `test_stale_attempt_outcome_cannot_overwrite_restart`
- `test_cancel_one_task_does_not_close_shared_client_for_other_tasks`
- `test_engine_or_runtime_is_closed_on_terminal_remove_and_window_close`
- `test_restart_closes_replaced_engine`
- `test_removing_paused_task_releases_worker_and_slot`
- `test_duplicate_source_path_cannot_write_concurrently`

### 13.4 UI 与持久化

- `test_worker_thread_never_calls_tk_api`
- `test_queue_setting_controls_actual_queue_concurrency`
- `test_progress_events_are_coalesced_per_task`
- `test_batch_checkpoint_survives_cancel_or_process_restart`
- `test_checkpoint_writer_is_single_flight`
- `test_save_failure_keeps_task_dirty_and_not_completed`

### 13.5 性能基准场景

| 场景 | 数据 | 目的 |
| --- | --- | --- |
| 多个小文件 | 8 个文件，每个 1 Batch，Fake API 固定 200 ms | 验证文件级并发 |
| 单个大文件 | 40 个等大小 Batch | 验证空闲槽位借用 |
| 长短混合 | 1 个 40 Batch + 6 个 1 Batch | 验证公平性和首个完成时间 |
| 429 注入 | 第 3、4 个请求返回 429 | 验证全局降速和无重试风暴 |
| 乱序完成 | 奇偶 Batch 使用不同延迟 | 验证行顺序和失败索引 |
| 取消/重启 | 运行中取消后立即重新开始 | 验证 attempt 隔离 |
| 生命周期 | 连续完成并移除 100 个单批任务 | 验证 Client、心跳线程和内存回落 |

## 14. 验收指标

### 14.1 确定性正确性

- `max_active_http_requests <= configured_global_limit <= hard_cap` 始终成立。
- 任务数增加不会改变全局最大在途请求数。
- 所有成功行与 source index 一一对应，乱序完成不改变最终输出。
- 等待任务取消后的 API 调用数为 0。
- `PAUSED` 确认后没有旧 attempt 写回。
- 一个任务取消不会影响其他任务的客户端或请求。
- 完成、移除、重试 100 个任务后，客户端和心跳线程数量不持续增长。
- 工作线程调用 Tk API 的次数为 0。

### 14.2 性能目标

在固定延迟 Fake API 中：

- 多小文件场景并发 2 相对串行的吞吐提升至少 1.75 倍；
- 多小文件场景并发 4 相对串行的吞吐提升至少 3.0 倍，同时 `max_active == 4`；
- 单大文件场景并发 2 相对串行的吞吐提升至少 1.6 倍；
- 长短混合场景中，小任务应在有限轮次内开始，不等待长任务全部完成。

在真实 API 中，以并发 1 为基线：

- 默认并发 2 的队列总耗时目标下降至少 30%，理想达到 40%；
- 429 比例不高于 2%，且不能显著高于基线；
- 行对齐失败率和空译文率不得高于基线；
- 若并发 4 相对并发 2 提升低于 10%，默认保持 2；
- 首个文件完成时间和队列 P95 任务等待时间必须一并记录，不能只看总吞吐。

真实 API 指标受 Provider 和账号套餐影响，阈值用于选择默认策略，不应写成无条件 CI 断言。

## 15. 不建议的做法

- 只把 `max_concurrent` 从 1 改成 4。
- 同时把文件并发和 `queue_translation_concurrency` 都改成 4。
- 继续使用 80 行/16k token 作为所有请求的固定目标。
- 为每个文件创建一个长期 ThreadPoolExecutor。
- 让每个请求单独处理 429 并在相同时间一起重试。
- 通过关闭共享 HTTP Client 取消一个任务。
- 在 Worker 中修改 `TranslationTask`、保存文件或调用 Tk。
- 任务尚在等 Semaphore 时就显示 `running`。
- 暂停后立即允许编辑，但不等待在途请求结束。
- 只用总耗时判断性能，不记录错误率、429、行对齐和首个完成时间。

## 16. 建议提交拆分

1. `test(queue): add deterministic multi-task scheduler baseline`
2. `fix(queue): wire queue-specific concurrency settings`
3. `fix(queue): close engines and isolate attempts`
4. `refactor(queue): introduce immutable batch jobs and outcomes`
5. `feat(queue): add global fair request scheduler`
6. `feat(queue): add shared provider limiter and adaptive concurrency`
7. `feat(queue): checkpoint completed batches`
8. `feat(queue-ui): render immutable queue snapshots and metrics`
9. `perf(queue): tune batch targets with recorded provider benchmarks`

每个提交只改变一个主要变量，便于比较吞吐、正确率和资源曲线，也便于回滚。

## 17. 完成定义

只有同时满足以下条件，才能认为队列并发优化完成：

- 多文件和单大文件都能使用全局请求槽位；
- 并发只有一个清晰 owner，不存在两层乘法；
- 用户修改的队列配置确实控制队列行为；
- 429 和 `Retry-After` 在 Provider 范围统一生效；
- 暂停、取消、重启和旧回调有确定状态语义；
- 中间成功结果可恢复；
- 客户端、线程和 Future 数量有界且能释放；
- UI 无跨线程 Tk 调用；
- 确定性测试、故障注入测试和真实 API 基准均有记录；
- 默认值由 1/2/4 并发实测结果决定，而不是简单选择最大值。
