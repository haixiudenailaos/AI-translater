# Python 架构审查与重构实施计划

## 1. 文档目的

本文档记录当前项目的 Python 工程实践与模块边界审查结果，并将改动建议整理为可分阶段实施、验证和回滚的重构计划。

本次重构的核心目标不是单纯缩短文件，而是建立稳定的依赖方向，使翻译、EPUB、配置、图片翻译和持久化流程可以脱离 Tkinter 独立测试。

审查日期：2026-07-15。

## 2. 当前结论

项目已经具备以下良好基础：

- `domain` 中使用 dataclass、枚举和不可变结果表达业务状态。
- `application/ports.py` 已开始使用 Protocol 隔离外部实现。
- `AppPaths` 统一处理资源目录和用户数据目录。
- 配置、项目和映射数据开始使用原子写入。
- 已建立翻译取消、稀疏翻译、项目仓储、图片翻译和安全存储等专项测试。

当前主要问题是新旧架构并存。目录已经出现 `domain/application/infrastructure` 分层，但主程序仍主要通过 `ui -> core -> api` 运行。部分新应用服务只在测试中使用，没有接入真实 UI 流程，因此目前属于过渡架构，而不是已经完成的分层架构。

## 3. 目标依赖方向

```text
main.py / bootstrap.py
        |
        v
presentation (Tkinter UI)
        |
        v
application (用例、编排、端口)
        |
        v
domain (模型、状态、规则、错误)
        ^
        |
infrastructure (HTTP、EPUB、JSON、密钥环、文件系统)
```

约束：

- `domain` 不依赖项目中的其他层，也不依赖 Tkinter、HTTP 或文件系统。
- `application` 只依赖 `domain` 和本层定义的 Protocol。
- `infrastructure` 实现 application 定义的端口。
- `presentation` 只触发用例并渲染结构化结果，不直接编排 HTTP、EPUB 或持久化细节。
- `bootstrap.py` 是唯一了解所有具体实现并负责依赖组装的位置。
- 兼容层可以临时存在，但必须有明确删除条件，不能成为第二套长期架构。

## 4. 优先级问题与改动建议

### P1-1 图片翻译持久化失败被误报为成功

位置：`src/application/image_translation_service.py`。

现状：

- `_write_manifest()` 捕获并吞掉所有写入异常。
- `translate()` 在 manifest 写入失败后仍返回 Provider 的成功结果。
- 磁盘满、权限错误或原子替换失败时，UI 可能显示成功，但结果没有保存。

改动：

1. 定义 `ImageManifestRepository` Protocol，并通过构造函数注入。
2. manifest 保存失败必须向上传递结构化持久化异常。
3. Provider 执行成功但结果保存失败时，不得返回 `SUCCEEDED`。
4. UI 显示“翻译已完成，但结果保存失败”，并提供重试保存入口。
5. 增加仓储写入失败、旧 manifest 不被误用和重试保存测试。

验收标准：

- 任意 manifest 写入异常都不会被记录后静默忽略。
- UI 成功状态与磁盘持久化状态一致。
- 写入失败后旧文件保持完整，且用户可以重试。

### P1-2 API Key 未持久化时仍提示保存成功

位置：`src/config/config_manager.py`、`src/ui/settings_window.py`。

现状：

- `save_api_config()` 忽略 `store_key()` 返回的 `PERSISTED / SESSION_ONLY / FAILED`。
- keyring 不可用时，密钥只保存在当前进程环境变量中。
- 设置窗口仍显示“设置已保存”，重启后密钥会丢失。

改动：

1. 使用 `SecretSaveResult` 返回密钥保存状态、配置文件保存状态和错误信息。
2. `FAILED` 时配置保存操作整体失败，不关闭设置窗口。
3. `SESSION_ONLY` 时允许继续当前会话，但明确提示“密钥未持久化，重启后需重新输入”。
4. 将 `SecretStore` 定义为 Protocol，keyring 实现移动到 infrastructure。
5. 增加 keyring 不可用、写入失败、回读不一致和空密钥删除测试。

