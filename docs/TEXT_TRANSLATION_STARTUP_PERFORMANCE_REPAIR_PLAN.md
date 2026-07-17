# 启动与文本翻译性能修复实施文档

## 1. 文档信息

- 文档状态：待实施
- 编写日期：2026-07-16
- 适用范围：桌面应用启动、TXT/EPUB 文本翻译、翻译期间 UI 更新、自动保存、HTTP 调度和 PyInstaller 发布包
- 不直接覆盖：Manga 模型推理算法、图片生成服务质量、EPUB 内容正确性重构
- 相关文档：
  - [性能优化指南](./PERFORMANCE_OPTIMIZATION_GUIDE.md)
  - [Python 架构审查与重构实施计划](./PYTHON_ARCHITECTURE_REVIEW_AND_REFACTORING_PLAN.md)
  - [图片翻译修复与优化](./IMAGE_TRANSLATION_REPAIR_AND_OPTIMIZATION.md)

本文档不是方向性建议清单，而是针对当前代码剩余性能问题的落地实施方案。每项改动都包含目标结构、接口草案、迁移步骤、测试要求、性能指标、风险和回滚条件。

代码位置按 2026-07-16 工作区快照记录。实施期间行号可能变化，应以类名和函数名为准。

## 2. 执行摘要

当前项目已经完成若干有效优化：

- 主窗口出现后再加载非首帧模块。
- `TranslatorEngine`、`FileHandler` 和 `EPUBProcessor` 使用延迟构造。
- HTTP 客户端使用持久连接池。
- 流式响应使用列表累积，避免持续字符串全量拼接。
- 表格导入按块插入。
- 自动保存具备 debounce 和单飞机制。
- 主翻译与队列翻译使用不同批次策略。

当前剩余的主要问题不是某个 Python 表达式过慢，而是线程边界和状态所有权不清晰：

1. 翻译工作线程仍会间接调用 `Tk.after()`，并且部分回调在持有进度锁时执行。
2. `Treeview` 同时承担显示层和数据源职责，流式刷新、保存和查漏会反复跨 Tcl/Tk 边界扫描全部行。
3. 交互翻译每轮调度固定等待 100 ms，文档越长，累计人为延迟越明显。
4. 自动保存虽然在后台写文件，但生成保存快照仍在主线程逐行读取 `Treeview`。
5. PyInstaller 单文件包在 Python 代码执行前解包全部资源，运行时延迟导入无法减少这部分冷启动成本。
6. 已采集部分 API 指标，但没有形成启动、翻译、UI 和保存的端到端性能记录。

修复顺序必须保持如下依赖关系：

```text
性能基线
  -> 线程安全事件邮箱
  -> Python 文档状态模型
  -> 自动保存协调器
  -> 调度与批次优化
  -> 启动与打包优化
  -> 基于指标的参数调优
```

不要先提高线程数，也不要先把整个项目改写为 `asyncio`。在当前 Tkinter 桌面架构下，受控线程池、不可变事件和主线程渲染是更小风险的方案。

## 3. 目标与非目标

### 3.1 必须达到的目标

#### 启动

- 主窗口首帧可以快速显示并保持可响应。
- 文本翻译启动路径不导入 Torch、OpenCV、ONNX Runtime 或 Manga 模型代码。
- 发布包冷启动不再被单文件解包和重复资源打包主导。
- 启动各阶段具有可比较的计时数据。

#### 翻译

- 后台线程不直接或间接调用任何 Tk API。
- 网络流读取不因 UI 线程繁忙而持有业务锁等待。
- 交互翻译不存在无条件固定批次完成等待。
- 并发、批大小和限流退避有明确上限。
- 完成、失败、取消和部分成功事件不会因合并 UI 更新而丢失。

#### UI

- 流式事件产生频率与 UI 刷新频率解耦。
- UI 刷新只访问发生变化的行，不在每个流式事件中调用 `get_children()`。
- 5,000 行文档翻译期间窗口仍可拖动，停止按钮可及时响应。
- 旧任务事件不会写入新任务或已经关闭的窗口。

#### 保存

- 保存快照从 Python 状态模型生成，不从 `Treeview` 反向读取。
- 保存状态只能在主线程更新；工作线程只返回结构化结果。
- 保存失败保留 dirty 状态，并允许重试。
- 关闭、暂停、停止和显式保存具有明确的 flush 语义。

### 3.2 非目标

第一轮修复不做以下事项：

- 不为了减少几十毫秒立即引入 SQLite。
- 不直接实现 Treeview 虚拟滚动或完全替换 UI 控件。
- 不启用无上限并发。
- 不在应用启动时自动加载本地图片模型。
- 不长期保留新旧两套数据源。
- 不缓存语义不完整或可能泄漏密钥的请求。
- 不以降低翻译正确率、行对齐率或保存可靠性换取吞吐。

## 4. 当前性能基线与已知事实

### 4.1 当前测量结果

本次审查得到以下参考数据：

| 项目 | 结果 | 说明 |
| --- | ---: | --- |
| `main` 冷导入 | 约 0.37 秒 | 当前系统 Python，非打包环境 |
| `src.ui.main_window` 冷导入 | 约 0.74 秒 | Tkinter 和控制器导入占主要部分 |
| `ConfigManager` 热构造中位数 | 约 3.5 ms | 不包含首次密钥环访问 |
| 10,000 条 EPUB 映射完整原子重写 | 约 32 ms | 本机合成数据，后台 I/O |
| 启动相关标准库测试 | 3/3 通过 | 延迟服务、密钥延迟读取、表格分块加载 |

这些数据说明：

- 配置 JSON 读取不是当前首要瓶颈。
- 后台 JSON 完整写入在一万条规模下尚可接受。
- 可见卡顿更可能来自主线程 Tcl/Tk 调用、跨线程 `after()`、全表快照和固定等待。
- PyInstaller 冷启动必须使用实际发布包单独测量，源码导入数据不能替代发布包测量。

### 4.2 环境前置条件

当前仓库中的 `.venv` 指向已经不存在的 Python 安装，系统 Python 又未安装完整项目依赖。开始性能修复前必须先恢复可重复验证环境。

项目当前的本地 Manga 依赖建议使用 64 位 Python 3.11。所有基准、测试和发布构建必须记录解释器绝对路径和版本。

环境验收：

```powershell
python --version
python -c "import sys; print(sys.executable)"
python -m pip --version
python -c "import httpx, ebooklib, bs4, lxml, PIL, keyring; print('runtime dependencies ready')"
python -m pytest -q
```

如果项目继续采用全局 Python，则删除失效 `.venv` 前先确认其中没有未迁移文件；性能脚本和构建命令必须始终使用同一个全局解释器。不要混用 `pip`、`python -m pip` 和不同版本的 `py` 启动器。

