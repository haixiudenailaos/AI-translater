# 性能优化指南

## 1. 文档目的

本文档给出当前项目的性能优化方向、测量方法、实施顺序和验收指标。

性能优化必须建立在正确性之上。开始实施前应先阅读：

- [Bug 修复指南](./BUG_FIX_GUIDE.md)
- [模块拆分与重构指南](./MODULE_REFACTORING_GUIDE.md)

尤其应先解决失败状态、EPUB 项目隔离、保存错误传播和资源关闭问题。错误结果更快地产生不属于性能优化。

## 2. 优化原则

- 先测量，再修改。
- 优先消除重复工作、无界增长和主线程阻塞。
- 网络 API 任务优先优化请求数量、批次质量和连接复用，而不是单纯增加线程。
- UI 刷新频率与业务事件频率解耦。
- 大型图片和 EPUB 内容避免同时保留二进制、Base64 和 JSON 三份副本。
- 并发必须有上限、取消机制、速率限制和资源 owner。
- 每次优化都要保留行为回归测试，并记录优化前后数据。
- 不使用私有属性或未文档化实现细节换取微小性能收益。

## 3. 当前高收益热点

| 编号 | 优先级 | 热点 | 预期收益 | 风险 |
| --- | --- | --- | --- | --- |
| PERF-001 | P0 | 流式文本反复全量拼接 | 降低长响应 CPU 占用和延迟增长 | 低 |
| PERF-002 | P0 | Tk 逐分片、逐行刷新及滚动 | 消除大文档翻译时界面卡顿 | 中 |
| PERF-003 | P0 | 自动保存反复整份读写 JSON | 显著减少磁盘 I/O 和 UI 停顿 | 中 |
| PERF-004 | P0 | 图片 Base64 内嵌 JSON | 显著降低峰值内存和工作区体积 | 中 |
| PERF-005 | P1 | HTTP 客户端和后台资源重复创建 | 降低连接建立成本和线程数量 | 中 |
| PERF-006 | P1 | async 函数内部执行同步网络 I/O | 避免事件循环被阻塞，明确并发模型 | 中 |
| PERF-007 | P1 | EPUB 多次遍历和重复解析 HTML | 缩短大型 EPUB 导入导出时间 | 中 |
| PERF-008 | P1 | 无节流的调试日志和字符串构造 | 降低热路径 CPU 与日志体积 | 低 |
| PERF-009 | P2 | 缓存未接入主路径或键不完整 | 减少真正重复的 API 请求 | 高 |
| PERF-010 | P2 | 伪批处理和固定按行分批 | 提升 API 吞吐与输出稳定性 | 高 |

## 4. 建立性能基线

### 4.1 固定测试场景

至少准备以下 fixture：

| 场景 | 规模 | 目的 |
| --- | --- | --- |
| 小型 TXT | 100 行 | 快速回归和启动开销 |
| 中型 TXT | 5,000 行 | UI、自动保存和批次调度 |
| 长单批响应 | 50,000 字符 | 流式拼接复杂度 |
| 小型 EPUB | 20 章、20 张图 | 功能回归 |
| 大型 EPUB | 200 章、500 张图 | 导入、内存和导出性能 |
| 大图 EPUB | 50 张 4K 图片 | 图片处理峰值内存 |
| API 故障场景 | 429、超时、断流 | 重试对吞吐的影响 |

测试 API 时优先使用本地假服务重放固定流式响应，避免公网波动污染结果。

### 4.2 核心指标

记录以下指标：

- 应用启动到主窗口可交互时间。
- EPUB 导入总耗时和各阶段耗时。
- 首个翻译分片延迟。
- 单批翻译总耗时。
- UI 每秒刷新次数。
- Tk 主线程最长阻塞时间。
- 自动保存次数、每次写入字节数和耗时。
- 峰值常驻内存 RSS。
- HTTP 请求数、重试数和新建客户端数。
- 活跃线程数和任务结束后的残留线程数。
- 缓存命中率及节省的请求数。

