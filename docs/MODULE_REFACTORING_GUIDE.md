# 模块拆分与重构指南

## 1. 重构目标

本次拆分的核心目标不是单纯缩短文件，而是建立清晰依赖方向，使以下能力可以脱离 Tkinter 独立测试：

- 文本翻译和重试流程。
- 任务队列、暂停、恢复和取消。
- EPUB 导入、映射、译文保存和导出。
- 配置与工作区持久化。
- API 提供商选择及资源生命周期。

目标约束：

- UI 不直接访问核心对象的私有属性。
- 核心逻辑不导入 `tkinter`、`messagebox` 或具体控件。
- 业务流程不依赖 `Path.cwd()` 或全局环境状态。
- API、文件系统和时间等外部依赖可以替换为测试实现。
- 不在一次提交中同时完成所有拆分。

## 2. 当前主要边界问题

### `EPUBProcessor`

当前类同时负责：

- 读取 EPUB。
- 解析 metadata、manifest、spine 和 TOC。
- 提取正文块。
- 生成和迁移映射 JSON。
- 提取 Base64 图片。
- 保存译文。
- 替换图片。
- 修改 HTML。
- 重建并写出 EPUB。

这些职责具有不同变化原因，应拆分。

### `TranslationController`

当前类同时负责：

- 读取和修改 Tk 表格。
- 启停线程。
- 翻译流程编排。
- 进度数据解释。
- 续译和查漏策略。
- 文件保存和 EPUB 导出。
- 弹窗与按钮状态。

它本质上同时承担 View、Presenter 和 Application Service 三种角色。

### `BaseAPI`

当前类同时负责：

- HTTP 客户端生命周期。
- 重试策略。
- 普通、流式和视觉请求。
- 缓存。
- 批处理。
- 流式回调注册。

缓存和批处理并未稳定进入主翻译路径，却增加了线程和状态复杂度。

### `ConfigManager`

当前类同时负责：

- 路径决策。
- 默认配置定义。
- JSON 读写。
- 配置迁移和合并。
- 术语库业务操作。
- API 预设。
- API Key 存储。

配置模型、持久化和密钥管理需要分开。

## 3. 推荐依赖方向

```text
presentation (Tkinter)
        |
        v
application (用例与流程编排)
        |
        v
domain (模型、状态、规则、端口)
        ^
        |
infrastructure (HTTP、EPUB、JSON、文件系统、密钥环)
```

规则：

- `domain` 不依赖其他项目层。
- `application` 只依赖 `domain` 中的模型和端口。
- `infrastructure` 实现 `domain/application` 定义的端口。
- `presentation` 调用 application service，并把事件映射到 Tk 控件。
- `bootstrap` 是唯一知道所有具体实现并负责组装的地方。

## 4. 建议目录结构

```text
src/
├── bootstrap.py
├── app_paths.py
├── domain/
│   ├── errors.py
│   ├── translation.py
│   ├── tasks.py
│   ├── epub.py
│   └── glossary.py
├── application/
│   ├── ports.py
│   ├── translation_service.py
│   ├── task_queue_service.py
│   ├── epub_project_service.py
│   └── image_translation_service.py
├── infrastructure/
│   ├── api/
│   │   ├── client.py
│   │   ├── provider_factory.py
│   │   ├── siliconflow.py
│   │   ├── deepseek.py
│   │   └── retry.py
│   ├── epub/
│   │   ├── archive_reader.py
│   │   ├── document_order.py
│   │   ├── segment_extractor.py
│   │   ├── mapping_repository.py
│   │   ├── image_rewriter.py
│   │   └── exporter.py
│   ├── persistence/
│   │   ├── atomic_file.py
│   │   ├── config_repository.py
│   │   ├── glossary_repository.py
│   │   ├── workspace_repository.py
│   │   └── secret_store.py
│   └── images/
│       ├── detector.py
│       └── generator.py
└── presentation/
    └── tk/
        ├── main_window.py
        ├── translation_presenter.py
        ├── queue_presenter.py
        ├── settings_presenter.py
        ├── views/
        └── widgets/
```

不必一开始创建全部文件。仅当迁移对应职责时再创建模块，避免空目录和占位抽象。

## 5. Domain 层设计

### 翻译模型

用结构化事件替代松散字典：

```python
from dataclasses import dataclass, field
from enum import Enum


class OperationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class TranslationOptions:
    target_language: str
    model_name: str
    batch_size: int = 20
    max_retries: int = 5


@dataclass(frozen=True)
class TranslationProgress:
    completed: int
    total: int
    batch_start: int
    preview_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class TranslationResult:
    status: OperationStatus
    lines: tuple[str, ...]
    failed_indices: tuple[int, ...] = ()
    error_message: str | None = None
```

事件对象必须明确字段含义，不允许 UI 猜测 `streaming` 字典中是否存在某个键。

### 任务模型

使用枚举替代字符串状态：

```python
class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
```

任务状态转换应集中验证，例如：

```text
PENDING -> RUNNING/CANCELLED
RUNNING -> PAUSED/COMPLETED/CANCELLED/FAILED
PAUSED  -> RUNNING/CANCELLED
```

