# Python 与桌面 UI/UX 最佳实践审查及优化路线图

## 1. 审查结论

审查日期：2026-07-17（UTC+08:00）。审查对象为当前 V1.6 动态工作树。

当前项目对 Python 与桌面 UI/UX 最佳实践的符合度为：**部分符合，暂不建议直接作为稳定版发布**。

项目已经具备比一般 Tkinter 工具更扎实的基础：`domain/application/infrastructure/ui` 分层、Protocol 端口、组合根、原子写入、keyring 三态结果、翻译事件邮箱、文档模型、自动保存 generation、队列限流与 checkpoint、图片路径信任边界，以及数量较多的专项测试。当前风险主要来自三类问题：

1. 新基础设施还没有完全接入生产路径，仍存在会话、模型、视图、后台任务和密钥存储的多套状态。
2. 构建与发布契约没有经过真实产物验证，Text/Full edition、macOS 入口和依赖安装互相不一致。
3. 网络、日志、子进程和 GUI 主线程等运行时边界仍有安全、资源泄漏和界面失去响应的风险。

在 README 和 CI 当前承诺同时发布 Windows/macOS、Text/Full 的前提下，发布前应先关闭 4 个 P0：macOS 包入口、edition 能力契约、未保存会话语义、模型与表格渲染一致性。若正式缩减为 Windows Text-only，前两项可移出当期发布阻断，但后两项仍是无条件 P0。视觉润色、更多并发和新导出格式不应排在这些问题之前。

## 2. 范围与方法

### 2.1 审查范围

- Python 第一方代码：`main.py`、`build.py`、`src/**`、`tools/**`、`tests/**`。
- 工程配置：`pyproject.toml`、`requirements*.txt`、两个 PyInstaller spec、GitHub Actions、README、`.gitignore`。
- 生产 UI：`src/ui/**` 及其直接依赖的应用、领域和基础设施层。
- `third_party/manga-image-translator/**`：不评价上游代码风格，只检查许可证、依赖、进程边界和本项目集成。
- `build/**`、`dist/**`、`workspace/**`：按生成物处理，不纳入源码风格统计。

`creative_proposal.html` 没有被生产入口引用，本次不把它当成当前产品界面。

### 2.2 参考基线

- Python：PEP 8、显式类型、窄异常、资源上下文、可测试依赖注入、可复现构建和最小副作用。
- Tkinter：所有 Tk 调用只在主线程发生；worker 只发布不可变事件；窗口、任务、会话和资源具有明确 owner 与关闭路径。
- `ui-ux-pro-max`：以“桌面 AI 文档翻译生产力工具、专业、紧凑、可访问”生成设计系统。结果中的 landing、CTA、Web 字体和触屏手势不适用于 Tkinter 生产界面；本文只采用对比度、焦点、异步反馈、状态表达和长列表等通用原则。
- `web-design-guidelines`：采用技能说明中的键盘、焦点、异步反馈、对比度、破坏性操作和长列表方向。技能要求在线拉取最新规则，但环境拒绝了该远程请求，因此本文不声称完成了最新线上规则的逐条核对。
- WCAG 2.2：普通文本最低对比度 4.5:1，状态不能只依赖颜色表达。

设计技能给出的 `#0D9488` 可作为候选品牌色，但白色普通文字配该颜色的对比度约为 3.74:1；当前界面应改用约 5.47:1 的 `#0F766E`，或直接使用系统 accent/theme token。

### 2.3 动态工作区限制

审查基线为 `HEAD 0d17d14` 加当前动态工作树；记录时 `git status --porcelain` 有 163 项，包含大量未提交和未跟踪改动。因此本文描述的是 2026-07-17 当前工作树，而不是纯 `HEAD`。发布前必须冻结变更并重新执行完整质量门禁；本次不回退或覆盖其他现有改动。

### 2.4 优先级定义

- **P0**：在已承诺发布范围内使产物不可启动、能力确定不可用，或可造成确定性数据丢失、错存、视图/磁盘不一致及关键安全破坏；关闭后必须有自动化回归或真实产物 smoke。
- **P1**：核心流程可靠性、安全性或可恢复性明显不足，但有绕行路径，或只在故障、并发、大文件等条件触发。
- **P2**：主要影响可访问性、感知性能、维护成本和长期缺陷率；不应挤占 P0/P1，但需进入版本计划并设置验收预算。

