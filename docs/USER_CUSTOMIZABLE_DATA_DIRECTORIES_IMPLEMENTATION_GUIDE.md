# 用户自定义数据目录功能实施指导（草案）

> 文档用途：为设置页增加数据目录自定义能力提供实施参考。本文是设计和开发指导，不是最终产品需求；其中带有“可调整”的目录名、配置键和界面文案，可以根据实际版本修改。

## 1. 需求目标

允许用户在“设置”中选择一个统一缓存根目录，用于集中保存：

1. 缓存文件
2. 翻译中间记录
3. 译文备份
4. TXT/EPUB 项目恢复数据

设置页不再要求用户分别选择多个目录。用户只选择一次“统一缓存目录”，程序在其中自动建立 `cache`、`translation_records` 和 `translation_backups` 子目录。底层继续识别旧版本的独立目录字段，但新设置保存后统一改为单根目录模式。

本功能需要满足以下原则：

- 设置生效后，所有新建文件必须使用新路径，不能只修改界面上的显示值。
- 已有数据不能因为改目录而丢失；首次切换时应提供“迁移 / 保留原位置 / 取消”选择。
- 翻译进行中不允许直接切换正在使用的目录，或者必须先暂停并完成一次保存。
- 目录不可写、空间不足、路径不存在、路径冲突时，要给出明确错误，并保留旧配置。
- 配置保存失败时，不能让界面显示“已切换”，实际运行却仍写入旧目录或半新半旧目录。
- 路径必须使用绝对路径保存，避免程序从不同工作目录启动时产生不同结果。

## 2. 当前项目现状

当前项目已经有统一路径对象 `src/app_paths.py`，启动时创建以下路径：

| 当前对象 | 当前用途 | 当前默认位置 |
| --- | --- | --- |
| `resource_dir` | 程序资源、图标等只读文件 | 项目根目录或 PyInstaller 资源目录 |
| `data_dir` | 用户数据根目录 | Windows `%APPDATA%/LightNovelTranslator` 等平台目录 |
| `config_dir` | `app_config.json`、API 元数据、术语表等配置 | `data_dir/config` |
| `workspace_dir` | EPUB 导入映射和图片等工作文件 | `data_dir/workspace` |
| `log_dir` | 日志 | `data_dir/logs` |

当前与本需求最相关的实际写入位置：

- TXT 翻译项目和跨重启记录：`data_dir/projects`，由 `ProjectRepository` 管理。
- EPUB 中间文件：`workspace_dir/mappings/<project_id>`，包含 `content_mapping.json`、`images.json`、`format_info.json` 等。
- 运行时缓存：`src/core/smart_cache.py` 同时提供内存 LRU 和可选磁盘持久化。组合根会把解析后的 `cache_dir` 注入主编辑器与后台队列的翻译引擎；缓存文件只保存哈希键、译文和过期时间，不保存原文。
- 译文备份：项目已有检查点机制，例如 `projects/<project_id>.ckpt_*.json`。主编辑器覆盖已有 TXT 或 EPUB 译文前，还会把旧文件复制到解析后的 `translation_backups_dir`；备份失败会取消覆盖，避免静默丢失最后一个版本。
- 设置本身：`config_dir/app_config.json`。不建议让程序在启动前依赖同一个需要从设置页才能确定的目录配置。

相关代码入口：

- 路径解析：`src/app_paths.py`
- 启动组装：`src/bootstrap.py`
- 应用配置：`src/config/config_manager.py`
- 设置界面：`src/ui/settings_window.py`
- TXT 项目持久化：`src/infrastructure/project_repository.py`
- EPUB 中间文件：`src/core/epub_processor.py`
- 内存缓存：`src/core/smart_cache.py`

## 3. 推荐目录模型

### 3.1 默认目录结构

假设用户选择的统一缓存根目录为：

```text
D:/AITranslatorData
```

推荐自动生成以下子目录：