### 4.3 推荐工具

- `time.perf_counter()`：业务阶段计时。
- `cProfile` 和 `pstats`：CPU 热点。
- `tracemalloc`：Python 对象分配和峰值内存。
- `py-spy`：不修改代码的采样分析，可选。
- `psutil`：进程 RSS、线程数和 I/O 计数，可选。
- 本地假 HTTP 服务：稳定重放普通和流式响应。

不要长期在热路径中保留高频详细计时。建议通过 `PERF_DIAGNOSTICS=1` 开关启用。

### 4.4 基准记录格式

每个优化 PR 附带如下表格：

| 指标 | 修改前 | 修改后 | 变化 | 环境 |
| --- | ---: | ---: | ---: | --- |
| 5,000 行 UI 完整刷新 |  |  |  | Python/OS/机器 |
| EPUB 导入时间 |  |  |  | fixture 版本 |
| 峰值 RSS |  |  |  | fixture 版本 |
| 自动保存写入量 |  |  |  | 测试时长 |

## 5. PERF-001：优化流式文本处理

### 现状

流式回调将每个 chunk 追加到列表后，再执行：

```python
current_text = "".join(stream_buffer)
```

响应越长，每个新 chunk 需要重新复制的历史文本越多。总复制量可能接近 O(n²)。结束阶段又再次拼接全部缓冲区。

### 优化方案

仅保留尚未形成完整行的尾部片段，并增量输出完整行：

```python
pending = ""
completed_lines: list[str] = []


def on_chunk(chunk: str) -> None:
    nonlocal pending
    pending += chunk
    parts = pending.split("\n")
    pending = parts.pop()
    if parts:
        completed_lines.extend(parts)
        emit_new_lines(parts)
```

请求结束后再处理 `pending`。如果最终需要完整响应，可让 API 层负责一次性累积，业务层不要为 UI 预览重复构建全量字符串。

进一步建议：

- API 层发送 `StreamLineEvent`，而不是发送原始字符 chunk。
- UI 事件只包含新增行或当前批次快照。
- 预编译行号正则，不在每个批次重复编译。
- 限制 UI 预览字符数，最终结果仍完整保存。

### 验收指标

- 长响应处理时间随字符数近似线性增长。
- 50,000 字符固定响应的 CPU 时间显著下降。
- 译文行号解析结果与修改前一致。
- chunk 在任意字符位置断开时都不会丢字或重复。

## 6. PERF-002：合并 Tkinter UI 更新

### 现状

流式进度中频繁执行：

- `root.after(0, update_ui)`。
- `progress_bar.update_idletasks()`。
- 多次 `Treeview.item()` 读取和写入。
- 每更新一行就调用 `Treeview.see()`。
- 每次回调重新获取全部 `get_children()`。

网络分片可能每秒产生数十到数百个事件，Tk 主线程容易被刷新任务淹没。

### 优化方案

#### UI 事件节流

将业务进度保存为“最新快照”，固定每 50 至 100 毫秒最多刷新一次：

```python
class TkUpdateCoalescer:
    def __init__(self, root, interval_ms: int = 75):
        self.root = root
        self.interval_ms = interval_ms
        self.pending = None
        self.scheduled = False

    def submit(self, snapshot) -> None:
        self.pending = snapshot
        if not self.scheduled:
            self.scheduled = True
            self.root.after(self.interval_ms, self._flush)

    def _flush(self) -> None:
        snapshot = self.pending
        self.pending = None
        self.scheduled = False
        if snapshot is not None:
            self.render(snapshot)
```

中间快照可以覆盖，最终批次完成、错误和取消事件不能丢弃，应立即刷新。

#### 减少 Treeview 调用

- 加载表格时缓存行索引到 item ID 的列表。
- 每次只更新发生变化的行。
- 一次回调内完成一批行更新。
- 仅在当前行离开可视区域或批次完成时滚动。
- 移除热路径中的 `update_idletasks()`，让 Tk 正常事件循环刷新。

#### 大文档虚拟化