## 3. 验证结果

| 检查 | 当前结果 | 解释 |
| --- | --- | --- |
| AST 语法解析 | 141/141 通过，exit 0 | `python -c` 枚举 `main.py/build.py/src/**/*.py/tools/**/*.py/tests/**/*.py`，以 `utf-8-sig` 读取并逐个 `ast.parse`；不代表导入和运行时通过 |
| 聚焦 `unittest` | 53 passed，exit 0 | `python -m unittest tests.test_image_translation_regressions tests.test_image_translation_runtime tests.test_onboarding tests.test_settings_window_layout tests.test_ui_startup` |
| 扩大 `unittest` 尝试 | 76 tests，6 errors，exit 1 | 在上述集合加入取消与信任边界模块；错误均因宿主缺少 `httpx` 导致相关模块导入失败，没有执行到项目断言；不能当作绿色基线 |
| 完整 pytest | 未执行 | 宿主 `pytest` 解析为 namespace package：`__file__`/`__version__` 均为空且没有 `fixture`/`main`，无法作为 pytest runner |
| Ruff | 未执行 | 当前 Python 环境未安装 Ruff |
| 原生 GUI/DPI/键盘 | 未执行 | 本次未启动会写用户目录的生产 GUI；需在隔离用户目录和真实桌面环境补验 |
| PyInstaller 产物 smoke | 未执行 | 现有测试只检查 spec 文本/AST，没有解包并启动真实产物 |

本机是 CPython 3.13.5，项目 CI 声明 Python 3.11。上述动态结果仅用于识别当前审查环境的可运行范围，不能替代干净 Python 3.11 环境的完整结果；聚焦测试中的预期异常会写入日志，不代表测试失败。

## 4. 符合度矩阵

| 领域 | 结论 | 主要证据 |
| --- | --- | --- |
| 分层与依赖方向 | 部分符合 | 分层、Protocol、组合根已建立；UI 和 ConfigManager 仍直接创建/调用基础设施 |
| 状态一致性与数据安全 | 不符合 | clipboard dirty、session 替换、模型拒绝后视图仍更新 |
| 并发与 Tk 线程模型 | 部分符合 | 主翻译、导入、设置已使用 mailbox；队列图片 worker 仍直接 `after()` |
| 异常与资源生命周期 | 部分符合 | 原子写和结构化结果较好；HTTP response、checkpoint、worker deadline、hook 仍有缺口 |
| 类型与配置契约 | 部分符合 | domain/application 较清晰；大量 UI/core 字典、回调和公开方法仍弱类型 |
| 测试与质量门禁 | 部分符合 | CI 已有 Ruff/format/pytest；缺真实 GUI、artifact、coverage、类型与安全门禁 |
| 依赖与打包 | 不符合 | macOS 入口错误、edition 契约冲突、Full 外部 worker 源码/依赖未形成可运行分发契约、无可复现锁 |
| 凭据与日志安全 | 部分符合 | keyring 和脱敏 helper 已有；HTTP 自定义端点、全局日志 Filter、SecretStore 接线未完成 |
| 信息架构与工作流 | 部分符合 | 空状态、筛选、搜索、撤销、进度和引导已具备；命令重复且保存语义不一致 |
| 键盘与辅助功能 | 不符合 | 主窗口有快捷键；队列、模态框、滚动页、右键操作和进度文本路径不完整 |
| DPI、主题与布局 | 不符合 | 设置页开始支持缩放；主工具条、硬编码颜色/字体和固定窗口仍有裁切风险 |
| 长列表与感知性能 | 部分符合 | 主表分块加载较好；详情、术语、筛选和批量导出仍在主线程全量处理 |

## 5. P0：发布阻断项

### P0-1：macOS 应用包没有有效入口

**证据**

- `.github/workflows/build.yml:130-147` 复制 onedir 内容，真实二进制名来自 matrix：`LightNovelTranslatorV1.6` 或 `LightNovelTranslatorV1.6-Text`。
- 同一脚本在 `:141,146,156-157` 把入口写成 `LightNovelTranslator`，但没有执行重命名。
- `translator.spec:143` 与 `translator_text.spec:164` 明确生成 matrix 中的名称。