### 4.3 固定基准场景

新增 `tests/performance/fixtures/` 或独立、不进入普通单元测试的基准数据目录，至少准备：

| 场景 | 规模 | 主要观察项 |
| --- | --- | --- |
| 小型 TXT | 100 行 | 首 token、基础回归 |
| 中型 TXT | 5,000 行 | UI 刷新、全表扫描、保存 |
| 大型 TXT | 20,000 行 | 内存、Treeview 可用性、快照成本 |
| 长流式响应 | 50,000 字符 | 回调数量、事件合并、CPU |
| 中型 EPUB | 5,000 段 | 映射保存和恢复 |
| 429 重放 | 连续 2 次 429 后成功 | 退避和并发降级 |
| 断流重放 | 首 token 前/后断开 | 重试语义和重复输出 |
| 取消场景 | 多并发批次运行中取消 | 停止延迟和资源回收 |

公网 API 波动不可作为性能回归依据。文本翻译基准使用本地 Fake API 或注入式 Fake Provider，以固定 TTFT、chunk 数量和响应顺序。

## 5. 目标线程模型

### 5.1 线程所有权

| 资源 | 唯一 owner | 允许的操作 |
| --- | --- | --- |
| Tk root 和所有 widget | Tk 主线程 | 创建、读取、写入、销毁、`after()` |
| 文档状态模型 | Tk 主线程 | 行更新、人工编辑、生成快照 |
| 翻译调度器状态 | 翻译协调线程 | 批次提交、Future 回收、取消 |
| HTTP 客户端 | API 实例 | 多工作线程共享受支持的同步 Client |
| 流式批次局部状态 | 对应工作线程 | chunk 解析、行对齐、批次结果 |
| UI 事件邮箱 | 多生产者/单消费者 | 工作线程发布，Tk 主线程排空 |
| 文件写入 | 单个保存 worker | 只消费不可变快照 |
| 保存协调状态 | Tk 主线程 | generation、dirty、in-flight 状态 |

核心不变量：

```text
后台线程 -> 发布纯 Python 事件 -> 主线程消费事件 -> 更新模型 -> 更新控件
```

禁止路径：

```text
后台线程 -> root.after(...)
后台线程 -> widget.item(...)
后台线程 -> 修改主线程文档列表
持有业务锁 -> 调用 UI 或用户回调
```

### 5.2 事件流

```text
HTTP worker
  -> Stream parser
  -> TranslationProgressEvent
  -> TranslationEventMailbox
  -> TkEventPump (每 40~75 ms)
  -> TranslationPresenter
  -> TranslationDocumentModel
  -> TranslationTableAdapter
```

完成、失败和取消事件为终结事件，必须保序且不得覆盖。流式中间事件只保留每个批次的最新状态。

## 6. 修复一：移除跨线程 Tk 调用

### 6.1 当前问题

当前 `TkUpdateCoalescer.submit()` 会调用 `root.after()`。它由翻译工作线程中的进度回调触发，因此仍然跨线程进入 Tk。

同时，`TranslatorEngine._translate()` 的 `batch_progress()` 和批次完成处理在 `progress_lock` 内调用外部 `progress_callback`。如果 UI 线程繁忙，工作线程可能在持锁状态等待 Tk，其他批次的进度也会被阻塞。

### 6.2 新增结构化事件

建议新增 `src/application/translation_events.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TranslationEventKind(str, Enum):
    STREAM = "stream"
    BATCH_COMPLETED = "batch_completed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


@dataclass(frozen=True, slots=True)
class TranslationProgressEvent:
    run_id: str
    kind: TranslationEventKind
    batch_start: int
    expected_lines: int
    completed_lines: int
    overall_progress: float
    stream_start_line: int = 0
    stream_lines: tuple[str, ...] = ()
    translated_lines: tuple[str, ...] = ()
    display_batch_start: int | None = None
    message: str | None = None
```

要求：

- 使用 `frozen=True`，防止发布后被其他线程修改。
- 使用 `slots=True`，减少大量短生命周期事件的对象开销。
- 不包含 widget、回调、异常对象、HTTP 响应或 API Key。
- 终结事件携带用户可展示的脱敏消息，完整异常只写日志。
- `run_id` 用于丢弃旧任务和窗口关闭后的迟到事件。

### 6.3 使用有界合并邮箱

不建议把每个 token 放进无界队列。建议新增 `src/ui/translation_event_mailbox.py`，使用“最新流式状态字典 + 终结事件队列”：

```python
from __future__ import annotations

from collections import deque
from threading import Lock

from ..application.translation_events import (
    TranslationEventKind,
    TranslationProgressEvent,
)


class TranslationEventMailbox:
    def __init__(self) -> None:
        self._lock = Lock()
        self._latest_stream: dict[tuple[str, int], TranslationProgressEvent] = {}
        self._terminal: deque[TranslationProgressEvent] = deque()

    def publish(self, event: TranslationProgressEvent) -> None:
        key = (event.run_id, event.batch_start)
        with self._lock:
            if event.kind is TranslationEventKind.STREAM:
                self._latest_stream[key] = event
                return
            self._latest_stream.pop(key, None)
            self._terminal.append(event)

    def drain(self) -> tuple[
        tuple[TranslationProgressEvent, ...],
        tuple[TranslationProgressEvent, ...],
    ]:
        with self._lock:
            terminal = tuple(self._terminal)
            stream = tuple(self._latest_stream.values())
            self._terminal.clear()
            self._latest_stream.clear()
        return terminal, stream

    def discard_run(self, run_id: str) -> None:
        with self._lock:
            self._latest_stream = {
                key: value
                for key, value in self._latest_stream.items()
                if key[0] != run_id
            }
            self._terminal = deque(
                event for event in self._terminal if event.run_id != run_id
            )
```

该结构的流式积压上限约等于活动批次数，而不是 chunk 数量。终结事件数量约等于批次数，不能静默丢弃。

### 6.4 Tk 主线程事件泵

建议新增 `src/ui/tk_event_pump.py`：

```python
class TkTranslationEventPump:
    def __init__(self, root, mailbox, render, interval_ms: int = 50):
        self._root = root
        self._mailbox = mailbox
        self._render = render
        self._interval_ms = interval_ms
        self._after_id = None
        self._closed = False

    def start(self) -> None:
        if self._closed or self._after_id is not None:
            return
        self._after_id = self._root.after(self._interval_ms, self._poll)

    def _poll(self) -> None:
        self._after_id = None
        if self._closed:
            return
        terminal, stream = self._mailbox.drain()
        for event in terminal:
            self._render(event)
        for event in stream:
            self._render(event)
        self.start()

    def close(self) -> None:
        self._closed = True
        if self._after_id is not None:
            self._root.after_cancel(self._after_id)
            self._after_id = None
```