如果 5,000 至 10,000 行仍明显卡顿，再考虑分页或虚拟列表。不要第一步就重写 Treeview；先完成节流和增量更新。

### 验收指标

- 流式翻译时 UI 刷新不超过每秒 10 至 20 次。
- 主线程单次刷新目标小于 16 至 30 毫秒。
- 5,000 行表格翻译期间窗口可拖动、按钮可响应。
- 最终显示结果与未节流版本完全一致。

## 7. PERF-003：降低自动保存 I/O

### 现状

自动保存会读取表格全部行、重写完整 TXT，并在 EPUB 模式下重新读取和写出完整 `content_mapping.json`。大型项目中一次小改动也会产生 O(n) 序列化和磁盘写入。

### 优化层级

#### 第一阶段：改进 debounce

- 将连续编辑和流式更新合并为 1 至 2 秒一次。
- 批次完成时保存，而不是每个流式分片保存。
- 窗口关闭、任务暂停和显式保存时立即 flush。
- 保存正在执行时不启动第二次保存，只标记 `dirty_again`。

#### 第二阶段：维护内存项目状态

- 翻译期间以内存中的 segment 列表作为真相来源。
- UI 和保存层消费同一份应用状态，避免每次从 Treeview 反向重建全部数据。
- 记录 dirty segment ID，只更新变化项。

#### 第三阶段：选择存储格式

如果项目通常少于几万段，可以继续使用 JSON，但降低完整 checkpoint 频率。

如果需要高频增量保存、恢复和查询，考虑 SQLite：

```text
projects
segments(project_id, segment_id, source, target, updated_at)
images(project_id, image_id, source_path, translated_path)
```

SQLite 不是第一阶段必需项。先测量 JSON checkpoint 是否仍是瓶颈。

### 数据安全要求

- 所有 checkpoint 使用原子替换。
- 保存线程只接收不可变 snapshot，避免并发遍历正在修改的列表。
- 退出前等待最后一次关键保存完成，设置合理超时。
- 保存失败必须保留 dirty 状态并向用户报告。

### 验收指标

- 连续流式更新期间每个项目最多约 1 次写入/秒，建议更低。
- 单行编辑不再立即重写多次完整 JSON。
- 崩溃恢复最多丢失约定 checkpoint 周期内的数据。
- 保存期间 UI 不出现明显停顿。

## 8. PERF-004：移除图片 Base64 JSON 存储

### 现状

EPUB 图片被编码为 Base64 并写入 `images.json`：

- Base64 体积通常比二进制增加约 33%。
- JSON 解析会再创建大型 Python 字符串。
- 后续转换又会解码回二进制。
- 导入阶段可能同时持有原始 bytes、Base64 字符串和 JSON 序列化字符串。

大型插图书会产生很高峰值内存和工作区体积。

### 优化方案

导入时把图片按二进制写入项目资源目录，JSON 只保存元数据：

```json
{
  "images": [
    {
      "epub_path": "OEBPS/images/cover.jpg",
      "local_path": "assets/000001-cover.jpg",
      "mime_type": "image/jpeg",
      "size": 123456,
      "checksum": "..."
    }
  ]
}
```

处理要求：

- 需要上传时再按需读取单张图片并编码。
- 单张处理完成后释放 bytes 和 Base64 引用。
- 不把 data URI 长期保存在内存项目模型中。
- 相同 checksum 的资源可在同一项目内去重。
- 图片转换使用文件或流接口，避免额外复制。

### 兼容迁移

加载旧 `images.json` 时：

1. 逐张解码到 `assets/`。
2. 生成新 metadata。
3. 原子写入新 schema。
4. 验证 checksum 后再删除旧 Base64 字段。

### 验收指标

- 大图 EPUB 导入峰值 RSS 明显下降。
- 新 `images.json` 大小只与图片数量和路径长度相关。
- 图片上传和导出结果不变。
- 单张损坏图片不会阻止其他图片加载。

## 9. PERF-005：复用 HTTP 客户端和连接池

### 现状

