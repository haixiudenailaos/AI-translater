# Bug 修复指南

## 1. 文档目的

本文档记录当前项目中已确认的主要缺陷，并给出建议修复顺序、实现要点、回归范围和验收标准。

修复时应遵守以下原则：

- 先修复行为和数据正确性，再进行模块搬迁。
- 一个提交只处理一个缺陷或一组强相关缺陷。
- 不用 `except Exception: pass` 掩盖关键路径错误。
- 所有后台任务必须把成功、取消和失败作为不同结果返回。
- 所有持久化操作必须能向调用方明确报告失败。
- 涉及 EPUB 映射格式的修改必须考虑旧工作区迁移。

## 2. 建议优先级

| 编号 | 优先级 | 问题 | 主要风险 |
| --- | --- | --- | --- |
| BUG-001 | P0 | 打包配置目录未被实际使用 | 配置丢失、目录无权限、不同启动目录产生不同数据 |
| BUG-002 | P0 | Tk 延迟回调引用已清除的异常变量 | 原始异常不显示、界面状态无法恢复 |
| BUG-003 | P0 | 同名 EPUB 共用映射目录 | 跨书覆盖、译文串书、任务互相污染 |
| BUG-004 | P0 | API 失败被当作空译文并继续完成流程 | 空结果被保存、无限查漏、错误完成状态 |
| BUG-005 | P1 | API 客户端、心跳线程和线程池未统一释放 | 连接和后台线程累积、退出不干净 |
| BUG-006 | P1 | 保存失败被吞掉或仍提示成功 | 用户误以为数据已落盘 |
| BUG-007 | P1 | EPUB 阅读顺序没有真正使用 spine | 章节顺序错误、翻译上下文错位 |
| BUG-008 | P2 | 配置项和翻译模式部分无效 | 行为与界面不一致、无效资源开销 |
| BUG-009 | P2 | API Key 明文存储且预设重复保存 | 本地密钥暴露风险 |

## 3. BUG-001：统一应用路径

### 现状

- `hooks/runtime_hook_resources.py` 设置了 `APP_CONFIG_DIR`。
- `ConfigManager` 仍使用 `Path("config")`。
- EPUB 工作区使用 `Path.cwd() / "workspace"`。
- 日志使用 `Path("logs")`。
- 图标等资源也依赖当前工作目录。

当前工作目录不是稳定的应用路径。打包程序可能从桌面、下载目录、快捷方式工作目录或只读目录启动。

### 修复方案

新增唯一的路径解析模块，例如 `src/app_paths.py`，集中提供：

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    resource_dir: Path
    data_dir: Path
    config_dir: Path
    workspace_dir: Path
    log_dir: Path
```

路径规则建议：

- 开发环境资源：项目根目录。
- PyInstaller 资源：`sys._MEIPASS`。
- Windows 用户数据：优先 `%APPDATA%/LightNovelTranslator`。
- macOS 用户数据：`~/Library/Application Support/LightNovelTranslator`。
- Linux 用户数据：`${XDG_DATA_HOME:-~/.local/share}/LightNovelTranslator`。
- 测试环境允许显式注入临时目录。

`ConfigManager`、`EPUBProcessor` 和日志初始化都通过构造参数接收 `AppPaths`，不要在业务类内部重新计算路径。

### 兼容迁移

首次运行新版本时：

1. 如果新配置目录为空，检查旧的 `./config`。
2. 复制用户配置，不覆盖新目录中已存在文件。
3. 不迁移构建时的示例 API Key。
4. 迁移完成后写入版本标记，例如 `migration_state.json`。

### 验收标准

- 从任意当前工作目录启动都读取同一份用户配置。
- 打包程序不尝试写入 `_MEIPASS` 或可执行文件所在目录。
- 配置、日志和工作区路径可在测试中替换为临时目录。
- 开发模式和打包模式使用相同的路径接口。

## 4. BUG-002：修复后台异常回调

### 现状

以下形式在回调真正执行时会抛出 `NameError`：

```python
except Exception as e:
    root.after(0, lambda: show_error(str(e)))
```

Python 会在离开 `except` 代码块后清除异常变量 `e`。

### 最小修复

在注册回调前把消息绑定为普通局部值：

```python
except Exception as exc:
    error_message = str(exc)
    root.after(0, lambda message=error_message: show_error(message))
