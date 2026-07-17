# 图片翻译模块修复与优化文档

> 更新日期：2026-07-16  
> 适用项目：AI-translater-1.4 / LightNovelTranslator V1.6  
> 目标：恢复本地 Manga 图片翻译，提供本地与 AI 两种显式入口，并保持 Python 分层与模块化边界。

## 1. 结论

本次修复完成了代码层面的运行闭环：

1. `manga_translator` 源码已纳入 `third_party/manga-image-translator/`，不再依赖开发者机器上的绝对路径。
2. 新增统一引擎加载器，开发环境和 PyInstaller 环境共用同一套定位与诊断逻辑。
3. 修复 Provider 第一次任务结束后被永久关闭、第二次任务必然失败的问题。
4. 主界面新增“本地模块翻译”和“AI 图片翻译”两个可见按钮，用户必须显式选择翻译方式。
5. 本地模块失败时不会自动切换 AI，避免非预期 API 调用和费用。
6. 补齐全局 Python 的图片模块依赖文件、第三方源码校验清单和 GPL-3.0 许可记录。
7. 新增标准库回归测试，验证引擎定位、全局解释器诊断和 Provider 连续运行。

当前机器仍不满足实际模型推理的运行环境：全局解释器为 Python 3.13.5，且没有安装项目依赖；上游引擎明确要求 Python 3.10 或 3.11。因此本次已完成代码修复和无重依赖回归，但尚未在本机完成真实图片的端到端推理。

## 2. 故障根因

| 编号 | 根因 | 原行为 | 修复 |
|---|---|---|---|
| R1 | vendored 目录只有说明文件，没有引擎源码 | `import manga_translator` 必然失败 | 纳入完整源码快照及 SHA-256 清单 |
| R2 | 开发运行时未把带连字符的第三方父目录加入导入路径 | 即使复制源码，Python 仍找不到顶层包 | `engine_loader.py` 集中配置导入路径 |
| R3 | 图片依赖文件缺少 OCR、语言识别、渲染和运行时包 | 导入引擎时逐个出现 `ModuleNotFoundError` | 按上游实际导入关系补齐依赖契约 |
| R4 | 当前全局 Python 是 3.13.5，上游只支持 3.10/3.11 | 二进制包、NumPy 和 Pydantic 版本不兼容 | 执行前返回解释器路径和版本的明确错误 |
| R5 | 每次任务的 `finally` 都调用 `provider.close()` | 第一次可运行，第二次提示 Provider 已关闭 | Provider 跨任务复用，应用退出时统一关闭 |
| R6 | 本地和 AI 入口分散在菜单中 | 用户无法在主界面直观看到翻译方式 | 增加两个并列按钮并统一分派 |
| R7 | 缺少运行中互斥 | 快速重复点击可启动多个模型任务 | 工作线程存活时拒绝重复提交并禁用入口 |
| R8 | 第三方许可被错误记录为 MIT | 分发时存在许可合规风险 | 依据上游 `pyproject.toml` 更正为 GPL-3.0-only |

## 3. 用户交互契约

### 3.1 本地模块翻译

- 入口：主界面“本地模块翻译”按钮、项目菜单或更多操作菜单。
- Provider：`ImageTranslationProviderId.MANGA`。
- 流水线：文字检测 -> OCR -> 文本翻译 -> 擦除 -> 修复 -> 译文渲染。
- 文本翻译：复用项目现有 OpenAI 兼容 API 配置。
- 火山 Key：不需要。
- 失败策略：显示诊断，不自动调用 AI Provider。
- 生命周期：同一应用会话内复用引擎和模型，退出应用时释放。

### 3.2 AI 图片翻译

- 入口：主界面“AI 图片翻译”按钮、项目菜单或更多操作菜单。
- Provider：`ImageTranslationProviderId.AI_VOLCENGINE`。
- 执行条件：已配置火山引擎 Key，并再次确认可能产生费用和生成式修改。
- 选择范围：本次选择只作用于本次任务，不修改本地模块的默认行为。
- 失败策略：直接报告 AI Provider 错误，不回退本地模块。

### 3.3 状态规则

- 未导入 EPUB：两个按钮均禁用。
- 已导入 EPUB：两个按钮可用。
- 图片任务运行中：两个按钮和对应菜单项均禁用。
- 任务结束或失败：恢复按钮状态。
- 重复点击：不创建第二个工作线程。

## 4. 模块架构