API 实例刷新、连接测试和任务创建可能反复创建 HTTP 客户端、线程池及心跳线程。资源生命周期问题修复后，才能安全复用。

### 优化方案

- 每个 provider 配置版本持有一个长生命周期客户端。
- 配置发生实际变化时才替换客户端。
- 使用统一 provider factory，避免 UI 自行创建临时实例。
- 连接测试复用受控客户端，或使用上下文管理器及时关闭临时客户端。
- 为连接池设置与真实并发数匹配的上限。
- 删除没有数据证明收益的心跳线程；HTTP keep-alive 不要求应用主动发送 HEAD。

客户端键可以包含：

```text
provider + base_url + API key reference + timeout + transport settings
```

不要把明文 Key 输出到键或日志。

### 验收指标

- 稳定配置下连续翻译不重复创建客户端。
- 连接池连接数与配置并发上限一致。
- 任务结束后线程数保持稳定。
- 首批之后的连接建立延迟下降。

## 10. PERF-006：统一同步或异步网络模型

### 现状

图片翻译声明为 `async`，内部却使用同步 `OpenAI` 客户端和 `requests.get()`。同步调用会阻塞事件循环，因此当前异步结构没有提供真正的并发收益，反而增加了事件循环管理复杂度。

### 两种有效方案

#### 方案 A：保持同步，在线程中执行

适用于当前 Tk 应用和较低图片并发：

- 将图片服务改为普通同步函数。
- 整个工作流放入一个受控 worker thread。
- 使用同步 `httpx.Client` 或 SDK。
- 通过取消令牌在图片之间停止。

这是迁移成本较低的方案。

#### 方案 B：完全异步

适用于需要同时检测或下载多张图片：

- 使用 `AsyncOpenAI` 或真正异步 provider。
- 使用 `httpx.AsyncClient` 下载图片。
- 使用 `asyncio.Semaphore` 限制并发。
- 整个后台服务持有一个事件循环，不为每次操作新建 loop。

不要混用同步网络请求和异步函数后声称获得并发。

### 并发建议

- OCR/视觉检测初始并发 2 至 4。
- 图片生成初始并发 1 至 2。
- 下载并发可以略高，但仍需限流。
- 遇到 429 时自动降低并发或增加退避。
- 并发值必须可配置，但设置合理上下限。

### 验收指标

- 事件循环中不存在同步网络调用。
- 并发提高后总耗时下降，且 429/失败率没有显著上升。
- 取消任务能够中止未开始项并及时停止活动请求。

## 11. PERF-007：减少 EPUB 重复遍历和解析

### 现状

导入过程多次调用 `book.get_items()`，分别处理 manifest、CSS、正文、计数和图片。导出时文档还可能因图片替换和正文替换被分别解析多次。

### 优化方案

#### 单次分类

读取 EPUB 后先对 item 做一次分类：

```python
items = list(book.get_items())
documents = []
images = []
styles = []

for item in items:
    ...
```

正文顺序仍应以 spine 为准，不能用此分类列表替代阅读顺序。

#### 单次 DOM 解析

导出每个文档时一次完成：

- 正文定位和替换。
- `<img>`/SVG `<image>` 引用替换。
- figcaption 注入。
- 空白清理。

完成后只序列化一次 HTML。

#### 减少重复查找

- 图片映射同时建立规范化完整路径索引和文件名索引。
- 不在每个 `<img>` 上线性扫描完整 `image_text_map`。
- segment locator 建立 `document_href -> segments` 索引。
- 对大映射避免反复 `Path()` 构造和字符串标准化。

#### 按需处理图片

用户未启用图片功能时，不提取或编码所有图片。可以只保存 manifest 元数据，真正需要时再从源 EPUB 解压。

### 验收指标

- 每个 HTML 文档导入和导出阶段各解析不超过一次。
- 图片匹配从 O(document_images × mapping_size) 降为接近 O(document_images)。
- 不启用图片功能时，导入时间和内存显著降低。
- 导出 EPUB 结构和阅读顺序保持正确。

