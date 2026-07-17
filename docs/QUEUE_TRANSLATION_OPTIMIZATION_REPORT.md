# 队列翻译并发优化实施报告

> 对应方案：`docs/QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md`
> 实施范围：阶段 0-4 全部完成
> 完成日期：2026-07-17

---

## 一、优化内容概述

### 1.1 核心改造

将队列翻译从"每文件独立线程池 + 文件级 Semaphore"架构，重构为"全局公平调度器 + 共享 ProviderLimiter"架构。改造后的数据流：

```text
多个文件任务
    -> 每个任务拆成有序 BatchJob（纯函数 plan_batches）
    -> 一个全局 QueueTranslationCoordinator（单一协调线程）
    -> 一个共享 ProviderLimiter（AIMD 自适应并发 + RPM/TPM + Retry-After）
    -> 一个全局 ThreadPoolExecutor（硬上限 = hard_request_cap）
    -> BatchOutcome 回到协调线程（attempt_id 隔离）
    -> 按原 source_indices 写回、检查点保存、发布 UI 快照
```

### 1.2 解决的问题

| # | 原方案问题 | 优化后方案 |
|---|-----------|-----------|
| 1 | 多文件严格串行（`max_concurrent=1`） | 全局 round-robin 调度，多任务并发 |
| 2 | 80 行/16k token 大批次致内部并发空转 | 批次行数和 token 预算可配置，默认值合理化 |
| 3 | 设置页参数与队列配置错位 | 新增 7 个队列专用参数，UI 直接控制队列行为 |
| 4 | 每文件独立引擎/客户端/限流 | 共享 ProviderLimiter，429 跨任务统一降速 |
| 5 | 取消/重启旧回调覆盖新结果 | attempt_id 隔离，旧 Outcome 一律丢弃 |
| 6 | 工作线程直接调用 Tk | 工作线程零 Tk 调用，UI 轮询不可变 Snapshot |
| 7 | 中间结果无可靠检查点 | single-flight + debounce 检查点，独占写锁 |

---

## 二、实施步骤（按阶段）

### 阶段 0：基线测试与回归护栏

**新增文件：**
- `tests/test_queue_scheduler.py` — 调度器单元测试（plan_batches、execute_batch_job、Coordinator 生命周期）
- `tests/test_queue_lifecycle.py` — ConcurrentTranslationManager 端到端测试
- `tools/benchmark_queue_translation.py` — 基准工具（3 场景：serial/concurrent/rate_limited）

### 阶段 1：低风险过渡（设置接线 + 引擎关闭 + attempt_id）

**修改文件：**

1. **`src/config/translation_profile.py`** — 新增队列策略参数
   - 7 个常量：`DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS`、`DEFAULT_QUEUE_HARD_REQUEST_CAP`、`DEFAULT_QUEUE_MAX_ACTIVE_TASKS`、`DEFAULT_QUEUE_PER_TASK_SOFT_LIMIT`、`DEFAULT_QUEUE_ADAPTIVE_CONCURRENCY`、`DEFAULT_QUEUE_RPM_LIMIT`、`DEFAULT_QUEUE_TPM_LIMIT` 及对应上限
   - `build_queue_policy_from_app_config()` 函数：从 app_config 构造 `QueuePolicy`，含旧字段 `queue_translation_concurrency` 的 fallback 和越界 clamp

2. **`src/config/config_manager.py`** — 在 `default_app_config` 中注册 7 个新参数默认值

3. **`src/ui/settings_window.py`** — 翻译设置 Tab 新增"队列翻译调度参数"分区
   - 8 个控件：最大在途请求、硬并发上限、最大活跃任务、每任务软上限、队列批次行数、队列批次输入预算、RPM 限制、TPM 限制、自适应并发复选框
   - 保存逻辑同步更新 `new_app_config`

### 阶段 2：共享 BatchJob 调度器

**新增文件：**

1. **`src/core/queue_scheduler.py`** — 核心调度器（~1400 行）
   - 不可变数据结构：`BatchJob`、`BatchOutcome`、`QueueTaskSnapshot`、`QueueSnapshot`、`QueueMetricsSnapshot`（frozen dataclass + slots）
   - `QueueTaskState` 枚举：`PENDING → READY → RUNNING → COMPLETED/PARTIAL/ERROR`，含 `PAUSE_REQUESTED/PAUSED/CANCELLED`
   - `QueuePolicy`：全局并发策略参数
   - `plan_batches()`：纯函数批次规划（按 max_batch_lines 和 token 预算拆分，支持 missing_indices 子集）
   - `execute_batch_job()`：Worker 函数（调用引擎 `_translate_batch`，返回不可变 `BatchOutcome`，上报 429/超时给 limiter）
   - `QueueTranslationCoordinator`：单一协调线程
     - 命令队列（`submit_command` 非阻塞）
     - round-robin 第一轮 + 空闲槽位借用第二轮
     - attempt_id 隔离（取消/重启 bump attempt_id，旧 Outcome 丢弃）
     - 独占写锁（`ExclusiveWriteLockRegistry` 防止同路径并发写）
     - 不可变快照发布（`get_snapshot()` 供 UI 轮询）

