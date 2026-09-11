# 超长上下文翻译开发文档

日期：2026-09-11。基于当前工作区代码编写，HEAD 为 `24f92d2`，项目声明版本为 1.6.2。

本文是交给开发 AI 执行的实现规格，当前交付仅新增文档，不代表功能已经实现。工作区已有多处未提交修改；实施时以工作区实际代码为准，保留已有修改，不重置或覆盖用户工作。

## 1. 目标与语义

在主界面、批量翻译界面分别新增“超长上下文翻译”按钮。用户可以在设置中填写上下文大小；两个入口复用现有翻译、调度、流式响应、行映射、保存和导出管道。

本期采用以下明确语义，开发 AI 不需要再次询问常规实现选择：

- “超长上下文”指单次翻译请求能容纳更多待译原文，取消应用为普通模式设置的 6,000/20,000 token 和 20/200 行固定上限。它仍按用户预算分批，文档总长度不受单次请求预算限制。
- 设置中的“单次上下文大小”单位为 token，表示输入与输出合计的预算，不是字符数、原文行数或仅输入预算。
- 用户预算没有应用自定的最大值；允许输入 65,536、131,072、1,048,576 等正整数。模型实际容量、输出容量和服务限流仍然有效，不能宣称 API 支持无限上下文。
- 复用当前每批独立请求语义：每批包含系统提示、术语表和该批待译原文。本期不新增聊天历史、跨批摘要、检索增强或全文参考窗口；未进入该批的文字不会自动成为模型上下文。
- 两个新按钮默认只处理未完成行，保留已有译文和人工修改。明确的重新翻译仍使用现有覆盖确认与备份流程。
- 批量超长模式固定为“一个队列任务的一次 attempt 一个会话”：该 attempt 的所有批次复用同一个会话/引擎实例，不同任务不得共享会话，也不能每批新建会话。
- 普通翻译入口保持现有默认策略；超长模式是一次运行/任务的明确选项，不能通过临时修改全局配置再恢复来实现。

取消的是固定小批次限制。安全余量、输出预留、模型容量校验和限流等待必须有独立且可解释的语义，不得借这些机制暗中恢复旧上限。

## 2. 已核实的代码现状

以下使用文件路径和符号定位，行号会随开发变化。

| 区域 | 文件与符号 | 与本功能直接相关的现状 |
| --- | --- | --- |
| 普通/队列默认策略 | `src/config/translation_profile.py` | 主界面最多 20 行、6,000 输入 token；队列默认 80 行、16,000 token，最多 200 行、20,000 token。输出固定预留 4,096 token。 |
| 配置持久化 | `src/config/config_manager.py`：`load_app_config`、`save_app_config` | 主动删除 `batch_max_input_tokens` 和 `queue_batch_max_input_tokens`；不能直接复用它们保存新设置。API 默认 `context_window_tokens=32768`；清除旧 `max_tokens` 字段。 |
| 主界面执行 | `src/ui/translation_controller.py`：`start_translation`、`continue_translation`、`_translate_worker` | 收集缺失行，生成 `run_id`，工作线程调用 `translate_fast_mode`，经事件邮箱写回。 |
| 主界面入口/预检 | `src/ui/main_window.py`：`_run_primary_action`、`_run_preflight`、`refresh_action_state` | 主按钮依文档状态改变行为；预检按旧 `TranslationOptions.batch_size` 估算，需要接入实际策略。 |
| 引擎 | `src/core/translator.py`：`translate_fast_mode`、`translate_bulk_mode`、`_translate_run` | 两种旧入口复用 `_translate`；主界面分批仍在 `_build_batch_ranges`。 |
| 预算 | 同上：`compute_input_token_budget`、`_translate_run` | 重复计算上下文预算，使用模型窗口减去固定输出预留及 1,024 余量，且有 `max(512, ...)`。 |
| 单批执行 | 同上：`_translate_batch`、`TranslationRunContext` | 已有提示词、术语表、标记协议、缓存、流式解析及 Hunyuan-MT 分支。运行上下文已有不可变快照。 |
| 批量入口 | `src/ui/concurrent_window.py`：`_start_all` | 调用 `ConcurrentTranslationManager.start_all()`，不经过主界面 controller。 |
| 批量管道 | `src/core/concurrent_manager.py`、`src/core/queue_scheduler.py` | Manager 为兼容门面；Coordinator 管理状态，`plan_batches` 规划，`execute_batch_job` 直接复用引擎 `_translate_batch`。只改 `translate_bulk_mode` 不会覆盖这条路径。 |
| 队列准备 | `QueueTranslationCoordinator._prepare_attempt_locked` | 使用全局 `QueuePolicy` 规划任务；需补任务自己的上下文策略。已有 `attempt_id`、不可变 `BatchJob`/`BatchOutcome`。 |
| 调度/限流 | `src/core/queue_provider.py`：`ProviderLimiter` | 主界面与队列共享额度；一次估算 token 超过非零 TPM 上限时，会一直无法取得许可。 |
| 服务建议 | `src/api/base_api.py`：`recommended_input_budget` | 限流压力大时建议预算缩至 75%，属于动态建议，不是模型上下文容量。 |
| 设置 UI | `src/ui/settings_window.py`、`src/ui/form_validation.py` | 现有批次行数控件有固定上限；通用整数控件支持范围校验，不能直接把新上下文值裁剪为旧范围。 |
| 项目恢复 | `src/domain/project.py`：`ModelSnapshot`；`src/infrastructure/project_repository.py` | 快照目前包含模型、语言、提示词/术语版本和 `batch_size`；新增字段要兼容旧项目。 |
| 估算 | `src/utils/token_estimator.py`：`estimate_tokens` | 无模型 tokenizer 的近似估算，不能当作精确容量保证。 |