约束：

- `start()`、`_poll()` 和 `close()` 只由 Tk 主线程调用。
- 工作线程只能调用 `mailbox.publish()`。
- 终结事件先于本轮残留流式事件处理；终结事件发布时已经移除同批次流式状态。
- `TranslationController.close()` 必须关闭事件泵并使当前 `run_id` 失效。
- 新任务开始时生成新的 `run_id`，旧任务迟到事件直接丢弃。

### 6.5 在锁外调用事件 sink

`TranslatorEngine._translate()` 应改为：

```python
with progress_lock:
    # 只计算共享状态
    completed_by_batch[batch_start] = max(...)
    overall = ...
    display_start = display_batch_start()

event = TranslationProgressEvent(
    ...,
    overall_progress=min(overall, 100.0),
    display_batch_start=display_start,
)
event_sink(event)
```

批次完成分支同样必须先释放 `progress_lock`，再发布事件。

禁止在锁内执行：

- `event_sink()`。
- 日志格式化大量数据。
- `Future.result()`。
- 文件保存。
- Tk 调用。
- 用户提供的任意 callback。

### 6.6 迁移步骤

1. 新增结构化事件和邮箱单元测试，不接入生产路径。
2. 给 `TranslatorEngine` 增加新的 `event_sink` 适配层，暂时保留旧 callback API。
3. 先把 callback 移到锁外，保持现有 UI 行为不变。
4. 在 `TranslationController` 构造邮箱和事件泵。
5. 将三条入口统一接入事件泵：全文翻译、选中行翻译、缺失行补译。
6. 删除工作线程中的所有 `root.after()`。
7. 删除或重写旧 `TkUpdateCoalescer`，不保留两个合并器。
8. 关闭窗口时先使 `run_id` 失效，再停止引擎，最后关闭事件泵。

### 6.7 测试要求

新增 `tests/test_translation_event_mailbox.py`：

- 10,000 个同批次流式事件排队后只保留最新一个。
- 不同批次分别保留最新事件。
- 批次完成事件不会被流式事件覆盖。
- 终结事件发布后同批次中间状态被清除。
- 多线程并发 publish 不丢终结事件，不抛出异常。
- `discard_run()` 只清理指定任务。

扩展 `tests/test_translation_stream_ui.py`：

- Fake root 记录调用线程，任何非主线程 `after()` 都让测试失败。
- 事件泵关闭后不再调度。
- 旧 `run_id` 事件不更新当前表格。
- 完成事件立即形成最终译文，不依赖下一次流式刷新。
- 并发批次乱序完成时写回位置正确。

### 6.8 验收标准

- 代码扫描中，翻译和保存工作线程不调用 `root.after()`。
- UI 流式刷新频率默认不高于 25 次/秒，推荐目标 10 至 20 次/秒。
- 50,000 个合成 chunk 不产生同量级 Tk 回调。
- 停止按钮在 UI 压力场景下 200 ms 内进入处理流程。
- 所有终结状态和最终译文与修改前一致。

## 7. 修复二：建立 Python 文档状态模型

### 7.1 当前问题

`MainWindow.get_table_data()` 逐行调用 `Treeview.item()` 获取全部原文和译文。流式渲染又反复调用 `get_children()`。这使业务状态依赖 Tcl/Tk 控件，并造成大量跨语言边界调用。

`Treeview` 应是渲染结果，不应是应用状态的唯一真相来源。

### 7.2 文档模型

建议新增 `src/application/translation_document.py`：

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TranslationRow:
    source: str
    target: str = ""
    manually_edited: bool = False


class TranslationDocument:
    def __init__(self, rows: list[TranslationRow] | None = None) -> None:
        self._rows = rows or []
        self._version = 0
        self._dirty_indices: set[int] = set()

    @property
    def version(self) -> int:
        return self._version

    def replace(self, source_lines: list[str], target_lines: list[str]) -> None:
        self._rows = [
            TranslationRow(
                source=source,
                target=target_lines[index] if index < len(target_lines) else "",
            )
            for index, source in enumerate(source_lines)
        ]
        self._version += 1
        self._dirty_indices.clear()

    def update_target(
        self,
        index: int,
        value: str,
        *,
        manually_edited: bool = False,
    ) -> bool:
        row = self._rows[index]
        if row.target == value:
            return False
        row.target = value
        row.manually_edited = row.manually_edited or manually_edited
        self._dirty_indices.add(index)
        self._version += 1
        return True

    def snapshot_targets(self) -> tuple[int, tuple[str, ...]]:
        return self._version, tuple(row.target for row in self._rows)
```

模型要求：

- 不导入 Tkinter。
- 只在 Tk 主线程修改，因此不需要为每个行操作加锁。
- 后台保存只接收不可变 tuple 快照。
- 翻译工作线程不直接修改模型，而是发布事件。
- 人工译文默认优先，不被空响应或旧任务覆盖。
- 稀疏翻译的原始索引映射属于应用层命令，不存进 widget。

### 7.3 表格适配器

建议将表格细节收敛到 `src/ui/translation_table_adapter.py`：

```python
class TranslationTableAdapter:
    def __init__(self, treeview) -> None:
        self._treeview = treeview
        self._item_ids: list[str] = []

    def item_id(self, row_index: int) -> str:
        return self._item_ids[row_index]

    def apply_target_updates(self, updates: dict[int, str]) -> str | None:
        last_item = None
        for row_index, target in updates.items():
            item = self._item_ids[row_index]
            values = list(self._treeview.item(item, "values"))
            if values[2] != target:
                values[2] = target
                self._treeview.item(item, values=values)
            last_item = item
        return last_item
```

第一阶段不强制重写所有表格功能，但必须做到：

- 翻译热路径不调用 `get_children()`。
- 行号到 item ID 使用已经维护的稳定列表。
- 搜索和过滤如果重建 item 顺序，必须由适配器同步更新映射。
- 删除或重载文档时增加 generation，旧分块加载回调不得插入新文档。

### 7.4 更新顺序

所有译文更新统一采用以下顺序：

```text
校验 run_id 和索引
  -> 计算非空、非旧版本更新
  -> 更新 TranslationDocument
  -> 收集 changed_indices
  -> TranslationTableAdapter 批量更新 changed_indices
  -> 标记自动保存 dirty
```

人工编辑：

```text
读取单个当前单元格
  -> 更新 TranslationDocument(manually_edited=True)
  -> 更新 Treeview
  -> 写入 undo 记录
  -> 标记自动保存 dirty