```

建议同时记录完整堆栈：

```python
logger.exception("后台翻译失败")
```

### 涉及位置

- `src/ui/translation_controller.py`
- `src/ui/settings_window.py`
- `src/ui/image_translation_handler.py`

### 验收标准

- 人工制造网络异常时，界面显示原始错误而不是 `NameError`。
- 失败后翻译按钮、继续按钮和停止按钮恢复正确状态。
- 日志中包含异常类型、消息和堆栈。
- 窗口已关闭时，后台回调不会再次操作失效的 Tk 控件。

## 5. BUG-003：隔离 EPUB 项目并修复译文复用

### 现状

映射目录仅由 `epub_path.stem` 决定。不同目录中的同名 EPUB 会共享目录。

旧译文按 `original_text` 建立字典。书中重复出现的短句可能需要不同翻译，但当前逻辑只能保存其中一条。

### 修复方案

为 EPUB 建立稳定项目 ID：

```text
<安全文件名>-<源文件绝对路径哈希前 12 位>
```

如果需要在文件移动后仍识别同一本书，可再保存文件内容哈希，但不要每次启动都对大型 EPUB 全量计算。推荐组合：

- 首次导入：规范化绝对路径哈希作为目录 ID。
- 项目元数据：保存源文件大小、修改时间和可选内容哈希。
- 用户重新选择移动后的文件时，提供显式“重新关联源文件”操作。

每个文本段应具有稳定定位符，例如：

```text
chapter_href + DOM block index + source checksum
```

旧译文迁移优先按稳定定位符匹配；缺少定位符的旧数据可以按以下顺序降级：

1. 章节 ID、段落序号和原文同时相同。
2. 全书中原文唯一时按原文匹配。
3. 无法唯一匹配时保留为空并记录迁移警告。

禁止仅使用原文文本作为全局唯一键。

### 验收标准

- 同时导入两个不同目录下的 `book.epub`，生成不同工作区。
- 一本书中两个相同原文段可以保存不同译文。
- 重新导入同一本未变化的书能恢复对应译文。
- EPUB 内容变化时不会把旧译文静默写入错误段落。

## 6. BUG-004：建立明确的翻译结果语义

### 现状

API 多次失败后返回 `[''] * expected_lines`。调用方无法区分：

- 原文确实应该翻译为空；
- API 没有返回内容；
- 用户取消；
- 部分行成功；
- 整批失败。

主窗口随后启动自动查漏，可能无限重复；队列模式则可能标记任务完成。

### 修复方案

短期方案：重试耗尽后抛出明确异常，例如 `TranslationRequestError`，不要返回空列表。

推荐方案：返回结构化结果：

```python
from dataclasses import dataclass, field
from enum import Enum


class TranslationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class BatchTranslationResult:
    status: TranslationStatus
    lines: list[str]
    failed_indices: list[int] = field(default_factory=list)
    error_message: str | None = None
```

状态规则：

- 所有必需行成功才是 `SUCCEEDED`。
- 部分成功为 `PARTIAL`，必须保留成功结果并向用户提示。
- 用户停止为 `CANCELLED`，不能显示失败或完成。
- 重试耗尽为 `FAILED`，不能调用完成回调。

自动查漏必须增加上限：

- 单行或单批最多补译指定次数，例如 2 次。
- 记录失败索引及最后一次错误。
- 达到上限后停止自动循环，并允许用户手工重试。

### 验收标准

- HTTP 401、429、超时、空响应分别产生可识别状态。
- 失败任务不会变成 `completed`。
- 取消任务不会触发完成提示或自动查漏。
- 永久失败不会产生无限后台线程或无限请求。
- 部分成功结果可以保存，同时明确标记未完成行。

## 7. BUG-005：统一资源生命周期

### 现状

- `BaseAPI.reset_cancel()` 丢弃客户端引用但没有保证关闭。
- `TranslatorEngine.refresh_api()` 覆盖旧 API 实例。
- 设置窗口测试连接后不关闭 API。
- `BatchProcessor` 没有关闭线程池的方法。
- `SiliconFlowAPI` 创建心跳线程，但上层通常不调用 `close()`。

### 修复方案

所有资源持有者实现幂等 `close()`：

```python
class TranslatorEngine:
    def close(self) -> None:
        if self.api is not None:
            self.api.close()
            self.api = None