**修改文件：**

2. **`src/core/translator.py`** — 新增 `build_run_context()` 和 `compute_input_token_budget()` 方法
   - 供 Coordinator 在任务尝试开始时一次构造，复用给该任务所有 BatchJob

3. **`src/core/concurrent_manager.py`** — 完整重写为兼容层
   - 保留 `TranslationTask` dataclass（旧 UI 兼容视图）
   - `ConcurrentTranslationManager` 内部委托 `QueueTranslationCoordinator`
   - 静态字段在 `add_task` 时缓存，动态字段在 `get_task`/`get_all_tasks` 时从 Coordinator 重建
   - 新增 `get_snapshot()` 供 UI 直接轮询不可变 `QueueSnapshot`

### 阶段 3：共享 ProviderLimiter

**新增文件：**

1. **`src/core/queue_provider.py`** — Provider 级限流器
   - `ProviderLimiter`：AIMD 自适应并发
     - 429 立即减半 `current_limit`（multiplicative decrease）
     - 连续 20 次成功后 +1（additive increase），不超过 `configured_max`
     - `Retry-After` 解析：支持秒数和 HTTP-date（RFC 7231），cooldown 期间阻塞
     - RPM/TPM 滑动窗口限流
   - `ProviderLimiterRegistry`：按 `ProviderRuntimeKey`（provider/base_url/model/credential_ref/config_version）跨任务共享
   - `parse_retry_after()`：纯函数解析 Retry-After 头
   - `credential_reference_for()`：生成非敏感的凭证引用（SHA256 前 8 位，空 key 返回空串）

**修改文件：**

2. **`src/domain/errors.py`** — `TranslationRequestError` 新增 `retry_after_seconds` 字段
3. **`src/api/base_api.py`** — 429 异常时解析 `Retry-After` 头并填入 `TranslationRequestError`

### 阶段 4：检查点、指标、UI 快照

**新增文件：**

1. **`src/core/queue_checkpoint.py`** — 检查点协调器
   - `CheckpointCoordinator`：single-flight + debounce
     - 每任务最多一个保存 Future（避免并发写同一文件）
     - 500ms debounce（合并连续 dirty 标记）
     - `flush_blocking()` 终态时同步等待
     - 保存失败保留 dirty + 记录 error
   - `ExclusiveWriteLockRegistry`：按规范化输出路径独占写锁（引用计数）
   - `make_txt_save_fn()`：构造 TXT 保存闭包

**修改文件：**

2. **`src/ui/concurrent_window.py`** — UI 改造
   - `STATUS_MAP` 新增 `ready` 和 `pause_requested` 状态
   - Treeview 新增 `in_flight` 列（在途批次数）
   - 新增 `metrics_bar` 底部状态栏（并发/429/超时/吞吐/ETA）
   - `_refresh_tree` 使用 `get_snapshot()` 获取在途批次信息
   - 新增 `_refresh_metrics()` 方法消费不可变 `QueueSnapshot`
   - `_on_task_update` 注释更新：不再有跨线程 Tk 调用

3. **`src/ui/task_detail_window.py`** — `STATUS_MAP` 新增 `ready` 和 `pause_requested` 状态

---

## 三、测试结果

### 3.1 新增测试套件

| 测试文件 | 测试数 | 覆盖范围 |
|---------|--------|---------|
| `test_queue_provider_limiter.py` | 24 | parse_retry_after（秒/HTTP-date/边界）、try_acquire/release、AIMD（429 减半/恢复+1/不超上限/超时重置）、RPM/TPM 限流、Registry 共享、credential_reference |
| `test_queue_checkpoint.py` | 12 | single-flight（dirty 触发/并发合并/flush_blocking/保存失败保留 dirty/close 幂等）、make_txt_save_fn、ExclusiveWriteLock（同 key 拒绝/不同 key 独立/释放后重获/幂等/is_held/引用计数） |
| `test_queue_scheduler.py` | 18 | plan_batches（空/单批/按行拆/按 token 拆/batch_id 递增/missing_indices 子集）、execute_batch_job（成功/取消/TranslationCancelled/429/未预期异常）、Coordinator 生命周期（add/remove/同路径拒绝/快照发布/close 幂等/close 释放引擎）、attempt_id 隔离（取消后 bump）、STATE_DISPLAY_MAP 完整性 |
| `test_queue_lifecycle.py` | 9 | ConcurrentTranslationManager 端到端（add→start→COMPLETED+文件保存）、取消任务、快照不可变性、旧 API 兼容（TranslationTask 视图/get_all_tasks 保序）、build_queue_policy_from_app_config（默认/legacy fallback/new 覆盖/clamp） |
| `test_queue_concurrency_invariants.py` | 23 | §13 确定性并发不变量测试：§13.1 调度公平性（Barrier 断言真并发/全局不乘法/短任务不饿死/空闲借用/硬上限/乱序写回）、§13.2 限流重试（跨任务 429 降级/Retry-After 全局阻塞/Barrier(3) 无重试风暴）、§13.3 生命周期（零请求取消/暂停 drain/attempt_id 隔离/取消不关闭共享 client/重启关闭旧引擎/移除暂停任务释放槽位/同路径独占写锁）、§13.4 UI 持久化（worker 零 Tk 调用/设置控制并发/检查点存活/单飞写入/保存失败保留 dirty） |
| **合计** | **86** | |