```

撤销/重做必须同时更新模型和表格。不得只修改 widget。

### 7.5 迁移步骤

1. 新增 `TranslationDocument` 和纯单元测试。
2. 文档导入完成时同时填充模型和 Treeview。
3. 将 `get_table_data()` 改为从模型返回副本；保留方法签名以降低调用方改动。
4. 将流式和批次完成写回改为“模型优先”。
5. 将人工编辑、撤销、重做、查漏、质检和导出逐项迁移。
6. 增加开发期一致性断言：模型行数等于 item ID 数量。
7. 所有调用方迁移完成后，删除从 Treeview 反向构造全量数据的旧实现。

开发期一致性检查不能长期运行在每个流式事件中，只允许测试或诊断模式启用。

### 7.6 测试要求

新增 `tests/test_translation_document.py`：

- replace 后原文和译文严格对齐。
- 译文更新增加 version 并记录 dirty index。
- 相同值更新不增加 version。
- 空结果不覆盖人工译文。
- snapshot 为不可变数据，后续编辑不改变旧快照。
- 稀疏索引更新写入原始行。
- 撤销和重做后模型与视图状态一致。

UI 适配器测试：

- 更新 3 行时只调用 3 次 `Treeview.item()` 写入。
- 流式刷新不调用 `get_children()`。
- 分块加载完成后 item ID 与模型行数一致。
- 搜索过滤不破坏原始行索引。

### 7.7 验收标准

- `get_table_data()` 不调用 Tk API。
- 每次流式刷新成本与变化行数相关，而不是与文档总行数相关。
- 5,000 行文档连续翻译时，主线程单次刷新目标小于 30 ms。
- 保存、导出、查漏和任务详情读取同一份模型状态。
- 不存在模型译文与 Treeview 译文长期不一致。

## 8. 修复三：重构自动保存协调器

### 8.1 当前问题

当前保存已经把文件 I/O 放到后台线程，但仍存在以下问题：

- 保存快照在主线程通过全表 Treeview 扫描生成。
- 工作线程直接调用 `root.after()` 更新保存状态。
- `_save_in_progress` 和 `_save_dirty_again` 跨线程读写，状态所有权不清晰。
- 批次间隔超过 debounce 时，长文档可能每批都执行完整 checkpoint。

### 8.2 目标状态机

保存状态只在 Tk 主线程修改：

```text
CLEAN
  -> DIRTY
  -> SAVE_SCHEDULED
  -> SAVING(generation=N)
  -> CLEAN              当 persisted_generation == document.version
  -> DIRTY              当保存期间产生新版本
  -> SAVE_FAILED        当写入失败
```

后台线程只执行：

```text
SaveSnapshot -> File/EPUB repositories -> SaveResult
```

### 8.3 保存数据结构

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SaveSnapshot:
    generation: int
    target_path: Path
    mapping_dir: Path | None
    target_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SaveResult:
    generation: int
    succeeded: bool
    elapsed_seconds: float
    bytes_written: int
    error_message: str | None = None
```

`SaveResult` 通过通用 UI 事件邮箱返回主线程，不允许保存 worker 调用 Tk。

### 8.4 调度策略

建议区分事件来源：

| 来源 | debounce | 最大延迟 | 是否立即 flush |
| --- | ---: | ---: | --- |
| 人工单元格编辑 | 1,000 ms | 5,000 ms | 否 |
| 流式中间更新 | 不单独保存 | 不适用 | 否 |
| 批次完成 | 3,000 ms | 10,000 ms | 否 |
| 暂停 | 0 ms | 0 ms | 是 |
| 用户停止 | 0 ms | 0 ms | 是 |
| 翻译完成 | 0 ms | 0 ms | 是 |
| 显式保存 | 0 ms | 0 ms | 是 |
| 关闭窗口 | 0 ms | 5 秒退出等待 | 是 |

最大延迟用于持续有新批次完成时仍定期 checkpoint，避免普通 debounce 永远被推迟。

### 8.5 单飞和 generation 规则

1. 生成快照时记录 `document.version`。
2. 一个项目同时最多存在一个保存 Future。
3. 保存期间的新编辑只增加文档 version，不启动第二个并行写入。
4. 保存结果回到主线程后：
   - 结果 generation 等于当前 version：状态变为 CLEAN。
   - 结果 generation 小于当前 version：状态保持 DIRTY，并调度下一次保存。
   - 保存失败：状态变为 SAVE_FAILED，dirty 不清除。
5. 旧项目保存结果必须通过 project ID 或 document generation 校验，不能更新新项目 UI。
6. 关闭窗口时等待当前关键保存完成；超时必须记录并显示明确失败，不可假装已保存。

### 8.6 EPUB 保存策略

第一阶段继续使用完整 JSON 原子 checkpoint，因为 10,000 条合成映射约 32 ms，尚未证明需要更复杂存储。

第一阶段优化重点：

- 减少 checkpoint 次数。
- 从 Python 模型直接生成快照。
- 避免保存期间再次读取 Treeview。
- 在映射仓储内缓存已经解析的项目结构，仅在项目切换时加载。
- 最终仍使用唯一临时文件和 `os.replace()`。

只有满足以下任一条件才进入增量存储设计：

- 目标项目超过 50,000 段。
- 单次 JSON checkpoint P95 超过 250 ms。
- 10 秒 checkpoint 周期仍造成可见磁盘拥塞。
- 崩溃恢复要求低于 1 秒的数据丢失窗口。

达到条件后优先评估 SQLite WAL，而不是手写 JSON patch 日志。迁移必须包含 schema 版本、事务、备份和旧项目导入。

### 8.7 测试要求

新增 `tests/test_autosave_coordinator.py`：

- 连续 100 次变更只启动一次 debounce 保存。
- 保存期间继续编辑，完成后自动调度下一代快照。
- 旧 generation 成功不能清除新 generation 的 dirty。
- 写入失败保留 dirty，并产生可重试状态。
- 关闭时 flush 等待成功。
- flush 超时返回失败状态。
- 项目切换后旧保存结果不更新新项目状态。
- worker 线程不调用 Fake root。

### 8.8 验收标准

- 翻译期间保存快照不访问 `Treeview`。
- 普通流式 chunk 不触发文件写入。
- 同一项目并行保存数恒为 1。
- 保存失败不会显示“已保存”。
- 5,000 行连续翻译场景中，checkpoint 次数符合调度上限。
- 窗口关闭后不残留保存线程。

## 9. 修复四：移除固定批次等待并规范限流

### 9.1 当前问题

`translate_fast_mode()` 默认传入 `batch_delay=0.1`，调度循环在处理完成 Future 后无条件 `sleep(0.1)`。