验收标准：

- UI 不会把 `SESSION_ONLY` 表述为永久保存成功。
- `FAILED` 不会更新内存配置为看似可用的状态。
- 日志和返回对象均不包含密钥明文。

### P2-1 主翻译流程仍使用旧巨型控制器

位置：`src/ui/main_window.py`、`src/ui/translation_controller.py`、`src/core/translator.py`。

现状：

- `SparseLineTranslator` 和 `TranslatorEngineAdapter` 主要只被测试使用。
- UI 仍直接持有 `TranslatorEngine`、`EPUBProcessor` 和 `FileHandler`。
- 开始翻译、续翻、选中翻译和查漏在 UI 控制器中分别维护状态与索引映射。

改动：

1. 新建 `TranslationService`，统一以下用例：
   - 翻译未完成行；
   - 翻译指定行；
   - 重试失败行；
   - 取消当前任务。
2. 复用 `SparseLineTranslator` 处理所有稀疏索引映射。
3. UI 只提交 `TranslationCommand`，接收 `TranslationProgress` 和 `TranslationResult`。
4. 将线程创建和生命周期管理移入统一的任务执行器。
5. 接入新流程后删除控制器中旧的续翻、选中翻译和自动查漏重复实现。

验收标准：

- 生产代码实际调用 `TranslationService` 和 `SparseLineTranslator`。
- UI 不再拼接待翻译文本并自行维护全局索引映射。
- 四种翻译入口共享同一套取消、失败和部分成功语义。

### P2-2 Application 层直接依赖 Infrastructure

位置：`src/application/image_translation_service.py`。

现状：

- 应用服务直接导入 `ManifestRepository`、具体 Registry 和全局 `get_registry()`。
- 依赖方向与 `application/ports.py` 中的设计说明不一致。

改动：

1. 在 `application/ports.py` 增加：
   - `ImageManifestRepository`；
   - `ImageProviderRegistry`；
   - 必要时增加 `Clock` 和 `IdGenerator`。
2. `ImageTranslationService` 构造函数只接收 Protocol。
3. 具体 Registry、manifest repository、系统时间和 UUID 在 `bootstrap.py` 中注入。
4. 移除应用层对 `get_registry()` 全局状态的依赖。

验收标准：

- `src/application` 不再导入 `src/infrastructure`。
- 应用服务测试无需创建真实文件或使用全局注册表。
- 多个窗口或任务之间不会意外共享 Provider 注册状态。

### P2-3 UI 跨对象访问私有成员

位置示例：

- `ConcurrentWindow` 读取 `manager._epub_processor`。
- `SettingsWindow` 调用 `config_manager._get_default_prompt()`。
- `MainWindow` 读取其他窗口的 `_closed`，并调用 `_on_close()`。

改动：

1. 增加公开接口：`is_closed`、`close()`、`default_prompt`、`export_task()`。
2. 窗口生命周期由统一的 `WindowRegistry` 或主窗口负责。
3. 队列导出通过 application service 完成，不从 manager 中取私有 EPUB 对象。
4. 增加静态检查规则，禁止跨模块访问下划线开头成员。

验收标准：

- 生产代码不存在 `other_object._private_member` 形式的跨对象调用。
- 子窗口可以独立替换或测试，不要求了解主窗口内部状态。

### P2-4 超长类和方法职责过多

当前主要热点：

- `src/ui/translation_controller.py`：约 955 行。
- `src/ui/settings_window.py`：约 919 行。
- `src/ui/main_window.py`：约 906 行。
- `src/core/translator.py`：约 693 行。
- `src/api/base_api.py`：约 669 行。
- `src/config/config_manager.py`：约 625 行。
- `EPUBProcessor.import_epub()`：约 442 行。

建议拆分：