```

约束：

- `refresh_api()` 先关闭旧实例，再创建新实例。
- `BaseAPI.close()` 同时关闭 HTTP 客户端和批处理线程池。
- `BatchProcessor.close()` 调用 `executor.shutdown(wait=False, cancel_futures=True)`。
- 临时连接测试使用 `try/finally` 关闭。
- 主窗口关闭时停止翻译、关闭所有引擎，再销毁 Tk 根窗口。
- `reset_cancel()` 只重置取消标记，不承担客户端重建职责。

### 验收标准

- 连续点击连接测试 20 次后，线程数量回到稳定值。
- 连续切换 API 配置不会累积心跳线程。
- 应用退出后没有非预期后台工作继续访问网络。
- `close()` 可安全调用多次。

## 8. BUG-006：让持久化失败可见

### 现状

`FileHandler.write_file()` 捕获异常后返回 `False`，但调用方经常忽略返回值。自动保存还会吞掉全部异常。

### 修复方案

推荐让底层写入失败直接抛出具体异常，由 UI 边界负责展示：

```python
def write_text_atomic(path: Path, content: str) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)
```

需要补充：

- 临时文件必须与目标文件位于同一目录，保证替换尽量原子化。
- JSON 配置、EPUB 映射和普通译文统一使用原子写入。
- 写入失败保留原文件，不显示成功。
- 自动保存失败应更新状态栏并写日志，但避免反复弹窗。
- 手工保存失败应显示对话框。

### 验收标准

- 模拟只读目录时不显示“保存成功”。
- JSON 写入中断时旧文件仍可读取。
- 自动保存失败状态可见且不会造成弹窗风暴。
- 队列任务只有在结果保存成功后才能进入 `completed`。

## 9. BUG-007：按 EPUB spine 处理阅读顺序

### 现状

代码提取并保存了 `spine_order`，但正文提取和导出替换仍主要遍历 `book.get_items()`。Manifest 顺序不保证等于阅读顺序。

### 修复方案

新增统一的 `iter_spine_documents(book)`：

1. 遍历 `book.spine`。
2. 解析 `idref` 并取得对应文档对象。
3. 忽略非线性或不存在的项目时记录警告。
4. 导入和导出必须使用同一个迭代器和同一个块节点选择器。

不要分别维护两套“看起来相同”的 DOM 遍历逻辑。正文定位器应由一个共享组件生成。

### 验收标准

- 使用 manifest 顺序和 spine 顺序不同的 EPUB 验证 UI 章节顺序。
- 导入后不修改译文直接导出，阅读顺序与原书一致。
- 每个映射段在导出时都能定位到原始 DOM 块。

## 10. BUG-008：清理无效配置和伪功能

### 已确认问题

- `context_lines` 当前没有进入翻译提示构造。
- `chunk_size` 没有实际消费者。
- `auto_save` 开关没有控制主窗口自动保存。
- 逐行模式和快速模式使用同一流程，仅延迟不同，且快速模式延迟更长。
- 缓存默认创建，但主翻译路径调用 `translate_stream()`，绕过缓存接口。
- `BatchProcessor` 当前逐条提交，没有真正聚合批次；`priority`、`flush_pending()` 等参数无效。

### 处理原则

对每个配置项二选一：

- 实现并添加测试；或
- 从 UI、默认配置、文档和运行时代码中完整删除。

不要保留只影响显示、不影响行为的开关。

建议定义清晰模式：

- `standard`：稳定批大小、完整进度和重试。
- `fast`：更大批次或更高并发，但明确成本和限制。
- 如果两种模式没有真实行为差异，只保留一种。

### 验收标准

- 每个界面配置都有至少一个行为测试。
- README 描述与代码行为一致。
- 禁用自动保存后不发生后台写入。
- 缓存键包含模型、目标语言、提示词版本和术语表版本。

## 11. BUG-009：改进密钥存储

### 现状

API Key 会写入 JSON，预设中还会再次保存。`.gitignore` 只能防止误提交，不能保护本机密钥。

### 修复方案

- 优先使用系统密钥环，例如 `keyring`。
- 配置文件只保存密钥引用或提供商名称。
- 环境变量作为无桌面密钥环时的替代方案。
- 不在日志、异常消息和调试输出中记录完整 Key。
- 旧明文配置迁移成功后删除密钥字段，并原子写回。

### 验收标准

- 普通配置 JSON 中不出现完整 API Key。
- 日志搜索不到 Key。
- 从旧版本升级后仍可使用原有配置。

## 12. 测试补充计划

建议先建立以下测试，再进行大规模拆分：

### 单元测试

- 路径解析：开发、打包、环境变量和临时目录。
- 配置合并、损坏 JSON、原子写入失败。
- 翻译结果状态：成功、部分成功、取消、失败。
- 重试次数和自动查漏上限。
- EPUB 项目 ID 和重复原文定位。
- API `close()` 幂等性。

### 集成测试

- 使用本地假 HTTP 服务模拟流式响应、401、429、超时和断流。
- 使用最小 EPUB fixture 做导入、保存译文和重新导出。
- 两个同名 EPUB 的工作区隔离。
- Manifest 与 spine 顺序不一致的 EPUB。

### UI 测试

- 后台失败后按钮状态恢复。
- 用户取消后不弹出完成提示。
- 窗口关闭时丢弃迟到回调。
- 自动保存开关真实生效。

## 13. 推荐实施批次

1. 修复异常回调、错误完成状态和无限查漏。
2. 引入统一应用路径并迁移旧配置。
3. 修复 EPUB 项目标识、段落定位和 spine 顺序。
4. 统一原子保存及错误传播。
5. 补齐 API 和线程池关闭路径。
6. 清理无效配置、缓存和批处理行为。
7. 迁移 API Key 到系统密钥环。

每一批都应独立通过语法检查、单元测试和最小 GUI 冒烟测试。