额外注意：`src/application/translation_modes.py` 中已有稳定/均衡/快速/自定义枚举，表达的是执行档位；不要直接把“超长上下文”混进这个维度。`TranslatorEngineAdapter.translate_batch()` 当前未实际把 `TranslationOptions` 传给底层引擎，不能只给选项新增字段就认为接线完成。

## 3. 设置、交互与兼容规则

### 3.1 配置格式

在应用配置新增独立配置段，主界面和批量任务共享其默认值：

```json
{
  "long_context": {
    "schema_version": 1,
    "context_window_tokens": 32768
  }
}
```

这里的 `context_window_tokens` 是用户给本模式的总预算；API 配置已有的同名字段表示当前模型容量，两者不可混用。默认预算为 32,768，兼容当前默认模型容量；用户可自行增大。

设置窗口的“翻译设置”新增“超长上下文翻译”区域：

1. “单次上下文大小（token）”：可编辑正整数字段，可附带 32,768 / 65,536 / 131,072 / 262,144 / 1,048,576 快捷值，快捷值不是合法值白名单。
2. 说明：“包含提示词、原文和输出预留。仅用于超长上下文翻译；实际大小受当前模型容量影响。”
3. 显示当前模型配置容量及有效预算预览。API 设置中暴露已有 `context_window_tokens` 的编辑入口，使用户能够为自定义模型配置真实容量；随 API 预设保存、加载和切换。

模型容量配置是用户声明的值，不是假装已经查询服务端验证的能力。旧值 32,768 是默认配置，不是永久硬上限。不得根据模型名字包含某个字符串就推断百万 token 能力。

预算大于模型容量时允许保存，在预览与预检中明确显示“设置预算 X，按当前模型配置有效预算 Y”；不得静默改写用户保存值。换用大窗口模型后按新容量生效。

输入校验：

- UI 使用字符串接收输入，完整校验后转成整数；拒绝空值、0、负数、小数、布尔值和非数字内容，不通过 `int(1.5)` 等方式静默截断。
- 不设置人为 token 最大值，不用 `sys.maxsize`、无穷浮点数或巨大哨兵值表达不限。
- UI 校验失败时聚焦字段并保留待编辑内容，不将其自动裁剪为 6,000/20,000。
- 对磁盘旧配置的缺字段、错误类型、损坏值作可诊断的默认回退；未知较新 schema 不盲目重写。加载和保存使用同一个规范化入口，保证深层默认合并与幂等性。
- 保存失败遵循现有保存结果处理：不显示成功、不提前发布新运行默认值、不破坏旧文件；不新增存储路径。

### 3.2 两个新按钮

| 界面 | 行为 |
| --- | --- |
| 主界面“超长上下文翻译” | 翻译当前文档全部未完成行；沿用导入/API 校验、预检、运行锁和自动保存。过滤后的表格不是文档真相源。 |
| 批量界面“超长上下文翻译” | 对点击时现有、符合原 `start_all` 启动条件的未完成任务，提交带超长模式及预算快照的启动命令。运行中、取消处理中、保存中的任务不切换模式。 |
| 原“全部开始” | 保留普通模式；不受另一个任务选择超长模式影响。 |
| 继续/恢复 | 同一运行或暂停任务继承已捕获策略；显式点击另一模式入口重新开始时才为符合条件的任务创建新 attempt。 |
| 重试失败/自动补漏 | 继承原任务策略，只处理缺失或失败行；不回退普通预算。 |

运行状态需展示“普通/超长”、配置预算、有效预算；批量任务至少在详情中可见。主界面新按钮加入 `_set_control_states`、`refresh_action_state` 和关闭/切换文档逻辑。重复点击不能启动重叠运行。

小模型模式当前强制单行翻译，语义与超长批次冲突。本期明确互斥：开启小模型模式时禁用两个超长按钮并显示原因；程序调用入口也返回明确配置错误。不得静默覆盖小模型设置，或显示超长却仍逐行发送。