**影响**

`chmod` 会因目标不存在而让 job 失败；即使绕过失败，`Info.plist` 的 `CFBundleExecutable` 也找不到真实二进制，`.app` 无法启动。

**改进与验收**

1. 将真实二进制显式移动为 `Contents/MacOS/LightNovelTranslator`，或让 `CFBundleExecutable` 使用真实名称。
2. 在打包后执行 `test -x`、`plutil -lint`、最小启动和正常退出 smoke。
3. 对 Text/Full 两个 artifact 分别解压验证，不只验证 zip 存在。

### P0-2：Text/Full edition 的能力、依赖和运行时注册互相冲突

**证据**

- `translator_text.spec:108-113` 排除 `manga_translator`。
- `src/ui/image_translation_handler.py:74-104,169-198,242-247` 创建任何图片服务时都无条件调用 `_create_manga_provider()` 并导入 Manga provider；Text 版显式选择火山 AI 后，worker 仍先调用 `_get_service()`。
- Full 运行时实际通过 `src/infrastructure/image_translation/manga_provider.py:527-565` 代理到外部 Python 3.11 worker；`manga_worker_client.py:164-207` 用外部解释器执行 `-m src.infrastructure.image_translation.manga_worker`。
- `translator.spec:32-46,135` 明确不复制 `manga_translator` 源码，而是收进 PyInstaller 的 PYZ；外部 Python 无法直接导入 PYZ 内的 `src` 和第三方源码。CI 又只安装 `requirements.txt`，没有构建或验证独立 worker 环境。
- `tests/test_image_packaging.py:5-16` 只检查 spec/requirements 文本，把真实产物验证留为手工步骤。

**影响**

Text 版可能无法使用本应保留的在线图片功能；Full 版可能构建成功，但外部 Python 3.11 找不到 worker 模块/第三方源码或缺少依赖。CI 绿灯不能证明 edition 能力可用。

**改进与验收**

1. 定义显式 `EditionCapabilities`，按能力注册 provider；Text 版不得触碰 Manga 模块。
2. 明确 Full 是“内置依赖”还是“外部 Python 3.11 worker”，spec、安装、UI 和文档只能保留一种契约；若保留外部 worker，需把可导入的 `src` worker 模块、vendored engine 和依赖环境作为受版本管理的分发单元。
3. Full 使用专用锁文件或可校验的 worker 环境安装器；Text/Full 各跑 provider availability smoke。
4. 用 fake provider 完成最小 EPUB 图片任务，验证取消、关闭和结果持久化。

### P0-3：未保存状态没有真正归属于 DocumentSession

**证据**

- `src/ui/file_importer.py:519-535` 创建 clipboard session 时没有 target path。
- `src/ui/main_window.py:999-1006` 在没有 target path 时先返回，之后才设置 `_unsaved_edits=True`；剪贴板编辑因此不会进入关闭保护。
- `src/ui/file_importer.py:274-286` 提交新 session 时直接替换并调用 `table_loader`，没有 Save/Discard/Cancel guard。
- `src/ui/main_window.py:591-593` 又把 session 的内容复制到另一个 `TranslationDocument`；`DocumentSession.mark_dirty/mark_clean` 只在测试中使用。
- `src/ui/translation_controller.py:1098-1123` 的 `Ctrl+S` 每次打开“另存为”，成功后不更新当前 target，也不清理 MainWindow 的 dirty 状态。

**影响**

剪贴板内容可在退出时无提示丢失；导入新项目可能直接替换未保存文档；手动保存后仍可能提示未保存；session 的“唯一真相来源”只停留在导入瞬间。

**改进与验收**

1. 让 MainWindow、autosave、controller 和 FileImporter 共享同一个 `DocumentSession.document`。
2. dirty 属于 session，与是否有 target、是否启用 autosave 无关。
3. 关闭、导入、粘贴、打开最近项目统一走 Save/Discard/Cancel guard。
4. `Ctrl+S` 保存当前目标；无目标时执行 Save As，并把新目标写回 session；“另存为/导出”单列命令。
5. 覆盖 clipboard 编辑后关闭、dirty 时切项目、保存失败、Save As 后重启恢复等回归测试。