```text
D:/AITranslatorData/
├── cache/                 # 持久化翻译缓存（如启用磁盘缓存）
├── translation_records/   # TXT 项目、EPUB 映射、翻译中间状态
│   ├── projects/
│   └── mappings/
├── translation_backups/   # 译文备份和历史版本
├── logs/                  # 日志，可选择跟随数据根目录
└── config/                # 可选：仅在产品决定迁移配置时使用
```

建议不要把用户导入的原始 TXT/EPUB 和应用中间数据混在一起。源文件仍由用户选择并保留在原位置，数据目录只保存恢复翻译所需要的状态和备份。

### 3.2 配置字段

`app_config.json` 保留现有稳定配置段。新设置页只写入 `data_root`；其余路径字段用于兼容旧版本配置，不再展示给用户：

```json
{
  "storage": {
    "schema_version": 1,
    "data_root": "D:/AITranslatorData",
    "cache_dir": "",
    "translation_records_dir": "",
    "translation_backups_dir": "",
    "logs_dir": "",
    "path_mode": "root_with_overrides"
  }
}
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| `data_root` | 用户选择的统一缓存根目录；为空表示使用平台默认目录 |
| `cache_dir` | 旧版兼容字段；新设置保存时清空，最终使用 `<data_root>/cache` |
| `translation_records_dir` | 旧版兼容字段；新设置保存时清空，最终使用 `<data_root>/translation_records` |
| `translation_backups_dir` | 旧版兼容字段；新设置保存时清空，最终使用 `<data_root>/translation_backups` |
| `logs_dir` | 可选。若不想让日志跟随数据根目录，可以保留为空并继续使用 `AppPaths.log_dir` |
| `path_mode` | 记录当前解释规则，便于将来升级配置格式 |

子目录字段使用空字符串表示“跟随统一缓存根目录”，不重复保存派生出的绝对路径。这样用户更换一次 `data_root` 后，所有相关目录都会自动跟随移动。

### 3.3 路径解析优先级

统一由一个路径解析对象计算最终路径，不要让 UI、翻译队列和导出器分别拼接字符串：

```text
旧版显式子目录配置
    > data_root 下的默认子目录
    > 平台默认 AppPaths 目录
```

建议新增 `StoragePaths` 或扩展 `AppPaths`，提供类似以下属性：

```python
storage_paths.cache_dir
storage_paths.translation_records_dir
storage_paths.translation_backups_dir
storage_paths.projects_dir
storage_paths.mappings_dir
```

所有调用方只读取这些属性，不直接读取 `storage` 配置，也不在业务模块中写 `Path(data_root) / "projects"`。

## 4. 需要明确的产品决策

开发前请先确定以下问题，否则迁移和恢复行为容易不一致：

### 4.1 缓存持久化策略

当前实现采用“内存 LRU + 可选 JSON 持久化”方案：启用翻译缓存时，主编辑器和后台队列会在用户设置的 `cache_dir` 下创建按 Provider/模型隔离的缓存文件。应用重启后会加载未过期条目；缓存写入失败会退化为内存缓存，不会阻断翻译。

缓存和翻译中间记录目录保持独立。清理缓存只清理缓存索引，不得删除 TXT 项目、EPUB 映射或译文备份。若将来需要更高并发或更大缓存容量，可把 JSON 索引替换为 SQLite，但仍应沿用同一个 `cache_dir` 配置字段。

### 4.2 “翻译中间记录”包含哪些数据

建议统一包含：

- TXT 的 `project_id.json`、`recent_projects.json`、checkpoint 文件。
- EPUB 的 mapping 目录及其内容映射、图片资源、格式信息。
- 任务恢复所需的状态、源文件指纹、模型配置快照和失败行信息。

如果某类数据不能用于恢复任务，应放在缓存或日志目录，而不是中间记录目录。

### 4.3 “译文备份”保存什么

建议备份至少包含：

- 原始目标文件的完整副本，或完整的译文状态快照。
- 创建时间、来源文件路径、源文件指纹、项目 ID。
- 备份格式版本。

备份文件名不要直接使用用户输入的完整文件名拼接路径。应使用项目 ID 和时间戳，例如：

```text
<project_id>.backup_20260724_153000.json
```

## 5. 推荐实现步骤

### 第一步：扩展路径层

修改 `src/app_paths.py`，增加以下能力：

1. 读取 `app_config.json` 中的 `storage` 配置。
2. 规范化路径：展开环境变量、展开用户目录、转为绝对路径并 `resolve()`。
3. 校验路径不能是普通文件。
4. 创建必要目录，并在创建失败时抛出结构化异常。
5. 返回一个不可变的路径对象，供其他模块使用。

注意：启动时 `ConfigManager` 本身需要先找到配置文件，所以建议分两阶段初始化：

```text
1. 用平台默认路径启动，读取最小配置
2. 解析 storage.data_root 和各覆盖目录
3. 创建 StoragePaths
4. 用 StoragePaths 初始化项目仓库、EPUB 处理器、备份仓库和缓存仓库
```

不要在 `AppPaths.create()` 内部反过来依赖一个必须由 `ConfigManager` 读取的配置文件，否则会形成启动循环。

### 第二步：定义路径解析和校验服务

建议新增一个独立模块，例如：

```text
src/infrastructure/storage_paths.py
```

建议提供以下接口：

```python
class StoragePaths:
    def resolve(self, config: dict) -> ResolvedStoragePaths: ...
    def validate(self, paths: ResolvedStoragePaths) -> ValidationResult: ...
    def ensure_directories(self, paths: ResolvedStoragePaths) -> None: ...