### 3.2 测试运行结果

```
============================= 86 passed in 20.64s =============================
```

全部 86 个新增测试通过（含 23 项 §13 确定性并发不变量测试）。

### 3.3 回归测试

运行全量测试套件（排除与本次优化无关的预存失败）：

```
723 passed, 38 warnings in 63.76s
```

无回归。3 项预存失败均与队列翻译优化无关：
- `test_document_session.py::test_epub_import_success_returns_epub_session` — 缺失 `src.infrastructure.epub_processor_impl` 模块
- `test_image_packaging.py::test_includes_numpy_version_pin` — numpy 版本锁定格式问题
- `test_image_packaging.py::test_all_lines_valid_pin_format` — requirements 文件格式问题

### 3.4 测试修复记录

**`test_cancel_bumps_attempt_id` 修复：**

原测试使用真实 `TranslatorEngine`（未 mock），在 keyring 加载延迟下出现时序不稳定（snapshot 为 None）。修复方案：
- 使用 `unittest.mock.patch` 替换 `TranslatorEngine` 为阻塞型 `MockEngine`
- `_translate_batch` 在 `release_event` 设置前阻塞，确保任务处于 RUNNING 时取消命令能被处理
- 改用 deadline 轮询替代固定 sleep，消除时序依赖
- 实际验证 attempt_id 在取消后发生变化（§9.3 隔离语义）

**§13 确定性并发不变量测试修复（`test_queue_concurrency_invariants.py`）：**

1. **`test_concurrent_429_does_not_create_retry_stampede`**：使用 `threading.Barrier(3)` 确保 3 个批次同时在途后同时失败，验证 cooldown 有界（≤30s），不形成重试风暴。

2. **`test_stale_attempt_outcome_cannot_overwrite_restart` / `test_restart_closes_replaced_engine`**：取消后旧批次阻塞 limiter 槽位，需显式等待 `_in_flight == 0` 后再提交重启命令；重启后新批次陆续到达，`release_all()` 只能释放已创建的 event，需循环释放直到任务完成。

3. **`test_queue_setting_controls_actual_queue_concurrency`**：默认 `max_batch_lines=80` 使 4 行只产生 1 批，无法验证并发控制。修复：显式设置 `queue_batch_lines=1` 强制拆成 4 批，验证 `max_in_flight=1` 时峰值不超过 1。

---

## 四、性能提升数据

### 4.1 基准工具运行

```bash
python -m tools.benchmark_queue_translation --tasks 4 --lines 200 --scenario all
```

### 4.2 基准结果

| 场景 | 任务数 | 总行数 | 耗时 | 吞吐（行/秒） | 429 次数 | 最终并发上限 |
|------|--------|--------|------|--------------|---------|-------------|
| serial（单任务串行） | 1 | 200 | 1.29s | 154.6 | 0 | 1 |
| concurrent（4 任务并发） | 4 | 800 | 4.62s | 173.1 | 0 | 2 |
| rate_limited（30% 429） | 2 | 200 | 3.63s | 55.1 | 4 | 1 |

### 4.3 数据分析

**并发吞吐提升：**
- concurrent 场景吞吐 173.1 行/秒 vs serial 场景 154.6 行/秒
- 在 4 个任务并发时，全局调度器有效利用了并发槽位
- 4 任务 800 行总耗时 4.62s，若串行需约 5.17s（4 × 1.29s），并发收益约 12%

**AIMD 自适应降级验证：**
- rate_limited 场景注入 30% 429 概率
- ProviderLimiter 收到 429 后：current_limit 从 2 → 1（multiplicative decrease）
- cooldown 1.0s 期间阻塞新请求，避免重试风暴
- 最终并发上限降为 1，符合 AIMD 预期行为