禁止 UI 直接赋值 `task.status`。

### EPUB 模型

建议最小模型：

```python
@dataclass(frozen=True)
class SegmentLocator:
    document_href: str
    block_index: int
    source_checksum: str


@dataclass
class EpubSegment:
    segment_id: str
    locator: SegmentLocator
    source_text: str
    translated_text: str = ""
```

`line_number` 可以作为显示序号，但不应是唯一定位依据。

## 6. Application 层端口

端口应表达业务需要，而不是包装第三方库全部 API。

### 翻译提供商

```python
from collections.abc import Callable, Sequence
from typing import Protocol


class TranslationProvider(Protocol):
    def translate_batch(
        self,
        lines: Sequence[str],
        options: TranslationOptions,
        on_progress: Callable[[TranslationProgress], None] | None = None,
    ) -> TranslationResult: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...
```

应用服务只依赖此协议，不依赖 `httpx.Client` 或 SiliconFlow 类。

### 项目仓库

```python
class EpubProjectRepository(Protocol):
    def create_or_load(self, source_path: Path) -> EpubProject: ...
    def save_segments(self, project: EpubProject) -> None: ...
    def save_image_map(self, project_id: str, image_map: dict[str, str]) -> None: ...
```

### UI 调度器

Application 层不应调用 `root.after()`。Presentation 层提供调度：

```python
class UiScheduler(Protocol):
    def submit(self, callback: Callable[[], None]) -> None: ...
```

Tk 实现只需封装 `root.after(0, callback)`。

## 7. 翻译流程拆分

### `TranslationService`

职责：

- 验证请求参数。
- 分批。
- 调用 provider。
- 合并结果。
- 维护取消令牌。
- 发送结构化进度事件。
- 返回最终状态。

不负责：

- 修改 Treeview。
- 弹出对话框。
- 决定文件保存位置。
- 读取全局配置。

### `TranslationPresenter`

职责：

- 从 View 读取用户输入。
- 调用 `TranslationService`。
- 把进度事件映射到表格。
- 根据最终状态恢复按钮和显示消息。

不要让 Presenter 实现重试或分批算法。

### 查漏策略

将查漏提取成纯函数或策略对象：

```python
def find_missing_indices(source: Sequence[str], target: Sequence[str]) -> list[int]:
    ...
```

自动补译次数和终止条件由 `TranslationService` 管理，UI 只展示结果。

## 8. EPUB 模块拆分

### `archive_reader.py`

职责：读取 EPUB、返回 metadata、spine 文档和资源列表。

### `document_order.py`

职责：提供唯一的 `iter_spine_documents()`。导入和导出必须共用。

### `segment_extractor.py`

职责：

- 定义块级标签选择规则。
- 从文档生成 `EpubSegment` 和稳定定位器。
- 根据定位器重新找到 DOM 节点。

导入和导出不能复制两份 DOM 遍历算法。

### `mapping_repository.py`

职责：

- 读写映射格式。
- 原子保存。
- 格式版本管理。
- 旧格式迁移。

建议在 JSON 顶层加入：

```json
{
  "schema_version": 2,
  "project": {},
  "segments": []
}
```

### `image_rewriter.py`

职责：处理新图片 item、HTML 图片引用和 figcaption。不要混入正文翻译替换。

### `exporter.py`

职责：协调 archive reader、segment locator 和 image rewriter，最终写出 EPUB。

## 9. 配置与持久化拆分

### 配置模型

使用 dataclass 表示有效配置，并集中验证：

```python
@dataclass(frozen=True)
class ApiSettings:
    provider: str
    model_name: str
    base_url: str
    max_tokens: int
    temperature: float
```

配置模型不负责文件读写。

### `ConfigRepository`

职责：加载、合并默认值、验证、原子保存、迁移 schema。

### `GlossaryRepository`

术语增删改查不应放在通用配置管理器中。术语表可以独立版本化和测试。

### `SecretStore`

只负责 Key 的保存、读取和删除。优先实现系统 keyring；环境变量作为回退。

### `AppPaths`

只在启动阶段创建一次并注入各仓库。任何深层模块都不应再调用 `Path.cwd()` 决定持久化位置。

## 10. API 层拆分

### 保留的公共逻辑

- OpenAI 兼容请求体构建。
- 流式响应解析。
- 状态码到领域错误的映射。
- 统一超时和有限重试。
- `close()` 生命周期。

### 提供商差异

- 默认 URL 和模型。
- HTTP/2 或 transport 配置。
- 视觉能力支持。
- 提供商特有错误格式。

### 重试策略

将重试提取为可测试策略，至少区分：

- 401/403：不重试。
- 400/404 模型错误：不重试。
- 429：按 `Retry-After` 或退避重试。
- 连接错误和超时：有限重试。
- 用户取消：立即停止，不算失败重试。

缓存只有在键包含以下信息时才可启用：

- 原文。
- 目标语言。
- provider 和 model。
- temperature。
- 提示词版本。
- 术语表版本。

如果短期内不实现真实批处理，应删除 `BatchProcessor`，不要保留无效接口。