```

校验至少包括：

- 路径为空时使用默认目录。
- 路径是相对路径时转为绝对路径，或直接拒绝并提示用户。
- `cache_dir`、`translation_records_dir`、`translation_backups_dir` 不能是同一个目录，除非产品明确支持。
- 子目录不能覆盖源文件路径。
- 目标目录可创建、可写、可读取。
- 可选检查剩余磁盘空间。
- 路径包含 Windows 不允许的字符时给出可操作提示。

### 第三步：迁移各业务模块

按以下映射替换硬编码路径：

| 现有写入方 | 改造目标 |
| --- | --- |
| `ConcurrentTranslationManager` 中的 `data_dir / "projects"` | `storage_paths.projects_dir` |
| `EPUBProcessor` 中的 `workspace_dir / "mappings"` | `storage_paths.mappings_dir` |
| 项目 checkpoint | `storage_paths.translation_records_dir` 或其 `projects` 子目录 |
| `FileHandler.backup_file()` | 改为接收备份目标目录，不能总是写到源文件旁边 |
| 将来新增的持久化缓存 | `storage_paths.cache_dir` |
| 日志初始化 | 按产品决定使用 `log_dir` 还是 `storage_paths.logs_dir` |

尤其要避免只修改 `EPUBProcessor` 而遗漏 `ProjectRepository`。否则 EPUB 和 TXT 的恢复记录会落到不同根目录，用户迁移后只能恢复其中一部分。

### 第四步：增加设置页

在 `src/ui/settings_window.py` 的“数据与存储”页只保留一个路径输入：

| 控件 | 建议行为 |
| --- | --- |
| 统一缓存目录输入框 | 显示用户选择的根目录；留空表示平台默认目录 |
| “选择文件夹”按钮 | 使用目录选择对话框 |
| 最终路径预览 | 显示自动派生的缓存、中间记录、项目和备份目录 |
| “打开目录”按钮 | 打开最终解析后的统一缓存根目录 |
| “恢复默认”按钮 | 清除自定义值，恢复平台默认目录 |
| 当前占用空间 | 可选，后台统计，不能阻塞设置窗口 |

按钮文案可按现有 UI 风格修改。路径较长时，输入框可以显示截断文本，但应通过 Tooltip 或复制功能保留完整路径。

保存前应先显示最终路径预览，例如：

```text
缓存：D:/AITranslatorData/cache
中间记录：D:/AITranslatorData/translation_records
译文备份：D:/AITranslatorData/translation_backups
```

### 第五步：实现切换和迁移

点击“应用”或“保存”时，建议执行以下流程：

```text
检查是否有运行中的翻译任务
    ├─ 有：提示暂停/等待保存完成，禁止直接切换
    └─ 无：继续