默认批次 10 行、并发 1 时，1,000 行约 100 批，仅固定等待就可能累计约 10 秒。该等待发生在批次完成后，不能准确控制请求提交速率，也不能替代服务端限流退避。

### 9.2 最小安全修复

第一步直接完成：

- 删除 `batch_delay` 参数。
- 删除完成循环中的 `time.sleep(batch_delay)`。
- 保留当前最大并发和 HTTP 层 429 退避。
- 测试确保队列模式和交互模式行为不再依赖 sleep。

这是低风险、高确定性收益的修复，应单独提交，便于回滚和测量。

### 9.3 如果提供商需要主动节流

主动节流必须位于“提交请求前”，并使用单调时钟和可取消等待：

```python
class RequestPacer:
    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = max(0.0, min_interval_seconds)
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self, cancel_event: threading.Event) -> bool:
        while True:
            with self._lock:
                now = time.monotonic()
                delay = self._next_allowed - now
                if delay <= 0:
                    self._next_allowed = now + self._min_interval
                    return True
            if cancel_event.wait(delay):
                return False
```

注意：

- 不使用 `time.time()` 计算调度间隔。
- 等待期间不持有调度器主锁或进度锁。
- 取消使用 `Event.wait(timeout)`，不使用不可中断 `sleep()`。
- 默认 `min_interval_seconds=0`。
- 只有真实指标证明提供商需要节流时才启用。

### 9.4 并发策略

并发是 I/O 并发，不应根据 CPU 核数计算。

建议初始配置：

| 场景 | 批次最大行数 | 输入 token 预算 | 并发 |
| --- | ---: | ---: | ---: |
| 交互编辑器保守值 | 10 | 6,000 | 1 |
| 交互编辑器建议试验值 | 20 | 6,000 | 2 |
| 无人值守队列 | 80 | 16,000 | 2 |
| 队列硬上限 | 200 | 20,000 | 4 |

参数调整条件：

- 429 比例超过 2%：立即把新请求并发降为 1，并进入 cooldown。
- 行对齐失败率上升：降低批次行数，不要只增加重试。
- TTFT P95 显著上升：检查批次大小和提供商排队。
- 输出吞吐不变但请求数翻倍：回退并发，避免无效扇出。

不要在同一版本同时修改批次大小、并发和提示协议，否则无法判断性能或正确率变化来源。

### 9.5 运行上下文只构造一次

当前每个批次会重复读取配置、构造术语提示、计算 prompt 哈希并判断模型类型。建议在一次翻译开始时构造不可变上下文：

```python
@dataclass(frozen=True, slots=True)
class TranslationRunContext:
    run_id: str
    provider: str
    model_name: str
    target_language: str
    temperature: float
    system_prompt: str
    prompt_version: str
    glossary_version: str
    is_hunyuan: bool
```

构造顺序：

1. 一次读取 app config。
2. 一次读取 API config 和密钥。
3. 一次构造 glossary prompt。
4. 一次构造 system prompt。
5. 一次计算语义版本哈希。
6. 将上下文传给 batch planner 和所有 `_translate_batch()`。

API Key 不得进入该 dataclass 的 `repr`、日志、指标或缓存键。API 实例已经拥有鉴权信息，运行上下文无需再次携带密钥。

### 9.6 批次计划对象

建议将范围 tuple 替换为清晰的只读对象：

```python
@dataclass(frozen=True, slots=True)
class TranslationBatchPlan:
    batch_id: int
    start: int
    end: int
    estimated_input_tokens: int
```

这样可在性能日志中记录批次大小和 token 估算，同时避免散落的 `start/end` 字典字段。

### 9.7 测试要求

扩展 `tests/test_translation_performance.py`：

- 100 个即时完成 Fake 批次不再产生约 10 秒固定等待。
- 并发最大值不超过配置和 provider recommendation。
- 429 后新提交并发降为 1。
- 取消能够中断 pacer 等待。
- 运行上下文只构造一次。
- glossary prompt 不按批次数重复构造。
- 同一输入在不同并发完成顺序下最终行顺序一致。
- 批次参数变化不改变空行和失败索引语义。

### 9.8 验收标准

- 调度器不存在完成后的无条件 `sleep()`。
- Fake API 零延迟基准耗时不随批次数增加 100 ms/批。
- 并发不会超过硬上限 8；队列默认不超过 2。
- 429、超时、取消和部分成功回归测试通过。
- 行对齐准确率不得低于修改前基线。

## 10. 修复五：HTTP 客户端和协议配置

### 10.1 保留同步 HTTP 模型

当前 Tk 应用采用工作线程调用同步 `httpx.Client`，这是合理方案。不要为了形式统一同时维护同步和异步两套客户端。

要求：

- 一个 API 实例拥有一个持久 Client。
- Client 的创建、替换和关闭具有明确 owner。
- 同一 Client 可由受控翻译线程共享。
- 取消时关闭活动 Client 后必须置空，下一请求按需重建。
- 心跳不得关闭仍有活动请求的 Client。
- 刷新配置前关闭旧 Client 和后台资源。

### 10.2 HTTP/2 依赖

`SiliconFlowAPI` 开启了 `http2=True`，但基础依赖没有声明 `h2`。应选择一种明确方案：

方案 A，正式支持 HTTP/2：

```toml
dependencies = [
    "httpx[http2]>=0.27,<1",
]
```

方案 B，仅支持 HTTP/1.1：删除 `http2=True`，避免运行时异常后回退。

推荐方案 A，但必须在构建环境、PyInstaller 和目标 Windows 机器上验证。不要捕获所有异常后静默把协议能力变化隐藏起来；回退时至少记录一次脱敏 warning 和指标。

### 10.3 延迟创建未使用的批处理器

`BaseAPI` 默认创建 `BatchProcessor`，但主文本翻译路径使用 `translate_stream_enhanced()`。`ThreadPoolExecutor` 虽然会延迟创建线程，仍建议把批处理器改为首次 `translate_batch()` 时构造，或确认主路径确实需要后再保留。

目标：

- 创建 API Client 不创建无用后台 timer 或 executor。
- `translate_batch()` 首次调用线程安全地初始化处理器。
- `close()` 不初始化尚未使用的资源。
- 测试连接不额外常驻一个 Client。

### 10.4 缓存边界

缓存键至少包含：

- provider。
- base URL 的非密钥规范化标识。
- model name。
- target language。
- temperature。
- system prompt 哈希。
- glossary 哈希。
- 行标记协议版本。
- 原文内容。

第一阶段继续使用内存缓存。跨进程持久缓存只有在以下条件满足后再实施：