## 12. PERF-008：控制日志热路径开销

### 现状

EPUB 和图片处理存在大量循环内 `logger.debug(f"...")`。即使 DEBUG handler 不输出，f-string 也会先构造。部分日志还构造完整标签列表、映射字符串或较长内容片段。

### 优化方案

使用 logging 的惰性参数：

```python
logger.debug("处理图片: path=%s size=%d", image_path, image_size)
```

对昂贵诊断数据先检查级别：

```python
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("文档标签: %s", collect_tag_names(soup))
```

其他规则：

- 循环内默认只记录失败和阶段统计。
- 每 N 项输出一次进度，不逐项输出普通成功。
- 禁止记录 Base64、完整提示词、API Key 和大型映射。
- 用统一 logger 替换 `print()`，GUI 模式下控制台通常不可见。
- 对日志文件启用轮转，例如 `RotatingFileHandler`。

### 验收指标

- INFO 模式下不会构造大型 DEBUG 数据。
- 大型 EPUB 导入日志大小有明确上限。
- 错误仍保留足够上下文和堆栈。

## 13. PERF-009：只缓存语义完全一致的请求

### 现状

缓存对象默认创建，但主翻译路径通常直接调用流式接口，未稳定使用缓存。简单以原文为键还可能在模型、提示词或术语变化后返回过期译文。

### 建议缓存键

缓存键至少包含：

```text
source_text
target_language
provider
model_name
temperature
prompt_hash
glossary_hash
normalization_version
```

### 缓存层级

- L1：单次会话内存 LRU，命中最快。
- L2：可选持久缓存，SQLite 比大型 JSON 更适合。
- 不缓存失败、取消或空响应。
- 支持按模型、提示词或术语版本失效。

### 流式命中行为

命中缓存时无需创建后台字符模拟线程。可以：

- 立即返回完整结果；或
- 由 UI 以固定少量帧展示，不逐 3 字符模拟网络流。

### 验收指标

- 缓存命中不产生网络请求。
- 配置变化后不会命中过期语义结果。
- 缓存命中率和节省请求数可观测。
- 缓存维护成本低于节省的 API 延迟。

## 14. PERF-010：重新设计批次和并发

### 现状

当前 `BatchProcessor` 实际将每条请求分别提交线程池，没有按 `max_batch_size` 或 `max_wait_time` 聚合。翻译引擎则按固定行数分批，行长度差异可能导致请求大小极不稳定。

### 文本批次策略

优先按估算 token 或字符预算分批，并同时设置最大行数：

```text
max_lines = 20
max_input_budget = 模型上下文预算的一部分
max_output_budget = 预留译文和行号标记
```

要求：

- 单个超长段落单独处理或先安全切分。
- 不跨越必须保持上下文的章节边界。
- 计入提示词、术语表和行号标记开销。
- 根据实际输出截断或上下文错误调整预算。

### 并发策略

并发只应位于明确层级：

- 单个任务内部批次并发；或
- 多任务队列并发。

不要两层同时无界并发。建议维护一个全局 provider 限流器：

- 最大并发请求数。
- 每分钟请求数。
- 估算 token 速率。
- 429 后退避。

文学翻译存在上下文顺序要求，默认仍建议按顺序处理。只有独立章节或图片任务适合并行。

### 验收指标

- `max_batch_size`、等待时间和 priority 如果保留，必须有真实行为测试。
- 请求大小分布更稳定。
- 提高并发后吞吐有测量收益，错误率可接受。
- 输出顺序和段落对应关系保持稳定。

## 15. 内存生命周期优化

除图片 Base64 外，还应检查以下引用：

- 完成任务是否继续持有完整源文、译文、引擎和线程对象。
- 关闭详情窗口后是否取消定时刷新。
- API stream callback 字典是否在异常路径删除。
- 导出结束后 BeautifulSoup、book 和图片 bytes 是否仍被长期对象引用。
- 旧表格内容是否在导入新文件后释放。

建议：