### P0-4：模型拒绝机器结果后，表格仍无条件显示该结果

**证据**

- `src/application/translation_document.py:115-118` 对人工编辑行拒绝任何非人工更新并返回 `False`。
- `src/ui/translation_controller.py:581-586` 与 `:609-615` 忽略该返回值，随后把原始 `updates` 全量交给 table adapter。

**影响**

人工译文在模型中仍被保护，但 Treeview 会显示机器译文；自动保存使用模型旧值。用户看到的内容与磁盘内容不一致，刷新后还可能“回滚”。

**改进与验收**

1. 只把 `update_target()` 返回 `True` 的行放入 `accepted_updates` 并渲染。
2. adapter 不得绕过模型修改业务值。
3. 增加流式和批次两组集成测试：人工编辑发生后，旧机器结果不能改变模型、表格、dirty 标记和保存文件。

## 6. P1：高优先级问题

### P1-1：自定义 API 可通过远程明文 HTTP 发送 Bearer 密钥

`src/config/translation_profile.py:176` 接受任意 `http://` URL，而 `src/api/base_api.py:95` 会把密钥放入 `Authorization`。仅允许 HTTPS；HTTP 只对白名单回环地址开放，并测试拒绝远程 HTTP。

### P1-2：checkpoint 失败后无法可靠重试和关闭

`src/core/queue_checkpoint.py:207-235` 在保存前清空 pending snapshot，失败时只保留 dirty；随后可能处于 `dirty=True/pending=None` 并永久等待。失败时必须恢复最新快照，加入有界重试/失败终态，并断言 `close()` 后线程退出。

### P1-3：图片运行时的资源与请求生命周期仍不完整

- `src/core/image_translator.py:166-203` 在重定向、HTTP 错误和 MIME 拒绝路径上不能保证所有 response 关闭。
- `src/infrastructure/image_translation/manga_worker_client.py:127-162` 阻塞 `stdout.readline()`，无 deadline；只锁写入，没有串行化完整请求/响应。
- `src/infrastructure/image_translation/manga_provider.py:192-214,307` 复用引擎但每次任务追加 progress hook，无移除路径。
- `src/infrastructure/image_translation/runtime.py:85` 没有在 loop 线程内完整 cancel/gather/shutdown async generators。

统一使用上下文管理 response；worker 使用单 reader + request ID + deadline；hook 使用稳定桥接或 finally 移除；runtime 在 loop 线程完成异步清理后再关闭。验收要求：所有 HTTP 状态/MIME/重定向/超限分支都关闭 response；并发请求不串包且 deadline 后返回；连续任务的 hook 数量不增长；关闭后无 pending task、worker 或读线程。

### P1-4：SecretStore 和组合根只接线了一半

`src/bootstrap.py:59` 已注入 `KeyringSecretStore`，但 `src/config/config_manager.py:192,210,609,624` 仍直接调用全局 keyring helper，预设保存还把三态结果压成布尔。所有密钥读写删改必须只走注入协议，并统一返回 `SecretSaveResult`。

### P1-5：日志脱敏和可观测性没有形成闭环

`src/utils/logger.py:40-60` 使用普通 Formatter 和无限增长的 FileHandler；`sanitize_for_log()` 没有安装为 handler Filter；`src/api/base_api.py:349`、`src/api/siliconflow_api.py:177` 仍可能记录外部响应正文；第一方代码还有大量 `print()`。应使用 rotating handler、中央脱敏 Filter、有界响应摘要和 run/task correlation ID，提供可导出的脱敏诊断包。用包含 Bearer token、API Key、用户原文和超长响应的端到端日志测试证明输出已脱敏且有长度上限。

### P1-6：筛选往返会永久打乱表格顺序

`src/ui/main_window.py:747-764` 隐藏行使用 `detach()`，恢复时统一 `reattach(..., "end")`。例如 `[1,2,3,4] -> [2,4] -> 全部` 会变成 `[2,4,1,3]`。恢复时必须按 `_all_items` 原始索引插入，或重建稳定 row-id 视图，并增加筛选往返顺序测试。

### P1-7：术语窗口“保存/关闭”语义不一致