- 用户明确允许本地保存原文和译文。
- 缓存文件有大小上限和清除入口。
- 缓存键不含 API Key。
- 配置和提示变化会自然失效。
- 取消、空结果、部分响应和错误不写缓存。

### 10.5 验收标准

- HTTP/2 是否启用与依赖声明一致。
- 连续批次复用连接，客户端创建数保持稳定。
- 取消后下一次翻译能够正常重建连接。
- 关闭窗口后无心跳或批处理线程残留。
- 缓存命中不产生后台迟到 UI 回调。

## 11. 修复六：启动与 PyInstaller 打包

### 11.1 当前问题

当前 `translator.spec`：

- 将 `a.binaries`、`a.zipfiles` 和 `a.datas` 直接传给 `EXE`，属于单文件发布结构。
- 把整个 `src` 目录作为 datas 加入，同时 Python 模块又已经进入 PYZ。
- 把 vendored Manga 源码目录作为 datas 加入。
- 声明大量 hidden imports。
- 启用 `upx=True`。

单文件程序需要在 Python 启动前解包资源。即使应用内部延迟导入模型，Bootloader 仍可能先处理大体积二进制。源码目录和已编译模块重复打包也会增加包体及解包成本。

### 11.2 发布矩阵

建议提供两个明确构建目标：

| 构建 | 内容 | 默认格式 | 用途 |
| --- | --- | --- | --- |
| Text Edition | TXT/EPUB 文本翻译、AI 图片 Provider | onedir | 默认下载，启动优先 |
| Full Manga Edition | Text Edition + 本地 Manga 依赖 | onedir | 需要本地图片推理的用户 |

模型权重不打进可执行文件，继续放在用户数据目录并按需下载或由用户配置。

如果必须提供单文件版，将其标记为便携版，并明确冷启动可能慢于 onedir；不要把单文件版作为唯一性能基准。

### 11.3 onedir spec 结构

根据实际 PyInstaller 版本调整参数，核心结构如下：

```python
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LightNovelTranslatorV1.6",
    console=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LightNovelTranslatorV1.6",
)
```

实施时：

1. 删除 `('src', 'src')`，Python 模块由 Analysis/PYZ 收集。
2. 对 Manga 动态导入使用准确的 hidden import 或官方 hook，不把完整源码再次作为普通 data 复制。
3. 仅把字体、配置样例和真正的非 Python 资源放入 datas。
4. Text Edition 明确排除 Torch、OpenCV、ONNX Runtime、模型文件和 Manga 包。
5. Full Edition 使用独立 spec，避免基础版被可选依赖污染。
6. 分别对 UPX 开启和关闭构建，实测后决定；不要凭包体大小判断启动性能。
7. 构建日志保存 module graph 和最终文件清单，防止依赖悄然膨胀。

### 11.4 启动代码阶段

应用内部启动保留以下顺序：

```text
进程进入 main
  -> 创建 Tk root
  -> 设置最小窗口和加载状态
  -> 首帧绘制
  -> 解析 AppPaths 和日志
  -> 加载非敏感配置
  -> 构建主窗口必要控件
  -> 后台读取密钥状态
  -> 用户触发时加载翻译 API
  -> 用户触发本地图片翻译时加载模型
```

不要在启动阶段：

- 测试公网 API。
- 创建 Manga engine。
- 导入 Torch、cv2 或模型注册表。
- 扫描整个 workspace。
- 读取全部最近项目内容。
- 预先创建所有弹窗。

### 11.5 启动测量

内部时间点：

- `process_python_started`：`main.py` 能记录的最早点。
- `tk_root_created`。
- `loading_frame_requested`。
- `main_window_imported`。
- `config_loaded`。
- `main_window_constructed`。
- `main_window_interactive`。
- `api_status_loaded`。

PyInstaller Bootloader 解包发生在 `main.py` 之前，必须用外部启动器或 PowerShell 测量进程启动到窗口出现时间。内部指标和外部指标都要保留。

每个构建至少运行 10 次：

- 记录 median、P90、P95，不只看最好一次。
- 分开记录首次冷启动和后续启动。
- 记录 Defender、磁盘类型、CPU、Python/PyInstaller 版本。
- onedir 与 onefile 使用同一机器和同一构建内容比较。

### 11.6 验收标准

- 默认发布包使用 onedir。
- Text Edition 不包含本地模型重依赖。
- `src` 不作为重复 data 打包。
- 主窗口可交互时间相对当前发布包至少降低 30%，或达到产品目标阈值。
- 冷启动期间无公网请求和模型加载。
- 连续启动关闭 10 次无残留进程和临时解包目录泄漏。

## 12. 修复七：建立端到端可观测性

### 12.1 当前缺口

`BaseAPI.get_performance_metrics()` 已计算平均 TTFT、请求耗时和输出速度，但生产路径没有把这些数据关联到一次翻译运行，也没有记录 UI 队列延迟和保存耗时。

没有端到端指标时，无法区分：

- 服务端排队慢。
- DNS/TLS 首次连接慢。
- Python chunk 解析慢。
- UI 消费慢。
- 自动保存导致主线程停顿。
- PyInstaller 解包慢。

### 12.2 统一计时器

建议新增 `src/utils/performance.py`：

```python
from dataclasses import dataclass, field
from time import perf_counter_ns


@dataclass(slots=True)
class PerformanceSpan:
    name: str
    started_ns: int = field(default_factory=perf_counter_ns)
    finished_ns: int | None = None

    def finish(self) -> float:
        self.finished_ns = perf_counter_ns()
        return (self.finished_ns - self.started_ns) / 1_000_000_000
```

生产代码只记录低频阶段指标，不为每个 token 创建 span。

### 12.3 翻译运行指标

每次 `run_id` 聚合：

```text
source_lines
pending_lines
batch_count
configured_concurrency
max_observed_concurrency
request_attempts
retries
rate_limit_errors
ttft_seconds: median/P95
request_seconds: median/P95
output_tokens_per_second
stream_events_published
ui_flush_count
max_mailbox_batches
max_ui_event_age_ms
save_count
save_seconds
cancel_latency_ms
final_status
```

不要只保留累计平均值。累计平均会掩盖某一次严重卡顿。

### 12.4 UI 卡顿指标

诊断模式下，Tk 主线程每 100 ms 运行一次轻量 heartbeat：

```python
expected = time.perf_counter() + interval

def tick():
    nonlocal expected
    now = time.perf_counter()
    lag_ms = max(0.0, (now - expected) * 1000)
    recorder.observe_ui_lag(lag_ms)
    expected = now + interval
    root.after(interval_ms, tick)
```

只记录：

- 最大 lag。
- 超过 100 ms 的次数。
- 超过 250 ms 的次数。

不要每 100 ms 写日志。任务结束时汇总一次。