| 当前模块 | 建议模块 |
| --- | --- |
| `translation_controller.py` | `translation_presenter.py`、`translation_view_state.py`、`tk_progress_renderer.py` |
| `settings_window.py` | `api_settings_panel.py`、`translation_settings_panel.py`、`image_settings_panel.py` |
| `main_window.py` | `main_view.py`、`editor_controller.py`、`window_registry.py` |
| `translator.py` | `translation_service.py`、`batch_planner.py`、`response_aligner.py` |
| `base_api.py` | `http_client.py`、`retry_policy.py`、`stream_decoder.py`、`api_metrics.py` |
| `config_manager.py` | `app_config_repository.py`、`api_profile_repository.py`、`glossary_repository.py`、`secret_store.py` |
| `epub_processor.py` | `epub_import_service.py`、`archive_reader.py`、已有 mapping/exporter 基础设施模块 |

拆分原则：

- 一个模块只因一种业务原因变化。
- 拆分后优先传递领域对象，不传递松散字典。
- 不为了减少行数创建仅转发调用的空壳类。
- 每次只迁移一条真实运行链路，迁移完成后删除旧实现。

### P3-1 两套原子写实现并存

位置：`src/utils/file_handler.py`、`src/infrastructure/atomic_file.py`。

现状：

- 两处均定义 `write_json_atomic()`。
- Windows 文件占用重试、临时文件清理和异常类型不完全一致。

改动：

1. 统一保留 `infrastructure/atomic_file.py` 作为唯一底层实现。
2. 将 Windows 瞬态占用重试合并到统一实现。
3. 文本、JSON 和 bytes 使用同一个临时文件与 replace 策略。
4. 迁移所有调用方后删除 `utils.file_handler.write_json_atomic`。

验收标准：

- 项目中只有一个 `write_json_atomic` 定义。
- 并发写、替换失败、临时文件清理和 Windows 占用重试均有测试。

### P3-2 工程质量门禁不足

现状：

- 缺少统一的 `pyproject.toml`。
- 没有 Ruff、类型检查和 pre-commit 配置。
- CI 只构建，不运行测试和静态检查。
- 运行依赖与测试依赖混合，版本只有下限，没有可复现锁定。
- 当前 `tests/` 目录尚未被 Git 跟踪。

改动：

1. 新增 `pyproject.toml`，集中配置 Ruff、pytest 和类型检查工具。
2. 将依赖分为运行依赖、开发依赖和可选图片翻译依赖。
3. 为发布构建生成锁定依赖或 constraints 文件。
4. CI 顺序调整为：
   - 安装依赖；
   - Ruff lint/format check；
   - 类型检查；
   - pytest；
   - PyInstaller 构建；
   - 最小启动冒烟测试。
5. 将全部测试文件纳入版本控制。

建议初始门禁：

```bash
python -m ruff check src tests main.py
python -m ruff format --check src tests main.py
python -m pytest -q
```

类型检查可先覆盖 `domain` 和 `application`，待旧模块注解补齐后再扩大范围。

### P3-3 异常处理范围过宽

当前生产代码中存在大量 `except Exception`。UI 边界、后台任务入口和资源清理处允许宽捕获，但业务分支和仓储层不应使用宽捕获掩盖错误。

改动：

1. 仓储层捕获 `OSError`、`JSONDecodeError` 等具体异常，并转换为领域可识别异常。
2. HTTP 层区分连接、超时、限流、鉴权和响应格式错误。
3. 资源清理可以宽捕获，但必须记录上下文且不得改变主操作状态。
4. 禁止 `except Exception: pass`，除非代码注释说明该操作确实是非关键最佳努力清理。

## 5. 分阶段实施计划

### 阶段 0：恢复可验证环境

任务：

- 重建 `.venv`，确保其不依赖已经失效的 Python 安装路径。
- 使用项目声明的 Python 3.11 运行测试。
- 将测试文件纳入版本控制。
- 记录当前测试基线和失败项。

完成条件：

- `python -m pytest -q` 可以稳定执行。
- CI 至少运行现有测试。

### 阶段 1：修复两个数据一致性问题

任务：

- 修复 manifest 写入失败仍返回成功。
- 修复密钥会话级保存被显示为永久成功。
- 补充相应失败路径测试。

完成条件：

- P1 问题全部具备回归测试。
- UI 状态与实际持久化状态一致。