`src/config/config_manager.py:554-556` 只返回顶层浅拷贝；`src/ui/glossary_window.py:275,307` 的添加/更新会修改共享嵌套对象，`:330` 的删除却替换窗口副本列表；`:174,402` 又分别提供直接关闭和显式保存。窗口应使用 `deepcopy` 工作副本与 dirty 状态，保存原子提交，关闭脏窗口提供保存/放弃/取消。

### P1-8：队列图片 worker 仍直接调用 Tk，且没有防重复启动

`src/ui/concurrent_window.py:503-591` 的 worker 在 `:530,550,589` 调用 `self.win.after()`，每次点击都创建新线程。统一接入现有 `UICallbackMailbox/TkUICallbackPump`，增加 busy/run ID、禁用入口、取消和迟到事件丢弃。

### P1-9：大文件导入/导出仍会阻塞 UI，EPUB 解析不可取消

- `src/ui/concurrent_window.py:312-330` 同步逐个导入；`:424-463` 同步批量复制/导出。
- `src/ui/translation_controller.py:1176-1248` 在 UI 回调内保存映射并导出 EPUB。
- `src/ui/file_importer.py:295-412` 的模态解析框禁止关闭，worker 没有取消令牌。

这些操作应改为后台 job + 不可变进度事件，提供当前文件、总进度、取消、失败项和重试入口；任何路径都必须在 finally 中释放 grab 并恢复控件。用大型 EPUB 和批量任务验证 Tk 心跳持续推进、取消有界完成、窗口关闭后没有迟到回调。

### P1-10：主命令层级与入口重复

主界面同时提供“翻译未完成行/继续翻译”，图片翻译和 EPUB 导出又分散在项目菜单、更多操作和独立按钮区。完成 P0-3 的保存语义修复后，应收敛为一个上下文主动作，低频图片/导出动作各保留一个稳定入口，菜单、工具条和快捷键使用同一 command。

### P1-11：CI 有质量 job，但仍不能证明发布产物可用

`.github/workflows/build.yml:20-45` 已加入 Ruff、format、pytest，这是明显进步；但没有 coverage、类型、安全、wheel install、真实 GUI 和 artifact smoke，且只在 Ubuntu/Python 3.11 跑逻辑测试。构建 job 必须消费并验证真实 Text/Full artifact，Windows/macOS 至少启动到首帧并正常退出。

### P1-12：依赖、发布和文档不可复现

- `requirements-image-manga.txt` 自称“锁定”但大量使用范围版本，额外索引的 Rust wheel 没有精确版本/hash；PyInstaller 也未固定。
- `.github/workflows/build.yml:185-203` 会在 V1.6 分支 push 时复用固定 release tag，版本散落在 workflow、spec、main、build 和 pyproject。
- `README.md:38-42` 声称单 exe 与 macOS Universal，实际是 onedir zip 且没有 universal2 证据；`:47-62` 仍指导把明文密钥写入仓库配置文件。
- `README.md:147` 声称根 MIT LICENSE，但仓库根没有该文件。

按 OS/edition 生成带 hash 的 lock/constraints；只从版本 tag 发布；单一版本源派生 artifact 名；补根许可证、checksum/SBOM，并让 README 反映 keyring、Text/Full 能力和真实文件名。

## 7. P2：结构、质量与体验改进

### P2-1：键盘与破坏性操作

- 队列详情主要依赖双击，单任务命令依赖右键；缺 `Enter/F2/Shift+F10/Menu/Delete` 等价路径。
- 单任务取消、移除和批量清空没有一致确认；批量清空会产生多条 undo。
- 模态窗口普遍缺 Escape、关闭后焦点恢复和默认按钮策略。
- 异步进度主要依赖可见文本变化，没有统一的可访问状态事件，屏幕阅读器不一定能获知“开始、失败、完成”。

为每个核心流程提供纯键盘路径；确认对话框显示对象、数量、进度和后果；批量编辑形成单个 undo command；异步状态变化应进入可聚焦状态区并通过原生可访问性桥接验证可被 NVDA/Narrator 宣布。

### P2-2：主题、对比度、字体和信息层级

- `src/ui/main_window.py:216-230` 把次级“批量翻译队列”做成最强强调色，真正主翻译动作只是普通按钮。
- `:414-432,639-640` 强制白色表格；`src/ui/settings_window.py:785` 使用低对比 `#0a0`。
- 队列、详情和导入混用 Emoji 与“微软雅黑”，字体回退会改变行高或显示方框。
- “界面字号”实际只更新 Treeview，不是全局 UI 字号。