工具栏空间不足时分行布局，验证现有最小窗口尺寸和 Windows 高 DPI；不要依靠扩大固定窗口宽度隐藏布局问题。

## 4. 模块边界与数据契约

推荐增加以下少量模块。名称可在实现时微调，职责和依赖方向不能变化。

| 模块 | 职责 |
| --- | --- |
| `src/domain/translation_policy.py` | `ContextMode`、`TranslationContextPolicy`、`ResolvedContextBudget`、不依赖引擎的批次规划值对象。只依赖标准库。 |
| `src/config/long_context_config.py` | 配置默认值、版本化读写转换、严格校验；不依赖 Tk、队列或 API 客户端。 |
| `src/application/context_budget.py` | 解析用户预算与模型容量、计算输入输出预留、返回有效预算及限制原因。纯函数。 |
| `src/application/batch_planner.py` | 公共 token 分批算法，输入原文索引、实际协议开销和已解析策略，输出索引范围/计划；不引用 `core.BatchJob`。 |
| 既有引擎/队列适配层 | 将公共计划包装为 `TranslationBatchPlan` 或 `BatchJob`；继续使用既有调度和单批执行。 |

建议的最小类型契约（不是必须照抄的实现代码）：

```python
class ContextMode(str, Enum):
    STANDARD = "standard"
    LONG = "long"

@dataclass(frozen=True, slots=True)
class TranslationContextPolicy:
    mode: ContextMode = ContextMode.STANDARD
    context_window_tokens: int | None = None
    # None 只表示 STANDARD 由原策略解析；LONG 必须为正整数。

@dataclass(frozen=True, slots=True)
class ResolvedContextBudget:
    requested_context_tokens: int
    effective_context_tokens: int
    safety_margin_tokens: int
    max_batch_lines: int | None
    # LONG 的 max_batch_lines=None；不是 999999 等伪无限值。
```

实现时补充输出估算参数、模型输出容量及限制原因等实际需要的字段。执行中使用不可变策略/能力快照，不能让批次读取可变 UI 或共享配置。

关键接线要求：

1. 新入口参数采用带默认值的 keyword-only 参数，例如 `context_policy=...`；普通旧调用保持兼容。
2. 主界面 controller 将策略作为 worker 参数传递到引擎；`TranslationOptions`、协议和适配器凡被这条调用链使用，必须同步更新。复制选项时使用 `dataclasses.replace` 或完整构造，避免 `TranslationExecutionProfile.apply()` 丢失新字段。
3. 引擎可以增加薄入口，但不得复制 `_translate_run` / `_translate_batch`。普通和超长模式在策略解析之后走同一执行逻辑。
4. 队列命令、任务槽位、运行上下文都携带任务自己的策略。`QueuePolicy` 保持全局并发/公平调度职责，不能用更换全局 `QueuePolicy` 给某个任务切换模式。
5. `submit_command` 当前只有 kind/task_id；需要类型化命令载荷承载策略和目标任务集合。点击时冻结目标集合，之后新加入的文件不被意外纳入该次操作。
6. 保留 `run_id`/`attempt_id`、行绝对索引和 generation 校验；旧运行的流式事件、结果、准备完成消息不能污染新运行。
7. 本次快照包括提示词、术语、语言、模型和预算。设置改动仅影响新开始的运行；在途请求保持其已有客户端与限流器所有权。
8. 队列超长会话由 `_TaskSlot`/attempt 持有，并供其全部 `BatchJob` 复用。若底层客户端不线程安全，同一任务批次必须串行；确认线程安全后才可受控并发，但每个请求必须有独立流式上下文。会话句柄不得进入持久化快照、日志或跨 attempt 复用。
9. 取消、错误、终态和 Coordinator 关闭都必须幂等关闭 attempt 会话；迟到结果先校验 `attempt_id` 再释放请求资源。

纯应用模块不能反向导入 `src/core/translator.py`。如公共规划器需要协议 token 信息，由适配层传入实际请求协议信息，或提取一个独立协议构造模块；禁止复制出两份提示词开销算法。

## 5. 预算和分批算法

### 5.1 输入与输出共同计入预算

设 `U` 为用户总预算，`M` 为当前配置的模型总容量：

```text
C = min(U, M)
S = max(1024, ceil(C * 0.02))
I = 估算后的完整请求输入 token（系统提示 + 术语 + 原文 + 行标记 + 消息开销）
O = max(4096, ceil(本批带输出标记的待译文本估算 token * 1.5))
接受该批的条件：I + O + S <= C
```

`1.5`、`4,096`、`2%` 是本期可测试、集中定义的估算策略，不是模型容量事实。返回预算解析结果时保留其版本，后续可按语言/provider 调优。大上下文不能只预留固定 4,096 输出后塞满原文，否则长译文极易被截断。