- 队列任务完成后关闭 provider，并按需只保留结果路径和摘要。
- 详细源文和译文可以从项目仓库重新加载。
- 所有订阅返回 unsubscribe handle。
- 使用 `try/finally` 清理 stream callback 和临时资源。
- 不主动频繁调用 `gc.collect()`；先消除不必要引用。

验收时对同一操作连续执行 10 次，观察 RSS 是否趋于稳定，而不是每次持续增长。

## 16. 启动和打包性能

启动优化优先级低于翻译、EPUB 和 UI 热点，但可以检查：

- 主窗口显示前是否导入大型可选库。
- Pillow、ebooklib、OpenAI 等功能是否可以在首次使用时加载。
- PyInstaller 是否重复打包整个 `src` 数据目录和已编译模块。
- 无用依赖和 hidden import 是否增加体积及启动扫描。
- 构建产物是否包含不需要的调试工具和 workspace 数据。

注意：过度懒加载会把错误推迟到用户操作时。关键依赖应在启动后后台自检，并给出清晰错误。

## 17. 可观测性设计

建议定义轻量性能事件：

```python
@dataclass(frozen=True)
class PerformanceEvent:
    operation: str
    duration_ms: float
    item_count: int | None = None
    byte_count: int | None = None
    outcome: str = "success"
```

阶段名称建议：

- `epub.read`
- `epub.extract_segments`
- `epub.extract_images`
- `epub.write_mapping`
- `translation.first_chunk`
- `translation.batch`
- `persistence.checkpoint`
- `ui.render_progress`
- `image.detect`
- `image.generate`

默认仅输出汇总；诊断模式才记录单项事件。不要引入完整遥测平台作为首个优化步骤。

## 18. 推荐实施顺序

### 阶段 1：低风险高收益

- 改为增量流式行解析。
- UI 更新节流和批量渲染。
- 调试日志改为惰性参数。
- 移除热路径 `update_idletasks()` 和频繁 `see()`。

### 阶段 2：I/O 和内存

- 自动保存单飞化和 checkpoint。
- 图片从 Base64 JSON 迁移为二进制资源文件。
- EPUB item 单次分类和 DOM 单次解析。

### 阶段 3：网络与生命周期

- 修复并统一客户端 owner。
- 复用连接池。
- 选择全同步或全异步图片管线。
- 引入受控并发和限流。

### 阶段 4：语义优化

- 接入正确缓存键。
- 设计 token/字符预算批次。
- 评估真正的请求聚合或删除伪批处理。

### 阶段 5：大规模数据方案

只有基准证明 JSON 和 Treeview 在目标规模下仍不足时，再考虑：

- SQLite 项目仓库。
- 表格分页或虚拟化。
- 多进程图片转换。
- 持久化内容寻址资源库。

## 19. 不建议的优化

- 未测量就提高线程池大小。
- 在 Tk 主线程直接运行 CPU 或网络任务。
- 用 `except: pass` 减少错误处理开销。
- 为减少复制而向 UI 暴露可变内部列表。
- 缓存不包含模型、提示词和术语版本的译文。
- 在每个任务中创建独立事件循环、客户端和线程池。
- 用私有字段如 `ThreadPoolExecutor._max_workers` 作为业务接口。
- 为小型 JSON 过早引入复杂数据库迁移。
- 通过降低保存可靠性换取表面速度。

## 20. 完成定义

性能优化完成应满足：

- 所有结论都有固定 fixture 和前后基准。
- 长流式响应不再产生 O(n²) 历史文本复制。
- UI 更新有明确频率上限，最终状态事件不丢失。
- 自动保存不会并发重入或频繁整份写盘。
- 图片不再长期以 Base64 内嵌 JSON 保存。
- HTTP 客户端、线程池和事件循环数量稳定。
- async 代码中没有同步网络阻塞。
- EPUB 文档和 DOM 不做无必要重复遍历、解析。
- 缓存和批处理若保留，必须接入主路径并有正确性测试。
- 优化后全部功能测试、EPUB 往返测试和失败场景测试通过。