使用 ttk theme token 和 Tk named fonts 定义 surface/text/muted/accent/danger/focus/spacing；主色至少满足 4.5:1；状态同时使用文字/图标；统一图标资源并保留文本标签。

### P2-3：响应式桌面布局与滚动

主窗口在 800px、高 DPI、最大字号 18、长项目名/模型名时，工具条、筛选条和底部控制条都可能挤压。设置窗口同时设置 `minsize(600,560)` 又尝试适配更小屏幕，页内 Canvas 只有滚动条，没有滚轮、PageUp/PageDown 或焦点自动滚入视区。`tests/test_settings_window_layout.py` 只检查 geometry 字符串，没有模拟窗口管理器应用 `minsize` 后的实际可见区域。

按可用宽度把项目状态和低频操作移到第二行/菜单；设置窗口最小尺寸服从工作区；补滚轮、键盘滚动和 `FocusIn` 定位。

### P2-4：大列表与感知性能

`TaskDetailWindow` 打开时同步插入全部行并每 500ms 扫描目标列表；术语搜索删除重建全部匹配项；主筛选逐行访问 Tk。详情表使用分块加载和 changed-row 事件，术语使用索引/分页/可见区渲染，并为 10,000 行、5,000 术语、50 任务建立响应预算。

### P2-5：表单校验与异步状态

`src/ui/settings_window.py:282-307,336-583` 的大量 Spinbox 可输入非法文本，`:1093-1187` 只在保存时弹通用错误；`:738-844,1034-1049` 的连接测试和模块检测可重复启动，火山测试还会在用户最终保存设置前持久化密钥。加入字段级校验、首错聚焦、跨字段约束、testing busy 状态和临时配置测试；测试连接不得改变持久化配置，只有“确定/保存”才提交。

### P2-6：类型、配置和热点模块

`src` 约有 929 个函数定义，至少 258 个缺返回注解；第一方代码约有 234 个 `except Exception` 和 77 个 `pass`。Ruff 未启用 `ANN/S/BLE/ASYNC/RUF`，也没有 mypy/pyright。`main_window.py`、`settings_window.py`、`translation_controller.py`、`queue_scheduler.py` 均超过 1,200 行。

先对 `domain/application` 启用严格类型，再扩展 infrastructure 与新 UI adapter；按用例拆 command/service/adapter，不按行数机械拆文件；优先处理配置凭据、DocumentSession、队列编辑和导出。

### P2-7：启动与耐久性语义

`AppPaths` 对必要目录创建失败仍返回无效对象；`tests/test_bootstrap.py` 设置的 `AI_TRANSLATOR_*` 测试路径没有被 `AppPaths` 读取，测试可能触碰真实用户目录；图片模块仍有导入期 `logging.basicConfig()`；`atomic_file.py` 使用 `os.replace()` 但没有 `flush/fsync`，其“durable”承诺高于实际保证。必要目录失败应转为结构化启动错误；组合根允许显式注入测试路径并断言用户目录零写入；删除模块导入副作用；若承诺断电级耐久则补 fsync，否则收窄文档表述。

## 8. 已完成且应保留的改进

相较旧审查快照，当前工作树已经落实了以下正确方向：

- `pyproject.toml` 已声明 1.6.0，wheel package discovery 已包含 `src*`；其他版本入口仍待统一。
- CI 已增加 Ruff、format、pytest job，并拆出 Text/Full 构建 matrix。
- `TableCellEditor` 不再直接把 Treeview 当业务真相，人工编辑会写回 `TranslationDocument`。
- 已引入 `DocumentSession`、结构化 `ImportResult` 和原子 session 提交，导入创建路径已集中；生产状态与保存守卫仍待统一。
- 翻译 run ID 在主线程生成，事件通过 mailbox/pump 回到 Tk 主线程；大多数 worker 不再直接调用 Tk。
- 自动保存已有 generation、单飞和失败状态；已正确进入 dirty 的路径在关闭前有 Save/Discard/Cancel。
- 设置页能够显示 `SecretSaveResult` 的 FAILED/SESSION_ONLY，而不是一律冒充永久保存成功。
- 图片本地路径已限制在 assets 根，远程下载已有 HTTPS、host、MIME、大小和魔数校验。
- 主表采用 250 行分块加载，adapter 避免翻译热路径反复全表读取。
- 队列已有 limiter、硬并发上限、不可变 snapshot、checkpoint、取消和专项并发测试。
- 第三方源码已有 LICENSE、notices、upstream、local changes 和 source hash 记录。