若 provider 的最大输出容量已知，额外要求 `O <= model_max_output_tokens`，不满足则缩小批次；能力未知时明确是估算，在运行时处理截断。不强制新增在线模型元数据查询，也不要编造模型能力表。若引入容量字段，必须带来源并支持预设往返。

输出预留是规划参数，不能未经兼容设计就把用户总预算作为 API `max_tokens` 发送。保持当前各 provider 的请求格式；如为检测截断扩展响应元数据，使用每次请求局部结果，不能将 `last_finish_reason` 放在共享 API 实例上供并发请求读取。

### 5.2 统一规划规则

- 普通模式维持原行数/token 规则；超长模式不读取旧普通/队列行数上限，也不受 `batch_max_input_characters` 等历史小批次字段暗中限制。
- 按原文顺序贪心加入待译行，直到下一行无法满足预算。预算足够且输出可容纳时，可让整份文档成为一个批次。
- 两个界面使用同一个预算解析和规划算法；批次执行与并发策略可继续不同。
- 固定会话不等于自动累积聊天历史。本期每批仍是独立、可验证的行号请求；默认不把前一批内容拼入下一批历史，防止上下文随批次无限增长。
- 行 token 只计算一次，协议固定开销每次运行计算一次。不要每加一行就重新拼接整批并重新扫描；接近线性时间，避免 O(n²)。行号跨 999/9999 时按实际标记长度计费。
- 稀疏行通过绝对 `source_indices` 写回，批内标记是相对位置。不得把模型标记直接当文档行号。
- 引擎/队列当前“至少推进一行”的逻辑可能放行超预算单行：超长模式必须显式检查并返回可操作错误，不能无限循环、截断原文或越预算硬发。本期不新增段内切割与拼接管道。
- 系统提示加术语表已占满预算、或输出预留无法容纳任何一行时，返回预算不足错误，不用 `max(512, ...)` 强行变成正预算。
- 大任务规划在后台进行，分段检查取消。尤其避免在 Coordinator 状态锁下执行整份超长文档的昂贵规划；释放锁规划后，重新校验 attempt、取消和关闭状态才能提交结果。

### 5.3 可解释的动态限制

`recommended_input_budget()` 若导致超长模式缩小实际批次，需要返回并展示限制原因；不能覆盖用户保存值。只适用于新规划的批次，不重写在途工作。

TPM 配置非零且完整批次输入估算大于 TPM 时，本期采用“发送前报错并提示调小预算或调整限额”，禁止永远等待许可。`try_acquire` 的正常窗口等待仍沿用现有调度。测试必须区分临时用尽额度与单个请求永远不可能取得额度。

继续复用全局共享 `ProviderLimiterRegistry`、RPM/TPM、429/Retry-After 和 AIMD。不新增每文件内部线程池，不形成“文件并发 × 批次并发”。

## 6. 流式解析、错误与恢复

### 6.1 结果完整性

- 保留既有 `[LINE_...]` 协议及 Hunyuan-MT 分支，验证超过 999/9999 行的标记、跨 chunk 标记、乱序/重复/缺失/越界标记。冒号、换行和原文里相似标记不能导致写错行。
- 超长请求增加部分响应、超时和截断概率。provider 返回 `finish_reason=length` 等截断信号时，必须通过结构化信息传到结果判断，不能仅凭请求 HTTP 成功宣告整批成功。
- 复用现有 `OperationStatus`/`BatchTranslationResult`：只有验证完整的行能写为已完成；半行、缺失行不能被补齐假成功。无法判断完整性的行保持缺失/失败并进入现有补漏流程。
- 缓存键继续依赖真实发送内容、模型、语言、提示词和术语版本。策略改变导致请求内容改变时必然区分缓存；若请求内容和语义完全相同，允许复用。任何部分/截断结果不得作为完整成功缓存，新协议行为需升级缓存语义版本。
- 超长流式预览沿用合并/节流邮箱，不能每个 token 把整批数万行复制到 Tk；UI 预览与已提交译文仍分离。队列保持快照驱动，不引入逐 token UI 回调。

### 6.2 错误策略

本期采用明确失败加现有重试/补漏机制，不额外实现自动二分重试树。

| 场景 | 必须行为 |
| --- | --- |
| 模型拒绝上下文过长/输出截断 | 显示模型与有效预算、建议减小上下文或更换模型；保留已经验证的成功结果。相同超容量请求不作无意义无限重试。 |
| 429/网络超时/可重试服务错误 | 复用现有有限重试与退避，受取消信号控制；不能在新层重复套重试造成次数相乘。 |
| 401/403、无效模型/端点 | 返回配置/认证错误；不自动拆批掩盖问题。 |
| 用户停止 | 尽快停止规划、限流等待与流式请求；状态为取消，释放资源，保留成功行，不弹“全部完成”。 |
| 保存失败 | 延续检查点/dirty 状态及重试保存入口，不能报已保存。 |