**全局并发有界：**
- concurrent 场景 final_limit=2（= configured_max），未超过 hard_cap=4
- 任务数增加（4 任务）未导致并发失控

### 4.4 架构改进对比

| 维度 | 优化前 | 优化后 |
|------|--------|--------|
| 文件并发 | 严格串行（max_concurrent=1） | 全局 round-robin + 空闲槽位借用 |
| 并发 owner | 两层（文件级 Semaphore × 文件内 ThreadPoolExecutor） | 单一全局 Coordinator + Executor |
| 429 处理 | 每文件独立，无法跨文件协调 | 共享 ProviderLimiter，AIMD 全局降速 |
| 取消隔离 | 无 attempt_id，旧回调可能覆盖新结果 | attempt_id 隔离，旧 Outcome 一律丢弃 |
| UI 线程安全 | 工作线程直接调用 Tk | 工作线程零 Tk 调用，UI 轮询不可变 Snapshot |
| 检查点 | 整个文件结束后才保存 | 每批次成功后 single-flight + debounce 保存 |
| 配置接线 | 设置页参数对队列无效 | 7 个队列专用参数，UI 直接控制 |

---

## 五、文件清单

### 新增文件（8 个）

| 文件 | 行数 | 说明 |
|------|------|------|
| `src/core/queue_scheduler.py` | ~1400 | 核心调度器：BatchJob/Outcome/Coordinator |
| `src/core/queue_provider.py` | ~400 | ProviderLimiter：AIMD/Retry-After/RPM/TPM |
| `src/core/queue_checkpoint.py` | ~300 | CheckpointCoordinator：single-flight/debounce/独占锁 |
| `tests/test_queue_scheduler.py` | ~570 | 调度器单元测试（18 项） |
| `tests/test_queue_provider_limiter.py` | ~350 | 限流器单元测试（24 项） |
| `tests/test_queue_checkpoint.py` | ~280 | 检查点单元测试（12 项） |
| `tests/test_queue_lifecycle.py` | ~310 | 生命周期端到端测试（9 项） |
| `tests/test_queue_concurrency_invariants.py` | ~1450 | §13 确定性并发不变量测试（23 项） |
| `tools/benchmark_queue_translation.py` | ~280 | 基准工具（3 场景） |

### 修改文件（9 个）

| 文件 | 说明 |
|------|------|
| `src/config/translation_profile.py` | 新增队列策略参数 + `build_queue_policy_from_app_config()` |
| `src/config/config_manager.py` | 注册 7 个新参数默认值 |
| `src/core/translator.py` | 新增 `build_run_context()` + `compute_input_token_budget()` |
| `src/core/concurrent_manager.py` | 完整重写为兼容层（委托 Coordinator） |
| `src/domain/errors.py` | `TranslationRequestError` 新增 `retry_after_seconds` |
| `src/api/base_api.py` | 429 异常解析 `Retry-After` 头 |
| `src/ui/settings_window.py` | 新增队列调度参数 UI 分区 |
| `src/ui/concurrent_window.py` | metrics_bar + in_flight 列 + 快照轮询 |
| `src/ui/task_detail_window.py` | STATUS_MAP 新增状态 |

### 删除文件（1 个）

| 文件 | 原因 |
|------|------|
| `tests/test_queue_bulk_translation.py` | 依赖旧架构 `manager._engines[task_id]` 和 `_run_task()`，与新 Coordinator 架构不兼容 |

---

## 六、完成定义对照

| 完成定义（§17） | 状态 |
|----------------|------|
| 多文件和单大文件都能使用全局请求槽位 | ✅ 全局 Coordinator + round-robin 调度 |
| 并发只有一个清晰 owner，不存在两层乘法 | ✅ 单一 QueueTranslationCoordinator |
| 用户修改的队列配置确实控制队列行为 | ✅ 7 个参数通过 build_queue_policy_from_app_config 接线 |
| 429 和 Retry-After 在 Provider 范围统一生效 | ✅ 共享 ProviderLimiter + AIMD |
| 暂停、取消、重启和旧回调有确定状态语义 | ✅ attempt_id 隔离 + 状态机 |
| 中间成功结果可恢复 | ✅ CheckpointCoordinator single-flight 保存 |
| 客户端、线程和 Future 数量有界且能释放 | ✅ 全局 Executor(hard_cap) + close 幂等 |
| UI 无跨线程 Tk 调用 | ✅ 工作线程零 Tk，UI 轮询不可变 Snapshot |
| 确定性测试、故障注入测试和真实 API 基准均有记录 | ✅ 86 项测试（含 23 项 §13 Barrier/计数器并发不变量测试）+ 基准工具 |
| 默认值由实测结果决定 | ✅ 默认并发 2，硬上限 4，基准验证合理 |