### 阶段 2：建立 Composition Root

任务：

- 新建 `src/bootstrap.py`。
- 在其中创建配置仓储、SecretStore、HTTP Provider、EPUB 服务和图片 Provider。
- `main.py` 只负责创建 Tk root、调用 bootstrap 和启动事件循环。
- 移除 application service 内部的全局 Registry 获取逻辑。

完成条件：

- 除 bootstrap 外，没有模块同时了解 UI、application 和具体 infrastructure 实现。

### 阶段 3：迁移文本翻译主链路

任务：

- 接入 `TranslationService + SparseLineTranslator + TranslationProvider`。
- 用领域进度事件替换松散的 `batch_data` 字典。
- 将开始、续翻、选中翻译和失败重试统一为同一用例。
- 删除已被替代的控制器分支。

完成条件：

- 主 UI 不直接调用 `TranslatorEngine.translate_fast_mode()`。
- 生产代码使用新应用服务。
- 取消、部分成功和失败索引测试覆盖所有入口。

### 阶段 4：拆分配置与 EPUB

任务：

- 拆分 `ConfigManager` 的配置、术语库、预设和密钥职责。
- 将 `EPUBProcessor` 收缩为兼容门面。
- 统一原子文件实现。
- 逐步将导入和导出流程迁入 application service。

完成条件：

- 配置和 EPUB 流程可在无 Tkinter 环境下测试。
- `EPUBProcessor.import_epub()` 不再包含完整解析、映射和持久化流程。

### 阶段 5：收敛 UI 与删除兼容代码

任务：

- 拆分 MainWindow、SettingsWindow 和 TranslationController。
- 消除跨对象私有成员访问。
- 删除只为旧链路服务的 Adapter、状态字段和重复实现。
- 更新 README 和架构文档。

完成条件：

- UI 只负责输入收集、命令触发和结果渲染。
- 新旧双路径不再并存。
- 文档中的依赖方向与实际 import 关系一致。

## 6. 测试策略

### 单元测试

- `domain`：状态转换、索引规则、序列化兼容。
- `application`：使用 Fake Provider、内存仓储和 Fake Clock 验证用例。
- `infrastructure`：临时目录、模拟 HTTP transport、密钥环替身。

### 集成测试

- EPUB 导入 -> 翻译映射 -> 保存 -> 导出。
- 图片翻译 -> manifest 保存 -> EPUB 图片替换。
- 配置保存 -> 进程重启模拟 -> 密钥与配置恢复。
- 取消任务 -> 已完成结果保留 -> 项目状态可恢复。

### UI 测试

- UI 启动不应同步创建 HTTP 客户端或加载大型模型。
- 后台线程不得直接更新 Tk 控件。
- 关闭窗口后不再投递回调。
- 保存失败、部分成功和会话级密钥状态均有明确提示。

## 7. 建议的提交拆分

为降低回归风险，建议按以下顺序提交：

1. `fix: propagate image manifest persistence failures`
2. `fix: surface session-only secret storage state`
3. `build: add lint test and type-check quality gates`
4. `refactor: add application ports and bootstrap composition root`
5. `refactor: route text translation through application service`
6. `refactor: split configuration repositories and secret store`
7. `refactor: consolidate atomic file persistence`
8. `refactor: reduce epub processor to compatibility facade`
9. `refactor: split tkinter views and remove private coupling`
10. `cleanup: remove legacy translation path and adapters`

每个提交应满足：测试通过、没有同时保留两套新增实现、可以独立回滚。

## 8. 完成定义

整个重构完成时应满足：

- `domain` 无外层依赖。
- `application` 不导入 `ui`、`core`、`api` 或 `infrastructure` 具体实现。
- 所有外部依赖均通过 bootstrap 注入。
- 主翻译和图片翻译只有一条生产执行路径。
- 不存在跨对象私有成员访问。
- 原子写入只有一个实现。
- 测试、lint 和类型检查进入 CI。
- 发布构建使用可复现依赖。
- 所有持久化失败都能反馈给用户，不会被误报为成功。