超长请求保持现有首字等待提示、心跳和超时配置。HTTP read timeout 是无数据间隔，不应被实现成整个长请求的固定总时限；不设置无限超时来掩盖连接停滞。

### 6.3 暂停、重启与项目快照

将上下文模式、用户预算和预算策略版本序列化到既有项目配置快照；有效模型容量可用于诊断记录，但恢复时要校验当前配置容量。旧项目缺字段按普通模式读取，不触发全量重译。

- 同一暂停 attempt 恢复：继承预算及模式。
- 批量超长任务暂停时保留会话归属。若 provider 会话不能安全跨暂停或重启存活，则恢复时为同一 attempt 创建一个新会话，但该 attempt 的剩余批次仍只使用这个新会话，不能按批次轮换。
- 重启应用后恢复：继承保存的模式和用户预算，重新进行当前模型/密钥配置预检；配置改变时按现有恢复机制处理，展示有效预算变化，不静默回退普通模式。
- 显式用不同模式重试未完成任务：创建新 attempt，仅重规划未完成行。
- TXT 复用项目仓库；EPUB 在既有项目/映射持久化流程增加必要元数据，保持旧映射可读、generation 原子发布、独占写锁和资源导出约定。实现前核实两条恢复路径实际如何持久化模式，不能只改 `ModelSnapshot` 而漏掉 EPUB。
- 不持久化 API Key，不新增包含整份请求内容的诊断日志。记录 task/run/attempt、预算、批次行数、输入估算和状态足以定位问题。

## 7. Python 与工程规范

1. 遵循 `pyproject.toml`：Python 3.10、3.12、3.13；项目明确排除 3.11。不得使用仅 3.11+ 的 `StrEnum` 等 API。
2. 新公共函数有类型注解、清晰单位和异常契约；不可变配置用 `@dataclass(frozen=True, slots=True)`，默认可变容器用 `default_factory`。
3. `domain` / `application` 不依赖 Tk、httpx、文件读写和具体引擎；通过值对象、函数参数及必要的 `Protocol` 注入，遵循现有 Pyright 严格门禁。
4. 配置默认值、规范化和预算算法各有单一来源。不在巨型 `main_window.py` / `translator.py` / `queue_scheduler.py` 中堆入复制算法，也不为了抽象创建第二套翻译框架。
5. 校验和可预期错误使用具体异常，跨层转换保留异常链。宽捕获只留在现有线程/命令边界，必须报告状态和脱敏错误。
6. 网络、长规划和磁盘保存不阻塞 Tk；工作线程不读写 Tk 变量或控件。继续复用事件泵、取消事件、单飞保存和资源 `close()` 生命周期。
7. 数值估算尽量使用整数运算，避免巨大用户整数转 float 溢出；对实际文档按需分配，不按用户 token 数预分配巨型列表/缓冲区。
8. 不引入本功能不需要的新依赖，不重排全仓格式，不清理无关历史问题，不修改真实用户配置作为示例。

## 8. 实施阶段与交付顺序

### 阶段 A：契约和配置

交付领域策略、配置规范化、总预算公式、边界错误与旧配置迁移测试。确认 UI、预检和运行时使用同一套单位。审阅所有旧 token/行数裁剪点，列出保留于普通模式和旁路于超长模式的具体分支。

### 阶段 B：公共规划与引擎

提取共用规划器，将主界面引擎接入任务策略；保留普通模式行为与单批执行代码。处理输出预留、不可容纳单行、实际协议估算及截断完整性。用 fake provider 证明发出的真实请求能超过旧上限。

### 阶段 C：批量调度与持久化

扩展类型化队列命令及每任务策略，接入相同规划器、TPM 不可满足错误和模式快照。验证混合模式并行、取消/暂停、迟到结果丢弃、TXT/EPUB 恢复。不得只接入旧 bulk 入口。

### 阶段 D：设置与两个界面

接入设置字段、容量配置、预检预算展示、两个按钮、按钮状态和任务详情。按本文件确定的小模型互斥、默认缺失行范围、恢复语义实现，不另建平行 controller。

### 阶段 E：验证与用户说明

完成下述测试和验收，更新 README 的使用说明与上下文单位说明。在本文件末尾追加“实现结果”记录实际模块/入口、测试命令与结果、未完成事项；不要把未实现事项标为已完成。

## 9. 测试计划与验收标准

新增测试可按 `test_long_context_config.py`、`test_context_budget.py`、`test_long_context_translation.py`、`test_long_context_queue.py`、`test_long_context_ui.py` 组织。重点验证可观察行为，不用字符串搜索源码或只断言常量的测试替代端到端接线。