解析并校验新路径
    ├─ 失败：显示原因，保留旧配置
    └─ 成功：继续

检测新目录是否已有数据
    ├─ 空目录：询问是否迁移旧数据
    ├─ 有同版本数据：询问合并或使用现有数据
    └─ 无法识别：禁止静默覆盖

执行迁移到临时目录
    ├─ 失败：清理临时目录，旧配置继续生效
    └─ 成功：写入迁移清单并原子发布

保存配置
    ├─ 失败：回滚路径状态，提示用户
    └─ 成功：重建相关仓库/服务并刷新界面
```

迁移建议采用“复制后校验，再切换”的方式，而不是先移动后尝试恢复。复制内容后至少校验文件数量和每个文件的大小；重要文件可以校验 SHA-256。

推荐迁移目录结构：

```text
新数据根目录/.migration/
├── migration.json
├── staging/
└── committed
```

迁移清单至少记录：

```json
{
  "schema_version": 1,
  "source_root": "旧目录",
  "target_root": "新目录",
  "status": "staging",
  "copied_files": [],
  "failed_files": [],
  "created_at": "2026-07-24T15:30:00"
}
```

配置只有在全部关键数据迁移完成后才写成新路径。程序启动时如果发现 `status` 为 `staging` 或 `partial`，应继续恢复或提示用户选择，不要直接把新目录当作完整数据目录。

## 6. 运行时切换策略

推荐本版本只允许“保存设置后，下次启动生效”，原因是路径对象、项目仓库、EPUB 处理器和后台队列都可能持有旧目录引用。这样实现和测试更稳定。

如果产品必须立即生效，应满足：

1. 停止接收新任务。
2. 等待后台保存完成。
3. 关闭旧的项目仓库、缓存仓库和 EPUB 工作区句柄。
4. 完成迁移并创建新的路径对象。
5. 重新初始化依赖路径的服务。
6. 恢复 UI 中的最近项目和任务列表。

无论采用哪种策略，当前打开的项目都必须保存项目 ID、源文件指纹和 mapping 路径，不能只保存一个旧的绝对路径。

## 7. 备份和恢复建议

备份应使用独立的 `BackupRepository`，不要把“备份”逻辑散落在翻译控制器、文件处理器和导出器中。

建议接口：

```python
class BackupRepository:
    def create_backup(self, project, reason: str) -> Path: ...
    def list_backups(self, project_id: str) -> list[BackupInfo]: ...
    def restore_backup(self, backup_id: str) -> RestoreResult: ...
    def delete_backup(self, backup_id: str) -> bool: ...