## 11. 队列服务拆分

将 `ConcurrentTranslationManager` 更名为与实际行为一致的 `TaskQueueService`。

服务职责：

- 按顺序管理任务。
- 验证状态转换。
- 控制最大并发数。
- 为每个运行任务持有取消令牌和 provider 生命周期。
- 发布不可变任务快照。

UI 只调用公开方法：

```text
add_task
start_task
pause_task
resume_task
cancel_task
remove_task
list_tasks
subscribe
close
```

不要暴露 `_epub_processor`、`_engines` 或可直接修改的任务对象。`list_tasks()` 应返回副本或不可变 snapshot。

导出和图片翻译属于独立 application service，不应通过访问队列私有字段完成。

## 12. Tkinter 层约束

- 所有 Tk 控件只能在主线程操作。
- 工作线程只产生领域事件或结果。
- 所有 `after()` 回调都应绑定不可变局部值。
- 窗口销毁后取消定时刷新和订阅。
- Presenter 不直接访问其他 Presenter 的私有方法。
- View 只暴露明确操作，例如 `set_progress()`、`replace_rows()`、`show_error()`。

建议为主窗口定义小型 View 协议，测试时使用内存假实现，无需启动真实 Tk。

## 13. 渐进迁移顺序

### 阶段 0：建立安全网

- 新建 `tests/`。
- 加入 pytest、Ruff 和最小 CI。
- 为当前关键行为写 characterization tests。
- 准备两个最小 EPUB fixture。

此阶段不改变业务行为。

### 阶段 1：路径和持久化

- 引入 `AppPaths`。
- 引入原子文件写入。
- 让 `ConfigManager` 暂时接收路径参数。
- 修复打包配置迁移。

完成后再拆 `ConfigRepository`。

### 阶段 2：领域结果和异常

- 引入状态枚举、结果和进度 dataclass。
- 保留旧回调接口的适配器。
- 修复失败、取消和完成语义。
- 给自动查漏增加终止条件。

### 阶段 3：API 生命周期

- 定义 `TranslationProvider`。
- 给现有 API 类增加适配器和幂等 `close()`。
- 移除无用批处理器，或实现真实批处理。
- `TranslatorEngine` 逐步缩减为 application service。

### 阶段 4：EPUB 拆分

- 先提取 `document_order` 和 `segment_extractor`。
- 再提取 `mapping_repository`，加入 schema 迁移。
- 然后提取 `image_rewriter`。
- 最后让 `EPUBProcessor` 变成临时 facade，内部委托新模块。
- 所有调用方迁移完成后删除 facade。

### 阶段 5：队列服务

- 引入任务状态机和快照。
- 把线程、取消和状态转换移入 `TaskQueueService`。
- 把导出功能移到 `EpubProjectService`。
- UI 不再访问 manager 私有字段。

### 阶段 6：UI Presenter

- 先迁移翻译进度和结果处理。
- 再迁移保存、导出和查漏。
- 最后缩减 `MainWindow`，只保留布局和事件绑定。

### 阶段 7：删除兼容层

- 删除无效配置项和旧回调字典。
- 删除不再使用的 facade。
- 更新 README、构建脚本和示例配置。

## 14. 每阶段提交要求

每个重构提交应满足：

- 有明确的旧模块到新模块迁移范围。
- 不夹带无关格式化。
- 新旧行为有测试对照。
- 临时兼容层带删除条件或跟踪项。
- `python -m compileall` 通过。
- 单元测试通过。
- 最小 EPUB 往返测试通过。
- 不增加新的跨层反向依赖。

## 15. 建议的第一批 PR

### PR 1：错误语义和回调修复

- 修复异常变量延迟绑定。
- 增加翻译状态枚举。
- 失败不再返回全空结果。
- 限制自动查漏次数。

### PR 2：路径和原子保存

- 引入 `AppPaths`。
- 使用用户数据目录。
- 引入 `atomic_file.py`。
- 修复保存成功判断。

### PR 3：EPUB 项目标识

- 新项目 ID。
- 同名 EPUB 隔离。
- 映射 schema v2。
- 重复原文定位和旧格式迁移。

### PR 4：API 生命周期

- Provider 协议。
- `close()` 和上下文管理。
- 测试连接释放资源。
- 删除或重做伪批处理。

### PR 5：EPUB 解析与导出拆分

- spine 文档迭代器。
- segment extractor。
- mapping repository。
- exporter。

### PR 6：Tk Presenter

- TranslationPresenter。
- QueuePresenter。
- View 协议和 UI 测试替身。

## 16. 完成定义

模块拆分完成应满足以下条件：

- `domain` 和 `application` 测试不需要导入 Tkinter。
- 翻译流程可以使用 fake provider 完整测试。
- EPUB 导入导出可以使用 fixture 完整测试。
- UI 不访问任何核心对象的下划线私有成员。
- 所有后台资源都有清晰 owner 和 `close()` 路径。
- 配置、工作区和日志位置不依赖当前工作目录。
- 所有用户可见配置确实影响行为。
- README、示例配置和版本号与实现一致。