这些改进说明项目方向正确；下一步应完成生产接线和端到端验收，而不是再增加平行基础设施。

## 9. 执行追踪表

| ID | Owner / 模块 | 完成条件 | 自动化验收 | 前置依赖 |
| --- | --- | --- | --- | --- |
| P0-1 | Release / `.github/workflows` | `.app` 入口与 plist 一致 | Text/Full `plutil`、可执行检查、启动/退出 | 保留 macOS 发布范围 |
| P0-2 | Packaging + Image / spec、bootstrap、provider | edition 只注册承诺能力；Full worker 可独立导入运行 | Text 不导入 Manga；Full health + 最小图片任务 | 确定内置或外部 worker 契约 |
| P0-3 | App + UI / session、import、save | 单一 session/document/dirty/target；切换和关闭统一 guard | clipboard、切项目、保存失败、Save As、重启恢复 | 明确保存/导出命令模型 |
| P0-4 | App + UI / document、controller、adapter | 仅 accepted update 进入视图 | 流式/批次人工编辑竞态测试 | P0-3 的单一文档实例 |
| P1-1 | API + Config | 拒绝远程 HTTP，回环地址例外显式可测 | URL 参数化安全测试 | typed endpoint validation |
| P1-2 | Queue / checkpoint | 失败保留最新快照；关闭后线程退出 | 失败重试、连续更新、close 测试 | 定义重试与失败终态 |
| P1-3 | Image runtime | response、worker、hook、event loop 全部有 owner/deadline | 异常分支关闭、并发配对、hook 稳定、shutdown 测试 | P0-2 worker 契约 |
| P1-4/5 | Config + Platform | 密钥只经注入协议；日志全局脱敏/轮转 | fake SecretStore、三态结果、敏感日志 E2E | 组合根统一接线 |
| P1-6/7 | Main UI + Glossary | 筛选顺序稳定；关闭不产生隐式部分保存 | 筛选往返、Save/Discard/Cancel 测试 | 明确工作副本与 dirty |
| P1-8/9 | Queue UI + Import/Export | worker 不调用 Tk；长任务可取消并有迟到事件隔离 | Tk heartbeat、取消、关闭窗口、run-id 测试 | 统一 mailbox/job API |
| P1-10 | Main UI | 菜单/工具条/快捷键复用同一 command | 命令可用状态与快捷键测试 | P0-3 保存语义 |
| P1-11/12 | Release + Docs | 锁、版本、许可证、产物说明与真实 artifact 一致 | clean install、artifact smoke、secret/license scan | 冻结发布范围和版本源 |
| P2-1..5 | UX + QA | 键盘、读屏、DPI、长列表、表单达到验收矩阵 | 少量真实 Tk + Windows UI smoke | 主题 token、性能样本 |
| P2-6/7 | Architecture + Platform | 渐进类型门禁；路径和耐久承诺可证明 | pyright、隔离路径、fsync/契约测试 | P0/P1 生产接线稳定 |

Owner 表示职责域，不预设具体人员。完成条件和自动化验收同时满足后才能关闭；仅修改代码或仅补测试均不视为完成。

## 10. 推荐实施顺序

以下阶段按门禁排序，不是固定工期。若按 2 名熟悉代码库的工程师估算，阶段 0 通常需要约 3-7 个工作日；跨平台签名、公证和真实 GUI 验证取决于设备与证书条件。

### 阶段 0：发布止血

1. 修复 macOS 入口并跑真实 `.app` smoke。
2. 建立 edition capability，修 Text 无条件 Manga import，并让 Full 外部 worker（或选定的内置方案）形成可运行分发单元。
3. 修复 P0 session/dirty/Save As 语义。
4. 修复模型拒绝结果后 adapter 仍渲染的问题。
5. 暂停自动 release，直到 Text/Full 两个平台 smoke 都通过。