### 12.5 日志格式

建议使用稳定事件名和结构化字段：

```text
perf.translation_run_completed run_id=... lines=... batches=... elapsed_s=...
perf.api_summary run_id=... ttft_p95_s=... retries=... rate_limits=...
perf.ui_summary run_id=... flushes=... max_lag_ms=... max_event_age_ms=...
perf.save_summary run_id=... saves=... elapsed_s=... bytes=...
perf.startup interactive_ms=... config_ms=... window_ms=...
```

安全要求：

- 不记录 API Key、Authorization header、完整提示词或原文。
- model、provider 和批次数可以记录。
- 错误正文必须经过现有日志脱敏器。
- 性能诊断默认关闭详细模式，汇总指标可常开。

### 12.6 验收标准

- 一次翻译可以通过 `run_id` 关联 API、UI 和保存指标。
- 指标本身不会产生每 token 日志。
- 性能日志不包含原文、译文和密钥。
- 基准报告可自动生成修改前后对照表。

## 13. 测试与基准体系

### 13.1 单元测试

必须覆盖：

- 结构化事件不可变性。
- 邮箱流式合并和终结事件不丢失。
- callback 在锁外执行。
- 文档模型版本、dirty 和人工编辑保护。
- 保存 generation 状态机。
- pacer 可取消和单调时钟行为。
- 运行上下文只构造一次。
- HTTP/2 配置与依赖一致。

### 13.2 集成测试

Fake Provider 支持配置：

- TTFT。
- chunk 间隔。
- 每批 chunk 数。
- 指定批次乱序完成。
- 429 次数和 `Retry-After`。
- 首 token 前断流。
- 首 token 后断流。
- 空响应。
- 部分行缺失。

验证：

- 译文最终顺序。
- 最大并发。
- 请求数量。
- UI flush 数量。
- 自动保存次数。
- 取消后残留线程。

### 13.3 UI 测试

Fake Tk root 必须记录创建线程 ID：

```python
class ThreadCheckingRoot:
    def __init__(self) -> None:
        self.owner = threading.get_ident()

    def after(self, delay, callback):
        assert threading.get_ident() == self.owner
        ...
```

关键用例：

- 工作线程高频 publish，只有主线程调用 `after()`。
- 关闭事件泵后无新回调。
- 大量流式事件被合并。
- 最终事件立即覆盖中间快照。
- 新任务开始后旧任务事件被丢弃。
- 模型和表格最终一致。

### 13.4 性能测试

性能测试不应混入普通毫秒级单元测试。建议：

```text
tests/performance/test_event_mailbox_benchmark.py
tests/performance/test_translation_scheduler_benchmark.py
tests/performance/test_document_snapshot_benchmark.py
tests/performance/test_mapping_checkpoint_benchmark.py
tools/benchmark_startup.ps1
tools/benchmark_translation.py
```

CI 分层：

- 每次提交：正确性单元测试和小型确定性性能断言。
- 每日或发布前：5,000/20,000 行基准、PyInstaller 启动、内存和线程泄漏。
- 带模型测试：独立 GPU/CPU runner，不阻塞普通 PR。

### 13.5 防止脆弱性能测试

普通 CI 不断言“必须少于 20 ms”这类机器相关绝对值。优先断言结构性指标：

- 10,000 个同批次事件最多保留 1 个流式快照。
- 100 个批次不执行 100 次固定 sleep。
- 3 行变化只产生 3 行 widget 更新。
- 100 次连续编辑只形成 1 个 debounce 保存。
- 活动线程数在关闭后回到基线。

绝对耗时用于固定机器的基准报告和发布门禁。

## 14. 分阶段实施计划

### 阶段 0：恢复验证环境和记录基线

改动：

- 固定支持的 Python 3.11 解释器。
- 安装运行和开发依赖。
- 运行全量测试并记录失败。
- 建立 Fake Provider 基准。
- 对当前发布包测量 10 次启动。

完成条件：

- `python -m pytest -q` 可重复执行。
- 基准数据包含环境信息和原始结果。
- 不使用真实付费 API 作为回归基线。

### 阶段 1：callback 移出锁

涉及：

- `src/core/translator.py`
- `tests/test_translation_performance.py`

完成条件：

- 所有外部 callback 在锁外调用。
- 翻译结果和事件顺序回归通过。

这是最小提交，不同时改 UI 或批次参数。

### 阶段 2：事件邮箱和 Tk 事件泵

涉及：

- `src/application/translation_events.py`
- `src/ui/translation_event_mailbox.py`
- `src/ui/tk_event_pump.py`
- `src/ui/translation_controller.py`
- 对应测试

完成条件：

- 翻译 worker 不调用 Tk。
- 高频流式事件有界合并。
- 完成、失败、取消事件不丢失。

### 阶段 3：文档状态模型

涉及：

- `src/application/translation_document.py`
- `src/ui/translation_table_adapter.py`
- `src/ui/main_window.py`
- `src/ui/translation_controller.py`
- 编辑器、撤销重做和保存调用方

完成条件：

- 模型成为唯一业务状态来源。
- 翻译和保存热路径不全表读取 Treeview。
- 开发期一致性测试通过。

### 阶段 4：自动保存协调器

涉及：

- `src/application/autosave.py`
- `src/ui/main_window.py`
- 文件和映射仓储
- 对应测试

完成条件：

- 保存状态只由主线程管理。
- worker 不调用 Tk。
- generation、失败重试和关闭 flush 通过测试。

### 阶段 5：删除固定等待和构造运行上下文

涉及：

- `src/core/translator.py`
- `src/config/translation_profile.py`
- `tests/test_translation_performance.py`
- `tests/test_queue_bulk_translation.py`

完成条件：

- 无固定完成 sleep。
- 配置和 prompt 每次运行只构造一次。
- 吞吐提升且行对齐率不下降。

### 阶段 6：HTTP 和资源延迟构造

涉及：

- `src/api/base_api.py`
- `src/api/siliconflow_api.py`
- `pyproject.toml`
- `requirements.txt`
- 心跳、取消和配置测试

完成条件：

- HTTP/2 依赖明确。
- 未使用的批处理资源延迟创建。
- 关闭后线程和 Client 归零。

### 阶段 7：PyInstaller 构建拆分

涉及：

- `translator-text.spec`
- `translator-full.spec`
- `build.py`
- 构建文档和发布工作流

完成条件：

- 默认 onedir Text Edition。
- Full Edition 不污染基础包。
- 启动基准达到门槛。

### 阶段 8：参数调优

只有前述结构性问题解决并具备指标后再调：

- 交互批次 10 -> 20。
- 交互并发 1 -> 2。
- UI 刷新间隔 40 -> 50/75 ms。
- checkpoint 周期。
- provider-specific pacing。