| 编号 | 验证场景 | 通过条件 |
| --- | --- | --- |
| LC-01 | 保存 131,072 / 1,048,576，重新初始化 ConfigManager | 用户值完整往返，不被旧 token 字段清理删除或裁剪。 |
| LC-02 | 旧配置、损坏类型、0/负数/小数/布尔值、未知 schema | 按规范校验或迁移；写盘失败不发布新配置；配置外其他字段保持兼容。 |
| LC-03 | 同一输入分别普通/超长运行 | 在模型/输出预算允许的夹具下，超长真实请求超过 20 行和 6,000 输入 token；普通仍遵守原限制。 |
| LC-04 | 批量超长模式 | 经真实 Coordinator/worker 路径捕获到超过 200 行及 20,000 输入 token 的请求，不能只测试策略对象。 |
| LC-05 | 固定模型能力下增减用户预算 | 实际分批随预算变化；超模型预算时清楚展示限制原因，不改保存值。 |
| LC-06 | 超大术语表、输入恰好到边界、单行过大、已知输出容量较小 | 预算含全部协议开销和输出；不越界、不吞原文、不死循环；错误能指导处理。 |
| LC-07 | 1,001/10,001 行、多字节、emoji、稀疏缺失行 | 行号解析及绝对映射正确，乱序完成不改变原文顺序。 |
| LC-08 | stream 跨 chunk、重复/缺失/越界标记、`finish_reason=length` | 完整行保留，残行不标完成，失败索引准确，不写入成功缓存。 |
| LC-09 | 普通与超长任务共存，同时更改设置 | 各任务策略独立，在途任务不变；新任务读新值，不突破全局并发额度。 |
| LC-10 | 取消发生在规划、许可等待、首字等待、流式中；快速再启动 | 及时停止，旧事件/结果丢弃，资源释放，无重复写回；按钮恢复可用。 |
| LC-11 | 请求估算大于 TPM，或本分钟额度暂时用尽 | 前者明确失败，后者正常等待可取消，不永久挂起。使用假时钟避免测试真实等待一分钟。 |
| LC-12 | 部分成功后暂停/取消/应用重启，分别恢复 TXT/EPUB | 只翻译缺失行，保留人工修改，模式及用户预算恢复，检查点和导出有效。 |
| LC-13 | UI 预检与真实分批 | 使用相同策略和估算，显示的模式、有效预算与运行一致；无无条件额外确认弹窗。 |
| LC-14 | 小模型模式、空文档、全部已完成、重复点击、窗口关闭 | 入口状态与服务边界一致，无第二条重叠运行。 |
| LC-15 | 大文档规划与流式性能 | 用固定数据与假 API 记录运行时间/峰值内存和事件数量；数据翻倍不存在反复全量扫描造成的明显平方增长；UI 可响应取消。 |

LC-03/04 的 fake 模型容量应足够大，例如总容量 1,048,576，并设置足够输出容量。不要让夹具本身的模型输出上限导致“无法超过旧限制”的假失败。测试至少覆盖 SiliconFlow、DeepSeek、OpenAI-compatible 的现有适配路径及 Hunyuan 特殊分支；不依赖真实付费 API。