```text
Tk UI
  MainWindow
    -> ImageTranslationHandler          交互、线程和状态
        -> ImageTranslationService      请求校验、编排、manifest
            -> ProviderRegistry         按显式 id 查找，不做 fallback
                -> MangaProvider        本地流水线适配
                    -> EngineLoader     源码定位、版本和依赖诊断
                    -> MangaRuntime      独立 asyncio loop
                -> VolcengineProvider   AI 图生图适配
            -> ManifestRepository       原子保存结果
```

模块责任如下：

| 层 | 模块 | 责任 | 禁止事项 |
|---|---|---|---|
| Domain | `domain/image_translation.py` | Provider id、请求、进度和结果模型 | 不导入 Tk、HTTP 或模型库 |
| Application | `application/image_translation_service.py` | 用例编排、校验、取消、结果持久化 | 不实例化具体模型，不显示对话框 |
| Infrastructure | `image_translation/*_provider.py` | 外部引擎适配 | 不决定 UI 默认项，不自动切换 Provider |
| Infrastructure | `engine_loader.py` | 第三方包定位和环境诊断 | 不加载模型权重，不处理业务请求 |
| Infrastructure | `runtime.py` | 异步事件循环生命周期 | 不访问 Tk 控件 |
| UI | `image_translation_handler.py` | 用户确认、线程启动、主线程回调 | 不直接调用第三方引擎 |
| UI | `main_window.py` | 控件布局和可用状态 | 不包含图片翻译业务算法 |

该结构符合依赖方向：UI -> Application -> Domain/Ports，具体 Provider 位于 Infrastructure。第三方路径调整被限制在单一模块，不污染 `main.py` 或全局启动代码。

## 5. 全局 Python 环境

用户要求使用全局 Python 和全局包，因此不要使用项目内 `.venv`。推荐安装 64 位全局 Python 3.11。

### 5.1 确认解释器

```powershell
where.exe python
python --version
python -c "import sys; print(sys.executable)"
```

验收值应为 Python 3.10.x 或 3.11.x。不要使用 Python 3.12/3.13 运行本地 Manga Provider。

如果系统有多个全局 Python，使用解释器绝对路径执行后续命令，保证安装包和运行应用使用同一个解释器：

```powershell
C:\Users\<user>\AppData\Local\Programs\Python\Python311\python.exe -m pip --version
```

### 5.2 安装全局依赖

```powershell
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -r requirements-image-manga.txt
```

GPU 用户应先按 PyTorch 官方说明安装匹配显卡驱动的 `torch` 和 `torchvision`，再安装其余依赖。CPU 环境可以直接使用依赖文件的默认版本。

### 5.3 环境自检

```powershell
python -c "from src.infrastructure.image_translation.engine_loader import diagnose_manga_engine; print('\n'.join(diagnose_manga_engine()) or 'Manga engine ready')"
python -m unittest tests.test_image_translation_runtime -v
python main.py
```

自检输出 `Manga engine ready` 才表示源码、解释器和导入依赖均满足。首次真实翻译仍可能下载模型，必须允许网络访问并预留模型缓存空间。

## 6. 配置与数据路径

- 源码资源：`third_party/manga-image-translator/manga_translator/`。
- 源码完整性：`third_party/manga-image-translator/SOURCE_SHA256.txt`。
- 模型缓存：`AppPaths.data_dir/models/manga/`。
- EPUB 图片清单：工作区中的 `images.json`。
- 翻译结果图片：`translated_images/manga/` 或 AI Provider 对应目录。
- 结果清单：`image_translation_result.json`，由仓储原子写入。

模型和任务输出不得写入源码目录或 PyInstaller 的 `_MEIPASS` 临时目录。API Key 继续由现有安全存储读取，不写入环境变量、日志或 manifest。

## 7. Python 与模块化准则

后续改动必须遵循以下约束：