```

写入要求：

- 使用现有的原子 JSON 写入能力。
- 先写临时文件，再 `fsync`，最后原子替换。
- 备份失败时保留主记录的 dirty 状态。
- 备份数量或总大小应有上限，超限时按时间清理最旧备份。
- 清理缓存不能清理备份；删除项目时也应明确询问是否删除备份。

## 8. 安全和兼容性

- 不要把 API Key、系统凭据或 keyring 内容复制到用户选择的普通数据目录。
- 日志可能包含文件名和错误信息，若移动日志目录，应继续遵守现有脱敏规则。
- 通过符号链接、`..` 和不同大小写路径访问文件时，应在最终路径上做 `resolve()` 和边界校验。
- 路径配置写入失败时，旧的 `app_config.json` 必须保持可读。
- 升级旧版本时，默认目录结构不能改变已有数据的解释方式。
- 如果用户把数据目录放在网络盘、同步盘或移动硬盘上，应提示可能存在锁、延迟和断开风险。
- 不要为了“方便迁移”自动删除旧目录。旧目录至少保留到用户确认迁移成功，删除应是单独的显式操作。

## 9. 测试清单

### 路径解析

- 未配置时仍使用平台默认目录。
- 自定义 `data_root` 后，空的三个子目录配置跟随新根目录。
- 显式子目录覆盖 `data_root` 下的默认子目录。
- 相对路径、空路径、路径为文件、无权限路径都能得到明确错误。
- Windows 路径、UNC 路径、包含中文和空格的路径可正常工作。
- 路径包含符号链接或 `..` 时不会越出允许范围。

### 数据迁移

- 空的新目录可以成功迁移。
- 已有同名文件时不会静默覆盖。
- 复制中断后可重试，旧数据仍可用。
- 配置写入失败后，重启仍使用旧目录。
- 迁移完成后 TXT 项目、checkpoint、EPUB mapping 均能恢复。
- 迁移过程中关闭程序后，下一次启动可以识别未完成迁移。

### 运行行为

- 新建 TXT 任务写入新 `projects` 目录。
- 新建 EPUB 任务写入新 `mappings` 目录。
- 自动保存和手动保存都使用新目录。
- 创建、恢复、清理备份都使用新备份目录。
- 清除缓存不会影响翻译中间记录和备份。
- 运行中的翻译任务切换设置时不会产生半新半旧路径。

### UI

- 设置页显示的是最终解析路径。
- 选择目录后可以取消而不改变旧配置。
- 保存失败时保留用户输入并明确提示原因。
- 重启应用后设置仍显示正确路径。
- 小窗口、高 DPI 和长路径下文本不会遮挡按钮。

## 10. 建议的验收标准

功能完成后，至少应满足以下验收条件：

1. 用户只选择一次统一缓存根目录，就能让缓存、新建翻译项目、中间记录和备份写入该目录下。
2. 设置页不再显示缓存、中间记录和备份的独立路径选择项，所有子目录自动跟随统一缓存根目录。
3. 旧版本已有项目可以在不手工改 JSON 的情况下恢复。
4. 迁移失败时旧数据和旧配置仍然可用。
5. 程序重启后，最近项目、TXT 断点、EPUB 翻译进度和备份列表都能正常读取。
6. 代码中不再存在业务模块直接拼接 `data_dir / "projects"` 或 `workspace_dir / "mappings"` 的路径。
7. 设置页面的成功提示与实际磁盘写入位置一致。

## 11. 推荐开发顺序

建议按以下顺序提交，便于逐步验证：

1. 增加路径配置数据结构和默认值，不改变现有行为。
2. 增加路径解析、规范化、校验和目录创建测试。
3. 将 TXT 项目仓库和 EPUB mapping 接入统一路径对象。
4. 增加设置页显示、选择、恢复默认和保存逻辑。
5. 增加迁移清单、临时目录和失败恢复。
6. 为备份文件补充可配置的数量/大小清理策略，并在设置页提供恢复入口。
7. 根据缓存规模评估是否从 JSON 索引升级到 SQLite。
8. 完成完整回归测试后再考虑运行时即时切换。

## 12. 修改前需要补充的项目决策

以下内容建议在最终需求中明确：

- [ ] 缓存是否需要跨重启保留。
- [ ] “数据根目录”是否包含配置文件和日志。
- [ ] 子目录是否允许选择到数据根目录之外。
- [ ] 修改目录时是立即生效还是下次启动生效。
- [ ] 迁移遇到同名文件时允许覆盖、合并还是必须取消。
- [ ] 旧目录是否保留，以及由谁确认删除。
- [ ] 备份保留数量、总大小和保留天数。
- [ ] 是否支持网络盘、移动硬盘和同步盘。
- [ ] 是否需要提供“打开目录”“导出数据”“恢复数据”等辅助操作。