### 阶段 1：可靠性与安全

1. 拒绝远程 HTTP API endpoint。
2. 修 checkpoint 重试/关闭、HTTP response、Manga deadline/request pairing/hook。
3. 完成 SecretStore 注入，并让 AppPaths 可在测试组合根显式替换。
4. 中央日志脱敏、旋转和诊断导出。
5. 修筛选顺序、术语保存语义和队列图片 Tk 线程边界。

### 阶段 2：UI/UX 与可访问性

1. 把导入/导出/解析改为可取消后台 job。
2. 收敛主命令、保存、图片和导出入口。
3. 完整键盘、焦点、确认、滚动和进度文本路径。
4. 建立主题 token、全局 named font、对比度和高 DPI 布局。
5. 为长列表建立性能预算与真实 Tk 集成测试。

### 阶段 3：质量与发布治理

1. 生成 edition/OS 锁文件和 hashes，固定构建工具。
2. 加 coverage 基线、类型检查、依赖/秘密扫描、wheel install smoke。
3. 只从 tag 发布，加入签名、公证、checksum 和 SBOM。
4. 按垂直用例拆热点模块，删除已经迁移的旧入口。
5. 更新 README、根 LICENSE、发布产物和运维诊断文档。

## 11. 建议质量门禁

基础门禁：

```powershell
python -m ruff check main.py build.py src tests tools
python -m ruff format --check main.py build.py src tests tools
python -m pytest -q
python -m pytest --cov=src --cov-branch --cov-report=term-missing
python -m pyright src/domain src/application
python -m pip wheel . --no-deps --no-cache-dir --wheel-dir dist/wheels
# 随后在新建的干净环境安装 wheel，并执行 import、配置隔离和首帧/退出 smoke
```

短期不建议用高覆盖率一次性阻塞历史代码。先记录绿色基线并禁止下降；P0 涉及的 session、dirty、编辑、保存、edition 和 artifact smoke 必须达到分支全覆盖。

发布门禁至少包含：

1. Python 3.11 的 Linux 逻辑门禁。
2. Windows 10/11 Text/Full 解压、启动首帧、fake 翻译、保存、重开、退出。
3. macOS Text/Full `plutil`、真实入口、启动/退出；产物架构名称必须与实际一致。
4. Text 版在线图片 provider 可创建且不会导入 Manga。
5. Full 版 Manga provider availability 与最小图片任务。
6. 凭据不进入 artifact、日志、测试输出和配置样例。

## 12. 桌面 UI 验收矩阵

| 维度 | 最低覆盖 |
| --- | --- |
| DPI/分辨率 | Windows 100/125/150/200%；800x600、1366x768、1920x1080；字号 8/10/18 |
| 主题 | 默认、暗色和高对比；检查硬编码白底/灰字 |
| 输入 | 鼠标、纯键盘、中文/日文输入法、超长多行文本 |
| 键盘 | Tab/Shift+Tab、Enter、Escape、F2、Shift+F10/Menu、Ctrl+S/O/F/Z/Y |
| 辅助技术 | NVDA 或 Narrator：表格列、表单标签、错误、进度和异步完成状态 |
| 大数据 | 10,000 行、5,000 术语、50 队列任务、大型 EPUB |
| 故障 | 无网络、401、429、超时、磁盘满、只读文件、文件锁、损坏 EPUB/图片 |
| 会话竞态 | 翻译中编辑/导入/粘贴/关闭/切项目；旧事件不得污染新 session |
| 资源关闭 | 取消/退出后无残留请求、线程、子进程、after 回调或未关闭 response |

关键流程不能只靠截图验收。每个流程同时检查：可见结果、键盘焦点、模型值、保存文件、后台资源和重启恢复。

## 13. 参考资料

- PEP 8: <https://peps.python.org/pep-0008/>
- Python Tkinter threading model: <https://docs.python.org/3/library/tkinter.html#threading-model>
- Python Packaging User Guide: <https://packaging.python.org/en/latest/guides/writing-pyproject-toml/>
- WCAG 2.2 Contrast Minimum: <https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html>
- Web Interface Guidelines 规则源（本次环境未能拉取最新内容）：<https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md>
- 本地技能：`ui-ux-pro-max`、`web-design-guidelines`。