1. 使用 `pathlib.Path` 处理路径，不拼接平台相关分隔符。
2. UI 回调只在 Tk 主线程执行；模型和网络工作放在后台线程。
3. Domain 模型保持小型、明确、可序列化，Provider 必须返回结构化结果。
4. 依赖通过构造函数或 Protocol 注入，禁止业务代码读取全局注册表单例。
5. 重依赖使用惰性导入，应用启动不应加载 Torch、OpenCV 或模型权重。
6. `cancel()` 和 `close()` 必须幂等，生命周期由拥有者统一管理。
7. 不捕获后静默丢弃关键错误；对用户返回可操作信息，对日志记录异常栈。
8. 错误消息必须脱敏，不包含 API Key、Authorization Header 或完整 Base64。
9. 文件写入使用原子替换；只有落盘成功的文件才能进入 `result_map`。
10. 本地和 AI Provider 只能由显式 `provider_id` 选择，禁止隐式 fallback。
11. 第三方源码不直接修改；必须修改时记录在 `LOCAL_CHANGES.md` 并重建哈希清单。
12. 新功能至少覆盖成功、校验失败、取消、连续运行和资源关闭测试。

## 8. 验证记录

本次在全局 Python 3.13.5 下完成以下不依赖第三方包的验证：

```text
AST 语法解析：74 个项目 Python 文件通过
标准库单元测试：3/3 通过
- vendored package is discoverable
- diagnostics identify the running global Python
- provider is reused across consecutive runs
```

未执行的验证：

- 全量 `pytest`：全局 Python 未安装 pytest 和项目依赖。
- 本地模型真实推理：当前 Python 3.13.5 不在上游支持范围，且 Torch/OpenCV/ONNX Runtime 未安装。
- AI Provider 真实请求：会产生外部调用和费用，本次未发送。
- PyInstaller 构建：缺少全局构建及模型依赖。

完成全局 Python 3.11 安装后，应执行：

```powershell
python -m pytest -q
python build.py --check-deps
```

并准备一个包含日文或英文气泡文字的小型 EPUB 做以下验收：

1. 本地按钮首次翻译成功。
2. 不重启应用，再次运行本地翻译成功。
3. 无火山 Key 时本地按钮仍可运行。
4. AI 按钮在无 Key 时打开设置且不发请求。
5. AI 按钮在用户取消确认时不发请求。
6. 导出 EPUB 后图片替换正确，原始 EPUB 不被修改。

## 9. 后续优化路线

### P0：发布前必须完成

1. 在干净的全局 Python 3.11 环境执行完整依赖安装和真实图片冒烟测试。
2. 固定并记录可追溯的上游 commit/tag；当前本地快照没有 `.git` 元数据。
3. 确认项目整体分发方式满足 manga-image-translator 的 GPL-3.0-only 要求。
4. 校验 PyInstaller 能收集所有动态导入、YAML、tokenizer 和模型下载逻辑。
5. 为全局依赖生成经过验证的锁文件，避免仅靠宽松版本范围发布。

### P1：稳定性和性能

1. 将上游 `translators`、`detection` 等注册模块改造成按配置惰性导入，减少非默认依赖和启动耗时。
2. 增加独立“环境检查”界面，展示 Python、设备、模型目录、缺失包和预计磁盘占用。
3. 增加图片任务取消按钮，并把取消传播到每个检测/OCR/修复阶段。
4. 对 GPU OOM 提供明确诊断和低内存预设重试，但必须由用户确认，不自动切换 AI。
5. 记录每阶段耗时、模型加载耗时、峰值内存和单图失败率，不记录图片内容或密钥。
6. 队列窗口和主窗口共用 Provider 工厂及生命周期管理器，避免重复加载同一套模型。

### P2：可维护性和质量

1. 增加 Provider 契约测试，使用同一套用例验证 Manga、AI 和 Fake Provider。
2. 增加 manifest schema 迁移测试、损坏恢复测试和导出端到端测试。
3. 建立小型许可可分发测试数据集，覆盖横排、竖排、透明图、WebP 和重复文件名。
4. 增加 CPU/GPU 基准，基于数据调整 1024/1536/2048 质量预设。
5. 将模型进程隔离为可重启 worker，降低原生扩展崩溃对 Tk 主进程的影响。
6. 在 CI 中分离轻量单元测试与带模型的夜间集成测试，避免每次提交下载模型。

## 10. 验收标准

- 用户能在主界面直接选择本地模块或 AI 图片翻译。
- 本地入口不检查火山 Key，AI 入口必须检查 Key 并确认费用。
- 不存在任何自动 Provider fallback。
- 连续执行至少两次本地任务不会出现“Provider 已关闭”。
- 缺源码、错误 Python 版本或缺依赖时显示包含修复命令的明确诊断。
- 结果 manifest 与 EPUB 导出保持向后兼容。
- 应用退出后后台线程、event loop、模型和 HTTP 客户端被释放。
- 第三方源码、许可、来源和完整性清单随项目分发。