每次只改变一个参数族，并保留 A/B 数据。

## 15. 建议提交拆分

推荐提交顺序：

1. `test: add deterministic translation performance baselines`
2. `perf: invoke translation callbacks outside progress locks`
3. `refactor: add bounded translation event mailbox`
4. `perf: render translation events only on the Tk thread`
5. `refactor: add translation document state model`
6. `perf: stop reading the full Treeview on stream updates`
7. `refactor: add generation-based autosave coordinator`
8. `perf: remove fixed post-batch delay`
9. `perf: build immutable translation run context once`
10. `build: declare HTTP2 dependency explicitly`
11. `build: add onedir text and full distribution targets`
12. `perf: expose startup translation UI and save metrics`

每个提交必须独立通过相关测试。不要在同一提交中混入 UI 改版、翻译提示词修改或图片模型质量调优。

## 16. 风险与回滚

### 16.1 事件合并风险

风险：中间流式事件被覆盖后，某些行可能直到批次完成才显示。

控制：

- 最终批次结果永不合并丢弃。
- 每个批次保留最新完整快照或新增行范围。
- 使用 run ID 和 batch start 定位。

回滚条件：最终译文缺行、重复或写错索引。出现时仅回滚事件渲染适配器，不回滚 callback 移出锁的修复。

### 16.2 双数据源风险

风险：迁移期间模型和 Treeview 不一致。

控制：

- 模型优先写入。
- 只保留一个 `apply_target_updates()` 入口。
- 测试模式增加一致性断言。
- 迁移期明确截止版本，完成后删除旧全表反向读取。

回滚条件：保存内容与界面最终内容不一致。回滚时恢复模型适配层之前的读取入口，但保留测试和问题 fixture。

### 16.3 保存节流风险

风险：checkpoint 频率降低后，异常退出可能丢失更多最近进度。

控制：

- 设置最大 checkpoint 延迟。
- 暂停、停止、完成和关闭立即 flush。
- 保存失败保持 dirty 并提示。

回滚条件：正常关闭丢失数据或失败状态误报成功。该问题优先级高于性能，必须立即回滚保存协调器。

### 16.4 并发调优风险

风险：提高并发后触发 429、输出乱序或成本上升。

控制：

- 默认仍可保持并发 1。
- 独立发布并发参数变化。
- 429 自动降级。
- 结果按原始索引写回。

回滚条件：429 比例、失败率或行错位高于基线。

### 16.5 打包拆分风险

风险：onedir 文件较多，用户移动单个 exe 后无法运行；Full Edition 动态导入可能漏包。

控制：

- 使用 zip 安装包或安装器分发完整目录。
- 发布前运行干净机器冒烟测试。
- Text 和 Full 使用独立 smoke test。

回滚条件：目标 Windows 干净环境无法启动或关键 Provider 缺失。可临时继续发布旧便携包，但不能删除新的启动基准。

## 17. 代码审查清单

### 线程和回调

- [ ] 后台线程没有调用任何 Tk API。
- [ ] 外部 callback 不在锁内执行。
- [ ] 等待操作不持有共享锁。
- [ ] 所有线程、Client、Timer 和事件泵有 owner 和 `close()`。
- [ ] `cancel()` 和 `close()` 幂等。
- [ ] 迟到事件通过 run ID 丢弃。

### 数据模型

- [ ] Treeview 不是业务状态唯一来源。
- [ ] 人工译文不会被空结果覆盖。
- [ ] 稀疏索引和批次索引有单元测试。
- [ ] snapshot 不可变。
- [ ] undo/redo 同时更新模型和视图。

### 保存

- [ ] 保存快照不访问 Tk。
- [ ] 同一项目最多一个保存 worker。
- [ ] generation 旧结果不能清除新 dirty。
- [ ] 原子写入失败保留旧文件。
- [ ] 关闭 flush 有超时和失败状态。

### HTTP 和调度

- [ ] 不存在固定批次完成 sleep。
- [ ] 429 使用 `Retry-After` 或有界指数退避。
- [ ] 并发有硬上限。
- [ ] HTTP/2 与依赖声明一致。
- [ ] API Key 不进入缓存键、指标或日志。

### 启动和打包

- [ ] 启动不加载本地模型重依赖。
- [ ] `src` 不重复作为 data 打包。
- [ ] 默认发布包为 onedir。
- [ ] Text 和 Full 构建依赖隔离。
- [ ] 实际发布包完成多次启动基准。

## 18. 最终验收矩阵

| 类别 | 指标 | 目标 |
| --- | --- | ---: |
| 启动 | Text Edition 主窗口可交互时间 | 相对当前包下降至少 30% |
| 启动 | 启动期模型重依赖导入 | 0 |
| UI | 默认最大刷新频率 | 不超过 25 次/秒 |
| UI | 5,000 行翻译时单次刷新 P95 | 小于 30 ms |
| UI | 超过 250 ms 主线程 lag 次数 | 目标为 0 |
| 事件 | 10,000 同批次 chunk 的积压快照 | 最大 1 个 |
| 调度 | 固定人为等待 | 0 ms/批 |
| 调度 | 并发上限 | 配置值和硬上限内 |
| 正确性 | 行对齐准确率 | 不低于基线 |
| 保存 | 同项目并行保存 | 最大 1 |
| 保存 | 普通流式 chunk 触发写入 | 0 |
| 保存 | 正常关闭数据丢失 | 0 |
| 资源 | 关闭后残留翻译/心跳/保存线程 | 0 |
| 安全 | 性能日志中的原文、密钥 | 0 |

绝对启动和 UI 指标应在固定机器上建立产品门槛；上述相对指标用于第一轮修复验收。

## 19. 完成定义

只有同时满足以下条件，性能修复才视为完成：

- 全量正确性测试通过。
- 新增事件邮箱、文档模型、自动保存状态机和调度测试通过。
- 工作线程不直接或间接调用 Tk。
- 翻译热路径不再扫描完整 Treeview。
- 固定批次等待已经删除。
- 启动和翻译基准均提供修改前后原始数据。
- Text Edition 和 Full Manga Edition 在干净 Windows 环境完成冒烟测试。
- 性能日志经过密钥和文本泄漏审查。
- 保存失败、取消、部分成功和应用关闭的行为没有退化。
- 旧兼容路径已删除或有明确删除版本，不形成长期双架构。

完成后的优化重点应由指标决定。如果网络 TTFT 占总耗时 90% 以上，继续微调 Python 字符串操作不会带来可见收益；如果 UI lag 和本地阶段已经达标，应优先优化提供商、批次语义或用户工作流，而不是继续增加本地复杂度。