回归优先覆盖现有配置/预设、引擎性能与取消、流式 UI、single-flight、队列规划与限流、检查点、TXT 恢复、EPUB 导入/导出与映射测试。先跑针对性测试，再完成仓库既有门禁：

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pyright
python -m pytest -q
```

CI 当前矩阵为 3.10/3.12/3.13，3.12 另有覆盖率检查。执行时遵循当时实际 CI 配置。记录工作区基线失败、环境导致的跳过与本次新增失败；未执行的检查不能写成通过，也不得通过删除/弱化既有测试使门禁变绿。

完成定义：LC-01 至 LC-15 的行为均有验证，两个实际按钮能触发各自真实管道，设置能持久化，普通模式无回归，模型/输出/TPM 错误有明确状态，TXT/EPUB 恢复可用，并提交可审阅的实现说明。只增加按钮、常量或字段不算完成。

## 10. 可直接交给开发 AI 的 /goal 指令

在本项目工作区打开其他开发 AI 后，复制以下内容：

```text
/goal 按 docs/_LONG_CONTEXT_TRANSLATION_IMPLEMENTATION_GUIDE.md 完整实现超长上下文翻译。先阅读实际工作区代码、适用 AGENTS.md 和现有测试，保留已有未提交修改。新增主界面与批量翻译界面的“超长上下文翻译”按钮，以及可持久化的用户上下文大小设置。取消超长模式中的应用固定 token/行数上限，按文档定义的用户总预算、模型容量和输出预留分批；复用现有引擎、全局队列、ProviderLimiter、流式解析、行映射、保存和导出管道。实现任务级不可变策略、配置兼容、小模型互斥、暂停恢复和重试继承，避免全局配置串扰。遵循 Python 版本约束、类型检查和模块化边界，完成 LC-01 至 LC-15 的行为验证及现有 CI 门禁，更新 README 并在开发文档末尾记录实现结果。持续完成代码、接线、测试与文档，不止于计划或只改 UI；如有无法完成的检查，明确列出原因及证据，不宣称已通过。
```

本文已确定默认值、单位、按钮范围及互斥策略。实施中可自行完成必要的小范围结构调整；遇到代码现状与本文不同，先核实并记录依据，在不改变用户目标和完成定义的前提下适配。



---

# 实现结果

日期：2026-09-11。基于工作区 HEAD `24f92d2` 与既有未提交修改实现；未重置或覆盖用户工作。

## 新增模块

| 模块 | 内容 |
| --- | --- |
| `src/domain/translation_policy.py` | `ContextMode`、`ContextLimitReason`、`TranslationContextPolicy`、`ResolvedContextBudget`、`coerce_context_window_tokens`、`long_context_safety_margin`、`long_context_output_reserve`。只依赖标准库。 |
| `src/config/long_context_config.py` | `long_context` 配置段的默认值、版本化规范化与严格校验（加载/保存共用同一入口）。 |
| `src/application/context_budget.py` | 预算解析（`resolve_standard_budget` / `resolve_long_budget` / `resolve_context_budget`）、输出额度、`describe_budget` 可解释文案、`is_unsatisfiable_for_provider` TPM 可行性。纯函数。 |
| `src/application/batch_planner.py` | 公共 token 分批算法 `plan_batches`、`ProtocolOverhead`、`PlannedBatch`、`BatchPlanningError`、`estimate_batch_input_tokens`。不引用 `core.BatchJob`。 |

## 既有模块接线

| 区域 | 改动 |
| --- | --- |
| `src/config/config_manager.py` | 新增 `long_context` 段（默认值 + 规范化 + 保存），`get_long_context_window_tokens` / `set_long_context_window_tokens`；API 预设新增可选 `context_window_tokens` 与 `base_url` 往返。 |
| `src/domain/project.py` | `ModelSnapshot` 新增 `context_mode` / `context_window_tokens` / `context_policy_version`，旧项目缺字段按普通模式读取。 |
| `src/core/translator.py` | 新增 `resolve_run_budget`、`translate_long_context_mode`；`_translate` / `_translate_run` 接受 keyword-only `context_policy`；`TranslationRunContext.budget` 携带不可变预算快照；批次规划改用 `application.batch_planner`；TPM 不可满足与预算不足在发送前失败；截断信号（`finish_reason=length`）进入结果判断。 |
| `src/core/translation_result.py` | `BatchTranslationResult` 新增 `output_truncated` / `finish_reason`。 |
| `src/api/base_api.py` | 流式解析记录每线程 `finish_reason`（`last_finish_reason()`），不放在共享实例上。 |
| `src/core/queue_scheduler.py` | `_TaskSlot` 新增任务级 `context_policy` / `resolved_budget`；新增 `LongContextStartRequest` 类型化命令载荷与 `start_all_long` 命令；新增 `plan_long_context_batches`；TPM 不可满足时任务进入 ERROR 而非永久空转；上下文模式随项目快照持久化并恢复。 |
| `src/core/concurrent_manager.py` | 新增 `start_all_long_context(task_ids, context_policy)`。 |
| `src/ui/settings_window.py` | 「翻译设置」新增「超长上下文翻译」区域（正整数校验 + 快捷值 + 有效预算预览）；「API 配置」新增「模型容量（token）」并随预设往返。 |
| `src/ui/form_validation.py` | 新增 `validate_positive_int_text` / `parse_positive_int_text`（无上限、不静默截断、拒绝全角数字）。 |
| `src/ui/translation_controller.py` | 新增 `start_long_context_translation`；运行级策略按 `run_id` 冻结，三个 worker 共用 `(content, run_id)` 签名；超长按钮纳入 `_set_control_states`。 |
| `src/ui/main_window.py` | 工具栏新增「超长上下文翻译」按钮、`_refresh_long_context_button_state`、`_start_long_context_translation`、`_long_context_preflight_note`（预检与运行时共用预算解析）。 |
| `src/ui/concurrent_window.py` | 工具栏新增「超长上下文翻译」按钮，冻结点击时的目标任务集合并携带预算快照。 |

## 语义确认

- 超长模式取消的是应用固定的小批次限制（6,000/20,000 token、20/200 行）；模型容量、输出容量与服务限流仍然有效。
- 预算公式集中在 `src/application/context_budget.py`：`C = min(U, M)`、`S = max(1024, ceil(C×2%))`、输入额度 `C - S`、输出额度 `max(4096, ceil(本批待译文本×1.5))`，接受条件 `I + O + S <= C`。全部使用整数运算。
- 普通模式沿用旧语义（固定 4096 输出预留、`max(512, ...)` 下限、"至少推进一行"），未引入回归；`plan_batches` 仅在 LONG 模式下对不可容纳的单行抛 `BatchPlanningError`。
- 小模型模式与超长模式互斥，UI 与服务边界使用同一消息，不静默覆盖任一方设置。
- 每批仍是独立行号请求，不累积聊天历史；未进入该批的文字不成为模型上下文。

## 测试与结果

新增测试 8 个文件、210 个用例：

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `tests/test_long_context_config.py` | 30 | LC-01、LC-02 |
| `tests/test_context_budget.py` | 36 | LC-05、LC-06（预算与规划算法） |
| `tests/test_long_context_boundaries.py` | 28 | LC-06、LC-11（术语表、精确边界、单行过大、输出容量、TPM） |
| `tests/test_long_context_translation.py` | 21 | LC-03、LC-07、LC-08（主界面引擎真实请求） |
| `tests/test_long_context_queue.py` | 29 | LC-04、LC-09、LC-10、LC-11、LC-12（真实 Coordinator/worker 路径） |
| `tests/test_long_context_controller.py` | 22 | LC-03、LC-10、LC-14（controller 接线） |
| `tests/test_long_context_ui.py` | 28 | LC-13、LC-14（校验、预检一致性、互斥） |
| `tests/test_long_context_cancellation.py` | 16 | LC-10、LC-15（四阶段取消、规模性能） |

LC-03 / LC-04 在夹具中把模型容量设为 1,048,576，并通过**捕获 provider 实际收到的请求内容**证明：主界面真实请求超过 20 行与 6,000 输入 token，队列真实请求（经 `QueueTranslationCoordinator` + 真实 `TranslatorEngine`）超过 200 行与 20,000 输入 token。

执行的检查：

```text
python -m ruff check .                -> All checks passed!
python -m pytest tests/ -q            -> 1451 passed, 1 failed（见下）
python -m pytest <8 个新文件> -q      -> 210 passed
```

### 未执行的检查与失败项（如实记录）

1. **Pyright 未执行**：`pyright==1.1.411` 在本机离线环境中无法安装
   （`pip install pyright` 报 `No matching distribution found for nodeenv>=1.6.0`），
   因此 `python -m pyright` 未运行。**不能声称类型检查通过。**
2. **`python -m ruff format --check .` 未通过（基线既有）**：10 个文件需要重排，
   全部是本次实现之前就存在的格式化差异（`main.py`、`src/application/storage_settings.py`、
   `tests/test_autosave_coordinator.py`、`tests/test_document_session.py`、
   `tests/test_p0_3_missing_run_id.py`、`tests/test_translation_event_mailbox.py`、
   `tests/test_txt_queue_recovery.py`、本说明文档等）。本实现新增/修改的文件均已格式化。
   未做全仓重排（实现指南 §7.8 要求不重排全仓格式）。
3. **`tests/test_settings_form_validation.py::FormValidatorTests::test_clamp_field_handles_invalid_input`
   失败（基线既有）**：在 `git stash --include-untracked` 的干净基线上同样失败，
   与本功能无关，未修改该测试。
4. **`tests/test_p2_4_large_lists.py` 存在 Tk 交互抖动**：同一用例单独运行通过，
   在全量套件中约 1/3 的运行会因 `_tkinter.TclError: invalid command name
   "tcl_findLibrary"` 或相邻用例污染而失败，失败用例名每次不同。在干净基线上也可复现。
   未修改这些测试，也未通过删除/弱化测试让门禁变绿。
5. CI 矩阵（3.10/3.12/3.13）与覆盖率检查未在本地执行；本地解释器为 Python 3.14。
   代码遵循 `requires-python = ">=3.10,!=3.11.*"`（未使用 `StrEnum` 等 3.11+ API）。

### 已知未完成 / 保留事项

- `max_output_tokens`（模型输出容量）为可选 API 配置字段：程序读取它并据此收紧输入额度，
  但设置界面尚未提供编辑入口（未声明时按"能力未知"处理，运行时依赖截断信号）。
  实现指南明确"不强制新增在线模型元数据查询，也不要编造模型能力表"。
- `ContextLimitReason.MODEL_OUTPUT_CAPACITY` 与 `PROVIDER_PRESSURE` 的记录路径已实现并有测试，
  但设置预览只展示模型容量维度；provider 限流建议的影响在运行状态栏可见。
- EPUB 恢复路径复用同一 `ModelSnapshot` 持久化（`context_mode` / `context_window_tokens`），
  现有映射读写、generation 原子发布与独占写锁未改动；未新增 EPUB 专属的上下文模式字段。
