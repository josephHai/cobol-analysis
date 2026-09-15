# PDM 数据文件版本管理 — 设计文档 v0.1

> **范围**：在既有 Spring Boot + MongoDB 后端中新增的「文件版本管理」能力。首个场景为 PDM（数据迁移）数据管理；
> 长期目标是通用的多场景文件版本治理。
> **状态**：**设计已收敛，尚未实现。** 本文档是设计决策记录（design + rationale），不是现状说明。
> 标记为 **TBD** 的条目必须在**内网真实数据**上确定之后才能进入实现，§13 汇总了全部 TBD 及其验收依据。
> **与本仓库的关系**：本仓库（COBOL 遗留系统分析平台）的产物 `md / mmd / csv` 是本文档所述能力的
> **第二个场景**（§3.4，本阶段不实现），因此该设计文档落在本仓库的 `docs/` 下，与本仓库自身的技术栈无关。

---

## 0.1 已确认决策（v0.1 基线）

| # | 决策 | 选择 | 对设计的影响 |
|---|---|---|---|
| D1 | 部署形态 | **MongoDB 单节点，无事务** | 导入的原子性不能靠事务，改由「数据集级导入锁 + 状态机 + 单文档原子提交」保证（§4.2） |
| D2 | 版本粒度 | **一个版本 = 一组文件**（当前 1 个 xlsx + 13 个 JSON） | 版本、文件、实体（或文档）三级结构；差异必须逐层降级判断（§5.1） |
| D3 | 版本号 | **内部 `seq`（per-dataset 单调整数）+ `label`（业务版本号）**，不用 UUID | `seq` 承担排序/URL/diff 入参；`label` 从文件名解析、全局唯一、**禁止同 label 覆盖**（§2.3） |
| D4 | 文件身份 | **跨版本身份 = `(datasetId, logicalName)`**，不是文件名 | 因为 Confluence 附件名自带版本号、每版都不同，文件名必须在导入期归一化掉（§6.1） |
| D5 | 原件存储 | **GridFS 内容寻址 + 不可变 + 只软删** | 源文件是权威数据；所有解析结果均为派生、可从原件重建（§2.4） |
| D6 | 可插拔抽象 | **`FileTypeHandler` 注册表**，粒度 `ENTITY \| TEXT` | PDM = {MONGODB_JSON, XLSX} 现在做；CUS cobol = {MD, MMD, CSV} 留位（§3） |
| D7 | 比较引擎 | **两级漏斗 + Merkle 变更树 + 聚类 + MOVED + 数组 LCS** | 不使用 JSON Patch；差异靠分层聚合压制噪声，而不是靠字段黑名单（§5） |
| D8 | 报告形态 | **单份可折叠 HTML，双视角**（有效差异默认 / 全部差异） | 即时渲染 + 可选存档；JSON 契约同时冻结为未来任何 UI 的基础（§8.3） |
| D9 | 字段轨迹 | **单实体 × 单字段跨版本演变 (a)** | 第一屏面向 xlsx（业务模型定义），JSON 侧保留但非首发（§7） |
| D10 | 解析参数 | **允许存在，但不是「规则引擎」** | `keyFields` / 表头行 / 主键列 / `dialect` / 文件名模式属于解析参数；无规则集合、无规则版本、无重建接口（§3.5） |
| D11 | 忽略清单 | **降级为次要工具，初始为空** | 噪声主要靠聚类解决；忽略生效于 diff 期、零成本可改（§5.7） |
| D12 | 权限 | **不设计**，复用现有统一鉴权层 | 仅保留 `createdBy` 等审计字段（§10） |
| D13 | 顺序语义 | 对象键序不算差异；数组序由 LCS 接管 | 仅 `shape=COLLECTION` 的文档序与数组元素序保留文件级 `orderChanged` 标记（§5.6） |
| D14 | 历史补录 | **`seq` 按业务版本先后顺序**，不按导入时间 | 抓取脚本从文件名解析 `orderKey`，按序补录（§4.5） |

## 0.2 TL;DR

1. **噪声的正确解药是分层聚合，不是字段过滤。** 实测证明真正的问题不是「差异里有脏字段」，而是
   「同一个变更被放大成几百条」（§15 附录 A）。因此核心机制是 Merkle 变更树（浅层优先计数）+ 变更聚类，
   忽略清单退居次要（§5.2、§5.3、§5.7）。
2. **文件身份必须在解析之前确定。** Confluence 附件名带版本号，导致 `filePath` 永远无法跨版本匹配，
   会同时打死 diff 第一级漏斗（sha256 短路）与「文件新增/删除」这个珍贵信号。解法是在导入期从文件名
   归一化出 `logicalName`，而不是做改名检测（§6）。
3. **JSON 文件不是标准 JSON。** 实测发现注释、括号形式等方言特征，因此不能使用现成 JSON 库或
   JSON Patch，需要一个方言 lexer（§3.3、§5.8），规格 **TBD**（T1）。
4. **一个 JSON 文件 = 一个 collection 的单文档结构示例。** 因此 JSON 侧的差异是「单文档树 diff」，
   存储可以大幅简化：不需要实体表、不需要多键索引、不需要逐字段 hash（§2.2）。
5. **唯一可能再次改动模型的前提是 `shape` 分布**（是否所有 JSON 文件都是单文档）。建议 spike 第一步就验证它（T2）。

---

## 1. 背景与目标

### 1.1 现状

- PDM 数据当前维护在 **Confluence 页面的一张表格**里：一列版本号，一列对应的文件。
- 每个版本的文件组固定为两类：
  - **xlsx workbook**：1 个文件，约 1.03 MB，10 个 sheet，总有效行数约 7711 行。
    它描述 **PDM 的完整业务模型定义**。
  - **JSON**：13 个文件，总计约 49.2 KB（zip 约 51.5 KB）。每个 JSON 文件通常描述**一个 collection**，
    并提供该 collection 的**一个单文档结构示例**与实例值。它是 xlsx 所描述模型的一部分。
- 二者属于同一业务域，但**不是一一对应关系**（xlsx 是完整模型，JSON 是其中一部分 collection 的样例）。
- 源文件由业务方维护，**不可修改，是权威数据**。

> 后续版本的文件大小可能略有上浮，但不会出现数量级差异。

### 1.2 本阶段范围

| 在范围内 | 说明 |
|---|---|
| 文件导入 | 一次导入一个版本的整组文件；解析参数驱动的校验；幂等与失败清理 |
| 版本管理 | 列表、详情、原件下载、标签、软删、审计字段 |
| 版本差异查看 | **任意两版比较**（无基线概念）；汇总 + 分页明细；可折叠 HTML 报告 |
| 字段变迁 | 单实体 × 单字段的跨版本轨迹（§7） |
| 通用化底座 | `FileTypeHandler` 抽象、场景/数据集注册表、ENTITY/TEXT 两种粒度 |

**非目标（本阶段明确不做）：**

- PDM 数据真正迁移到目标库的执行逻辑。
- 审批流、通知、细粒度权限（鉴权复用现有统一层，D12）。
- 第二场景（CUS cobol analysis outputs：`md / mmd / csv`）的实现，仅预留能力声明（§3.4）。
- 文件级别的拆分/合并语义（§6.2 类型③），仅预留报告类别位。
- 相似度改名检测与 alias 边持久化，等真的出现时再补（§6.3）。

### 1.3 长期目标

同一套「版本 + 导入 + 比较 + 轨迹」引擎，通过可插拔 handler 支持任意场景与文件类型；PDM 只是第一个场景，
JSON/xlsx 只是头两个 handler 实现。

---

## 2. 领域模型

### 2.1 四层结构

```
场景 scenario（PDM / CUS_COBOL / …）
  └── 数据集 dataset（如 pdm@master，版本序列的作用域）
        └── 版本 version（= 一组文件；不可变快照）
              └── 文件 file（logicalName 稳定身份 + originalFileName 当版文件名）
                    └── 实体 entity（shape=SINGLE 时即文件本身；shape=COLLECTION 时为文档）
```

- **版本序列的作用域是数据集**：`seq` 在 dataset 内单调递增。
- **实体身份以 `(datasetId, logicalName)` 为命名空间**，不跨文件、不跨数据集统一。
- 因 PDM 的 JSON 与 xlsx 不是一一对应，**不做跨文件的实体统一**；文件级新增/删除（如某版新增一个 collection）
  是文件级的独立事件。

### 2.2 存储集合

只有 2 个集合 + 2 个 GridFS bucket：

| 组件 | 内容 | 性质 |
|---|---|---|
| `version` | 版本元数据 + manifest（文件清单、`logicalName`、`originalFileName`、sha256、大小、`handlerVersion`、`shape`、`stats`） | 权威、不可变 |
| `version_content` | 每 `(versionId, logicalName)` 一条：`merkleRoot`、`shape`、`stats`、`path → 源行号` 映射、解析覆盖率 | **派生、可从原件重建** |
| GridFS `raw` | 原始文件字节 | 权威、不可变 |
| GridFS `canon` | 规范化解析结果（BSON / 单元格矩阵） | **派生、可重建** |

`version` 的形态：

```json
{
  "_id": "<ObjectId>",
  "datasetId": "pdm@master",
  "scenario": "PDM",
  "seq": 7,
  "label": "PDM-2024.09",
  "tags": ["release"],
  "note": "",
  "status": "IMPORTING | READY | FAILED | ARCHIVED",
  "manifestHash": "sha256:...",
  "manifest": [
    {
      "logicalName": "PDM_Attr",
      "originalFileName": "PDM_Attr_v2.4.json",
      "fileType": "MONGODB_JSON",
      "shape": "SINGLE",
      "blobId": "<GridFS id>",
      "sha256": "sha256:...",
      "size": 3821,
      "handlerVersion": "1.0",
      "parseCoverage": 1.0,
      "stats": { "entities": 1, "nodes": 214 }
    }
  ],
  "createdAt": "...", "createdBy": "...", "source": "CONFLUENCE_SCRIPT",
  "deletedAt": null
}
```

索引：`unique(datasetId, seq)`（稀疏，见 §4.2）、`unique(datasetId, label)`、`(datasetId, status, seq desc)`。

> **为什么没有实体表、没有多键索引、没有逐字段 hash**：因为一个 JSON 文件就是一个单文档示例，
> 单版本 13 个 JSON 合计仅约 49 KB。把规范化后的文档整体存进 GridFS `canon`，diff 与轨迹在读取时现算
> 即可（§11 给出规模阈值）。这一简化是「文件即实体」直接带来的收益，早期版本中为此设计的实体表、
> `fields[]` 多键索引、`entityHash` 全部删除。

### 2.3 身份与命名

| 名称 | 含义 | 稳定性 |
|---|---|---|
| `logicalName` | 从文件名归一化出的稳定文件身份（剥掉版本号片段） | **跨版本稳定**，一切跨版本键都用它 |
| `originalFileName` | 当版实际文件名（Confluence 附件名） | 每版不同，仅展示与审计 |
| `seq` | 数据集内单调递增整数，展示为 `v7` | 永久不变 |
| `label` | 业务版本号（从文件名解析） | 数据集内唯一、不可覆盖 |
| `orderKey` | 业务版本先后顺序（同样来自文件名的版本片段） | 决定补录顺序（D14） |

**为什么不用 UUID**：`ObjectId` 已是 12 字节、全局唯一、自带时间序的内部主键；`seq` 需要「短且可比」以承担
排序、URL、diff 入参、轨迹表头。UUID 在这三个场景全是负分。跨环境同步那一天再加 `externalId` 映射列即可。

**`seq` 必须无洞且单调**，因此分配放在所有数据写入成功之后，并由数据集级串行锁保证不撞号（§4.2）。

### 2.4 不可变性与软删

- `READY` 版本的**文件与解析内容只读**；可改的只有 `label` / `tags` / `note` 等元数据。
- **禁止同一 `label` 重导覆盖**：命中即返回 409 + 已存在的版本 id，保持审计链干净。
- 删除为**软删**（`deletedAt`），GridFS blob **永不删除**（权威数据）。
- 所有变更写审计字段（导入、打标签、规则/参数变更），`createdBy` 来自现有统一鉴权层。

---

## 3. 文件类型抽象

### 3.1 `FileTypeHandler` 契约

```
FileTypeHandler                                    // Spring 注册表，按 typeId 注入
  ├─ typeId()                                      // "MONGODB_JSON" | "XLSX" | "CSV" | "MARKDOWN" | "MERMAID"
  ├─ capabilities()                                // { granularity, shape, dialect }
  ├─ supports(FileDescriptor)                      // 扩展名 + magic bytes
  ├─ parse(InputStream, ParseContext)              // → 规范化单元（ENTITY 流 / TEXT 文档 / 矩阵）
  ├─ render(value) → String                        // 值如何给人看
  └─ rulesSpec()                                   // 【已删除】不存在规则引擎
```

核心引擎（版本、导入、漏斗比较、聚类、轨迹、报告、导出）**不认识任何具体场景与文件类型**；
handler 只贡献「怎么读」与「能配什么解析参数」。

### 3.2 两种粒度

| 粒度 | 含义 | 差异方式 | 支持字段轨迹 |
|---|---|---|---|
| **ENTITY** | 文件可拆成有身份的单元（JSON 单文档 / 集合文档 / xlsx 行 / csv 行） | 两级漏斗 + Merkle 树 + 聚类 + MOVED + 数组 LCS | 是 |
| **TEXT** | 文件没有实体模型（md / mmd / 源码） | 文件级 sha256 短路 + 行级 diff（Myers）+ 可选分节 | 否 |

TEXT 粒度**不写 `version_content` 的实体部分**，只保留原件与可选的行级结果。
`capabilities()` 必须在 Phase 1 就存在，否则第二场景接入时是破坏性重构。

### 3.3 PDM 场景的 handler

| 文件类型 | 粒度 | `shape` | 身份 | 说明 |
|---|---|---|---|---|
| `MONGODB_JSON` | ENTITY | `SINGLE`（默认）/ `COLLECTION` | `logicalName`；`COLLECTION` 时再按 `_id` 或 `keyFields` | 见 §5.1、§15 附录 A |
| `XLSX` | ENTITY | `MATRIX` | `logicalName` + sheet + 主键列 | 见 §9 |

### 3.4 第二场景（本阶段不实现，仅留位）

场景 `CUS_COBOL`：`cus cobol analysis outputs`，文件类型为 `md / mmd / csv`。

| 文件类型 | 粒度 | 身份 | 计划实现 |
|---|---|---|---|
| `CSV` | ENTITY | 主键列（与 xlsx 同族，复用表格型 handler 基类，只换读取器） | Phase 3 |
| `MARKDOWN` | TEXT | — | Phase 3：行级 diff + 标题分节，无字段轨迹 |
| `MERMAID` | TEXT | — | Phase 3：先做文本 diff，后续可选升级为节点/边图 diff |

场景注册表：`PDM → {MONGODB_JSON, XLSX}`、`CUS_COBOL → {CSV, MARKDOWN, MERMAID}`。

### 3.5 解析参数 ≠ 规则引擎

以下参数**允许存在且必需**，它们决定「怎么读文件」，不影响「什么算差异」：

| 参数 | 作用域 | 说明 |
|---|---|---|
| `fileNamePattern` | 数据集级 | 从文件名解析 `logicalName` / `label` / `orderKey` 的正则（带捕获组） |
| `dialect` | 文件级 | `AUTO \| STRICT \| SHELL \| JSONC`，见 §5.8 与 T1 |
| `shape` | 文件级 | `SINGLE \| COLLECTION \| AUTO` |
| `keyFields` | 文件级 | `shape=COLLECTION` 时的文档主键字段 |
| `headerRow` / `keyColumns` | sheet 级 | xlsx 表头行与主键列（T5） |
| `ignoreFields` | 文件级 | 见 §5.7，**初始为空** |

**明确不存在**：规则集合（`diff_ruleset`）、规则版本号、`renormalize` / `reindex` 接口、JSONPath 表达式引擎、
按规则聚合的 hash。这些机制的共同问题是「把规则烘进存储」，会导致改规则就要重建数据。本设计中比较规则
在 **diff 期**生效，改参数零成本。

---

## 4. 导入流程

### 4.1 时序

```
POST /api/datasets/{datasetId}/versions:import      (multipart: zip 或逐个文件 + label/note)
  1. 取数据集锁（dataset_lock，租约制）
  2. 体积 / 扩展名 / magic bytes 校验 → 选择 handler
  3. 从 originalFileName 解析 logicalName / label / orderKey；校验唯一性与一致性（§4.3）
  4. 计算每文件 sha256 + manifestHash；命中已有版本 → 409（除非 force）
  5. Dry-run 校验：全部文件按 handler 试解析；失败则整体拒绝，不落任何数据
  6. 原始字节写 GridFS raw（候选对象）
  7. 解析 → 规范化 → 写 GridFS canon + version_content
  8. 【单文档原子操作】findAndModify：分配 seq + 状态 IMPORTING → READY
  9. 与 seq-1 自动比较（G），汇总写入版本列表视图
  10. 释放锁；失败路径见 §4.2
```

### 4.2 单节点无事务下的安全性

| 机制 | 做法 |
|---|---|
| 并发控制 | `dataset_lock` 文档 + 租约（owner / expiresAt），同一数据集串行导入。本场景 1 MB 量级、秒级完成，锁竞争无实际影响 |
| 原子提交 | `seq` 分配与状态翻转放在**同一次单文档 `findAndModify`** 中（Mongo 单文档操作原子） |
| 撞号兜底 | `unique(datasetId, seq)` 稀疏索引 + 冲突重试 |
| 失败清理 | 任何阶段失败 → 版本停留 `FAILED`，按 `versionId` 清理已写入的 `version_content`；因原件在 GridFS 且内容寻址，**重导天然可重入** |
| 孤儿回收 | 低频任务按「是否被任何 READY 版本引用」回收未被引用的 GridFS 对象 |

> 确认部署形态的一条命令（如需复核）：`db.adminCommand({getCmdLineOpts:1})` 查看是否有 `replication` 配置。

### 4.3 校验与护栏

| 护栏 | 作用 |
|---|---|
| 同一版本内 `logicalName` 必须唯一 | 文件名正则写错导致多文件映到同名 → **拒绝导入** |
| 跨版本 `logicalName` 集合应稳定 | 突然新增/消失 → 告警。**既抓真实的 collection 改名，也抓正则写错** |
| 文件名解析出的 version 与 `label` 一致 | 防止版本张冠李戴 |
| 首次导入输出「原始文件名 → logicalName → label」映射表 | 13 行 × N 版，人眼一眼验收正则（**建议不要省**） |
| 解析覆盖率必须 100% | 方言 lexer 有未识别 token 时拒绝导入，并报出首个非法 token 的行列号 |
| `keyFields`（COLLECTION 时）不得重复或缺失 | 缺失则整档 hash 比对并告警：该实体永远只会「增/删」，不会「改」 |

### 4.4 幂等与重复导入

- `manifestHash` = 对排序后的 `(logicalName, sha256)` 列表取哈希，用于识别「同一组文件已导入过」。
- 命中即返回 409 + 已有版本 id；`force=true` 时作为新版本导入并用新 `label`。
- **重复导入同一 `label` 一律拒绝**（D3）。

### 4.5 历史补录

Confluence 上已有多个版本时，抓取脚本一次性补录：

- 按文件名里的版本片段（`orderKey`）**升序依次导入**，使 `seq` 反映业务版本先后而非导入时间（D14）。
- 补录本质是 N 次普通导入 + N 次相邻版自动比较，无需特殊接口。
- 若某版文件组不完整（缺文件），导入仍可成功，但 manifest 会在报告中标记为不完整组。

---

## 5. 差异引擎

### 5.1 两级漏斗

```
第 1 级  文件级：按 logicalName 比对 sha256
         ├─ 相同 → 判定「无变化」，连解析都不做（PDM 场景多数文件命中此级）
         └─ 不同 → 进入第 2 级
第 2 级  文档树 diff（ENTITY 粒度）
         ├─ shape=SINGLE     → 文件即实体，直接做单文档树 diff
         └─ shape=COLLECTION → 先按 _id / keyFields 对齐文档，再逐文档做树 diff
```

> 早期设计中的「实体级」是第 3 级（先比 `entityHash` 再下钻字段）。在确认「一个 JSON 文件即一个单文档示例」
> 之后，SINGLE 形态下第 2、3 级合并，漏斗从三级降为两级。

`TEXT` 粒度只走第 1 级 + 行级 diff，不进入树 diff。

### 5.2 Merkle 变更树与浅层优先计数

- 每个节点自底向上计算 canonical hash；hash 相同 → **整棵子树跳过**；不同 → 递归。
- **计数规则**：一个变更只在「最浅的、其整个子树同质变化的节点」上计一次，`descendantCount` 上卷，
  默认视图不展开后代。
- 效果：`整棵子树新增` 是 **1 条 + N 个后代**，而不是 N+1 条同级差异；父子**永不重复计数**
  （直接修掉实测发现 #5）。
- 副产品：`子树新增` 的情形是 O(1) 判定，不需要枚举后代。

节点形态：

```json
{
  "kind": "MODIFIED",
  "descendantCount": 13,
  "children": [
    { "op": "ADD", "path": "_class", "nodeType": "SCALAR", "after": "com.x.PdmAttr", "sourceLine": 3 },
    { "op": "UPD", "path": "spec.color", "nodeType": "SCALAR",
      "typeBefore": "STRING", "typeAfter": "STRING", "before": "RED", "after": "GREEN", "sourceLine": 42 },
    { "op": "MOVE", "path": "spec.attrs", "from": "$.detail", "to": "$.meta.detail", "nodeCount": 12 }
  ]
}
```

`op ∈ {ADD, DEL, UPD, MOVE}`；`nodeType ∈ {SCALAR, OBJECT, ARRAY}`。

### 5.3 变更聚类

变更签名：

```
sig = (op, 规范化路径, 值形状)
      值形状 = (类型, 常量值)          若该路径在所有实体上取值相同  → level: SCHEMA
             = (类型, 'VARYING') + 直方图                           → level: DATA
```

- 同一签名的变更在**跨文件、跨实体**范围内合并为一条，带 `affected: { entities, files }`。
- `SCHEMA` 级（到处都以相同值新增/删除同一字段）与 `DATA` 级（取值各异）**分区展示**，
  这是 PDM 场景最有价值的区分：xlsx 定义模型，模型级变化才是业务关心的信号。
- 实测的 `_class` 批量加入由此从 118 条变成 **1 条**（§15 附录 A #1）。
- JSON 侧因「文件即实体」，聚类的主战场在**跨文件**与**文档内嵌套重复结构**；xlsx 侧则是**整列变化**。

### 5.4 MOVED 检测

- 依据 Merkle hash：某子树的 hash 在一处消失、在另一处以相同 hash 出现 → 报 `MOVE` 而不是「删除 + 新增」。
- 范围：同一文档内、同一文件的不同文档间。
- 护栏：仅对 **≥2 个节点**的子树做 move 检测（单标量的 relocate 不报，避免噪声）。
- 这是「更好的 diff」，不是「过滤后的 diff」——移动事实本身如实呈现（修掉实测发现 #3）。

### 5.5 数组 LCS 对齐

- 对数组元素的 canonical hash 跑 **LCS**，识别 insert / delete / move；未配对的元素再决定递归比较或按
  `arrayKeyFields`（可选，Phase 1 不做）配对。
- **这条比看起来重要**：数组头部插入一个元素会让其后所有索引路径「全部变化」，是一颗伪差异核弹，
  与「子树移动表现为大量删除新增」同源。
- 护栏：元素数 > 200 时降级为 hash 集合模式并给出告警（避免 LCS 最坏情况退化）。

### 5.6 顺序语义（D13）

| 顺序 | 是否算差异 | 处理 |
|---|---|---|
| 对象键顺序 | **否** | 解析为 BSON 值后天然无序 |
| 数组元素顺序 | 是 | 由 §5.5 LCS 对齐后如实报告 |
| 文档顺序（仅 `shape=COLLECTION`） | 否 | 按主键对齐后天然无关；仅在 `_id` 序列不同时打文件级 `orderChanged: true` |

### 5.7 双视角与忽略清单

- **有效差异（默认）** = 聚类视图：聚类 + 变更树前两层，其余折叠。**折叠 ≠ 过滤**，全部内容仍可取。
- **全部差异（raw）** = 诚实视角：包含格式、注释、被忽略字段带来的一切。
- 报告头部必须显式给出「另有 N 处变更仅涉及被忽略字段：`updatedAt`(118)」——**没有任何东西被隐藏**。
- `ignoreFields` 生效于 **diff 期**（基于已存的规范化内容逐字段比较），因此**改忽略清单零成本**，
  不需要重建任何数据；这也正是它不必成为「规则引擎」的原因。
- 忽略清单只支持**字段路径字面量 + 前缀通配**（`spec.*`），不引入表达式语言。
- 初始值**留空**：先用「全部差异」视角度量噪声，再决定填什么（T6）。

### 5.8 为什么不用 JSON Patch

1. **输入不合法**：实测文件含注释、`ObjectId(...)` 之类的括号形式，不是标准 JSON，RFC 6902 无法直接消费。
2. **模型错配**：JSON Patch 是**位置型**（`/items/0`），表达不了 move，一次数组头部插入就退化成全量重写——
   正是实测发现 #3 与 #4 的问题。
3. **语义丢失**：它没有「schema 级 vs data 级」的概念，无法承载聚类结果。

因此内部使用自研**变更树**作为规范形态；需要与他人互操作时，再导出一份 JSON Patch 兼容数组即可。

---

## 6. 文件改名与身份

### 6.1 问题：文件名每版都不同

Confluence 中的文件名**带着版本号**，因此不同版本的文件名必然不同。若直接用文件名作为跨版本键：

- `filePath` 永远无法匹配 → **diff 第一级漏斗（sha256 短路）彻底失效**，每个文件都掉到全量解析；
- 相邻两版的报告开头永远是「13 个文件删除、13 个文件新增」——100% 假象；
- 「文件新增/删除」这个本该珍贵的信号（新增一个 collection、废弃一个）被噪声覆盖。

**结论：不做改名检测，而是在身份层面把它归一化掉。**

```
manifest[] = {
  logicalName:      "PDM_Attr",             // 稳定身份，从文件名解析
  originalFileName: "PDM_Attr_v2.4.json",   // 当版实际叫什么，仅展示与追溯
  label:            "v2.4",                 // 从文件名解析
  orderKey:         "2.4",                  // 补录排序用
  sha256, size, fileType, shape, ...
}
```

**关键约束**：身份必须在**解析之前**就拿到，否则 sha256 短路失效。因此主身份只能来自文件名，
**不能**来自文档内容里的 collection 名；后者降级为**校验用的第二信号**（两者不一致 → 告警）。

**顺带收益**：`label` 与 `orderKey` 都从文件名解析，消除手填错误；「label 唯一」自然成立；
补录顺序不需要额外输入（D14）。

### 6.2 三类「改名」

| 类型 | 描述 | 处理 | 成本 |
|---|---|---|---|
| ① 仅附件名变、collection 名不变 | Confluence 文件带版本号，**每版必然发生** | 由 §6.1 的归一化**按构造消除** | 无 |
| ② collection 名本身变了 | 业务重命名了集合，预期较少 | 分层检测 + 报告显式呈现 + 可选 alias 边（§6.3） | 低 |
| ③ 拆分成多个 / 合并 | 一个文件变成两个或反之 | **本阶段不做**，报告里保留「文件结构变化」类别位 | 延后 |

### 6.3 分层检测（照 Git 的分层，只补一处）

Git 的做法值得学：**存储层不承认身份**（tree entry 就是 path A 消失、path B 出现），
**展示层推断身份**（现代 Git 默认开启改名检测，相似度阈值默认 50%）。我们采用同样的分层：

| 层 | 依据 | 置信度 | 时机 |
|---|---|---|---|
| 1 | 内容 sha256 完全相同 | 确定 | Phase 1（sha256 本来就要算） |
| 2 | 名称 / 从文档内容解析出的 collection 名 提示匹配 | 高 | Phase 1（几乎免费） |
| 3 | 内容相似度启发式 | **猜测**，必须显式标注置信度，与「删除+新增」并列展示而非以事实口吻出现 | 真出现 ② 时再加 |
| 4 | 都不匹配 → 删除 + 新增 | 事实 | Phase 1 |

**唯一比 Git 多做的一点**：把第 1、2 层确定的（或人工确认过的）改名落成一条 **append-only 的 alias 边**：

```json
{ "datasetId": "pdm@master", "fromLogicalId": "PDM_Schema", "toLogicalId": "PDM_ModelSchema",
  "fromSeq": 5, "toSeq": 7, "evidence": "sha256 | name | similarity", "confirmedBy": "..." }
```

理由：`git log --follow` 是我们字段轨迹的精确类比，而它出名的不可靠、昂贵，正是因为**每跨一版都要重新猜**，
且**表达不了改名链**（两两启发式在 A→B→C 上会产出互相矛盾的链条）。字段轨迹不能建立在一个「每次重猜」的
机制上。

alias 的性质保证其安全：

- **append-only**：只新增边，不触碰任何已密封版本的数据；
- **可撤回**：判错即作废该边，底层版本毫发无损、轨迹回到原样；
- **只读参与**：轨迹遍历时沿 alias 走，仅此而已；
- **不进导入路径**：导入期不读它，因此不存在「已索引数据被改名」这种需要迁移的状态。

这也正是**不能**把 `logicalId` 设为必填的原因：一旦必填，它就变成导入期状态，而导入期状态才是有可能被
改名影响、需要迁移与重算的东西。

---

## 7. 字段轨迹 (a)

**需求**：某个具体实体的某个字段，随版本演变的轨迹。**第一屏为 xlsx**（PDM 完整业务模型定义所在），
JSON 侧保留但非首发。

### 7.1 接口

```
GET /api/datasets/{datasetId}/lineage
      ?logicalName=PDM_Model&sheet=Products&rowKey=P-1001&column=Color      # xlsx
      ?logicalName=PDM_Attr&field=spec.color                                # JSON
```

### 7.2 响应契约

```json
{
  "logicalName": "PDM_Model", "sheet": "Products", "rowKey": "P-1001", "column": "Color",
  "points": [
    { "seq": 3, "label": "PDM-2024.06", "importedAt": "...", "sourceFileName": "PDM_Model_v1.9.xlsx",
      "state": "FIRST_SEEN", "value": "RED", "valueType": "STRING" },
    { "seq": 5, "label": "PDM-2024.07", "state": "CHANGED", "value": "GREEN", "previous": "RED" },
    { "seq": 7, "label": "PDM-2024.09", "state": "UNCHANGED", "value": "GREEN" }
  ],
  "gaps": [ { "seq": 4, "reason": "ENTITY_ABSENT" } ]
}
```

`state ∈ {FIRST_SEEN, CHANGED, UNCHANGED, FIELD_ABSENT, ENTITY_ABSENT}`；`gaps` 显式列出该实体或字段
不存在的版本，**不允许静默跳过**（这是 §6.3 强调身份稳定性的直接原因）。

### 7.3 实现

从 GridFS `canon` 按版本顺序取规范化内容，抽取目标路径后逐个比较。本场景规模下无需索引（§11）；
`shape=COLLECTION` 时先按主键定位文档再取字段。

---

## 8. API 与输出契约

### 8.1 接口清单

| 接口 | 说明 |
|---|---|
| `POST /api/datasets/{id}/versions:import` | 导入一个版本（multipart zip 或逐个文件 + label/note） |
| `GET /api/datasets/{id}/versions` | 分页列表：`seq`、`label`、`tags`、时间、文件数、统计、**相对上版 diff 汇总** |
| `GET /api/datasets/{id}/versions/{seq}` | 元数据 + manifest（`logicalName`、`originalFileName`、sha256、大小、下载链接） |
| `GET /api/datasets/{id}/versions/{seq}/files/{logicalName}` | 流式下载原件（GridFS） |
| `PATCH /api/datasets/{id}/versions/{seq}` | 仅改 `label` / `tags` / `note` |
| `DELETE /api/datasets/{id}/versions/{seq}` | 软删 |
| `GET /api/datasets/{id}/diff?from=&to=&view=effective\|all&cursor=` | 任意两版比较：聚类 + 汇总 + 分页明细 |
| `GET /api/datasets/{id}/diff/report?...&archive=false` | 渲染可折叠 HTML 报告；`archive=true` 生成存档（稳定 URL） |
| `GET /api/datasets/{id}/lineage?...` | 字段轨迹（§7.1） |

> `view` 是一个**二值枚举**，不是规则引擎；忽略清单来自解析参数。

### 8.2 变更树 / 聚类契约

```json
{
  "datasetId": "pdm@master",
  "from": { "seq": 6, "label": "PDM-2024.07", "versionId": "..." },
  "to":   { "seq": 7, "label": "PDM-2024.09", "versionId": "..." },
  "profile": { "ignoreFields": [], "profileHash": "sha256:..." },
  "summary": {
    "effective": { "clusters": 4, "filesChanged": 6, "entitiesChanged": 121, "fieldChanges": 137 },
    "raw": { "filesChanged": 13, "entitiesChanged": 121, "fieldChanges": 1204, "formattingOnlyFiles": 7 },
    "ignoredOnly": { "count": 0, "fields": {} }
  },
  "clusters": [
    { "op": "ADD", "path": "_class", "level": "SCHEMA", "after": "com.x.PdmAttr",
      "affected": { "entities": 118, "files": 13 }, "sourceLine": 3 },
    { "op": "MOVE", "path": "spec.attrs", "from": "$.detail", "to": "$.meta.detail",
      "nodeCount": 12, "affected": { "entities": 1, "files": 1 } }
  ],
  "fileChanges": [
    {
      "logicalName": "PDM_Attr", "originalFileName": "PDM_Attr_v2.4.json",
      "status": "MODIFIED",
      "sha256Changed": true, "parsedValuesEqual": false,
      "formattingOnly": false, "orderChanged": false,
      "entities": { "added": 0, "removed": 0, "modified": 1, "unchanged": 0 },
      "details": [ { "key": "PDM_Attr", "tree": { "kind": "MODIFIED", "descendantCount": 13, "children": [ "…§5.2…" ] } } ]
    }
  ]
}
```

`fileChanges[].status ∈ {UNCHANGED, MODIFIED, ADDED, REMOVED, RENAMED, RESTRUCTURED}`；
`RENAMED` 仅在 §6.3 第 1、2 层命中时出现；`RESTRUCTURED` 为类型③预留。

### 8.3 HTML 报告

**单份可折叠报告**，开发 / 测试 / 业务共用（不给两类读者两个入口）：

```
① 头部    from/to 版本、label、时间、文件数；生效的忽略清单 + profileHash（自描述、可审计）
② 总览    文件级：新增/删除/改名/重命名/修改/未变；有效差异 vs 全部差异两套计数
③ 聚类    SCHEMA 级在前、DATA 级在后，各按 affected 降序
④ 文件 → 实体（默认折叠）→ 字段级 before/after（k、类型、旧值、新值、源行号）
⑤ xlsx 专属：列级变化（新增/删除列 ≈ 模型字段变化）置顶
⑥ 尾部    仅涉及被忽略字段的变更清单（诚实层的明细入口）
```

- **即时渲染**为默认路径；业务需要发链接时用 `archive=true` 生成**存档**（记录当时的 `profileHash` 与忽略清单，
  并给出稳定 URL）。存档是「快照资产」，即时视图是「当前视图」。
- HTML 优先；xlsx 导出延后到 Phase 2。

---

## 9. xlsx 专项

| 项 | 规则 |
|---|---|
| 比较对象 | **只比 value**（公式单元格取**缓存值**） |
| 日期 | 按 cellStyle 的日期格式与 `date1904` 标志还原为真实日期再比，**不比较 serial number** |
| 数字 | 以配置精度规范化后比较，避免浮点误差 |
| 行身份 | **主键列拼接**；**绝不能用行号对齐**（中间插入一行会让其后全部误报） |
| 表头 | `headerRow` 与 `keyColumns` 为解析参数（T5） |
| 合并单元格 | 值取左上角；是否铺开为规则内的固定行为（不做成选项） |
| sheet 身份 | 按名称；sheet 重命名是否算「删除+新增」待实测后定 |
| 列级变化 | **整列变化 = 1 条模型级变更**（聚类），列增删置顶展示 |
| 公式无缓存值时 | 大量 `None` 时给出告警（程序设计生成、未经 Excel 保存的工作簿可能没有缓存值） |
| 解析器 | Apache POI `XSSF`；handler 内部保留替换为 SAX（`XSSFReader`）的口子以应对未来增长 |

---

## 10. 安全与权限

- **不设计权限模型**（D12）：复用现有统一鉴权层，本能力只提供接口与资源。
- 保留审计字段：`createdBy`、`createdAt`、`source`；元数据变更同样记录。
- 导入接口的输入校验：体积上限、扩展名 + magic bytes、zip 路径穿越防护。
- 原件下载走流式，避免全量载入内存。

---

## 11. 规模与性能

### 11.1 已知规模（真实数据）

| 项 | 数值 |
|---|---|
| xlsx | 1 个文件 / 约 1.03 MB / 10 sheet / 约 7711 有效行 |
| JSON | 13 个文件 / 约 49.2 KB（zip 约 51.5 KB） |
| 单版本合计 | 约 1.08 MB 原始字节 |

### 11.2 结论

| 议题 | 结论 |
|---|---|
| 是否需要实体表 | **不需要**：单版本 JSON 仅 49 KB，规范化内容整体存 GridFS `canon` 即可 |
| 是否需要单元格索引 | **不需要**（版本数 ≤ ~30）：xlsx 规范化内容约 1–1.5 MB/版，30 版约 40 MB 顺序读取，轨迹查询足够快 |
| 何时才需要 | 版本数超过约 30、或单版本数量级上浮时，再加**派生**的单元格索引 `{datasetId, sheet, rowKey, colKey, versionSeq}`，可从 `canon` 重建，非权威数据 |
| diff 成本 | 第 1 级 sha256 短路承担主要开销；只有真正变化的文件进入树 diff |
| 数组 LCS | 元素数 > 200 降级（§5.5） |

> **扩展边界**：本方案是「导入时物化规范化内容」，在 MB 级以下最优。若未来某场景单版本上到 100 MB+，
> 让该 handler 切到 lazy 模式（不物化，diff 时流式比较）——漏斗结构已经预留这层（第 1 级短路在任何模式下都成立）。

---

## 12. 分期计划

### Phase 1（可用）

1. `version` / `version_content` 集合 + GridFS `raw` / `canon`（不可变、软删）
2. `FileTypeHandler` 注册表 + 能力声明（`granularity` / `shape` / `dialect`）；第二场景留位
3. JSON 方言 lexer（T1）+ 单文档规范化 + Merkle 根 + 解析覆盖率门槛
4. xlsx 解析（只比 value、公式取缓存值、按主键列对齐）
5. 导入：数据集锁 + 状态机 + 幂等 + 护栏（§4.3）
6. 两级漏斗 + Merkle 变更树（浅层优先计数）+ MOVED + 数组 LCS + 跨文件聚类
7. 双视角可折叠 HTML 报告 + 存档
8. 任意两版比较；导入时自动与 `seq-1` 比较并挂汇总
9. xlsx 侧字段轨迹 (a)
10. 文件名归一化（T4）+ 护栏

### Phase 2（好用）

- 标签与审计完善、xlsx 导出、列级变更汇总增强、孤儿 blob 清理、忽略清单按实测填充（T6）

### Phase 3（通用）

- 第二场景 `CUS_COBOL` 接入（`md / mmd / csv`，TEXT 粒度）以验证抽象
- 按需引入相似度改名检测与 alias 边（§6.3）、派生单元格索引（§11）、`externalId` 跨环境映射

---

## 13. TBD（待内网真实数据确定）

| # | 待定项 | 用什么数据解决 | 验收标准 |
|---|---|---|---|
| **T1** | JSON 方言 lexer 规格（注释 / 括号形式 / 尾逗号 / BOM / 是否一行一文档） | 13 个 JSON 文件 | 解析覆盖率 100%；能定位首个非法 token 的行列号 |
| **T2** | `shape` 实际分布：是否**全部**都是单文档？有无 `COLLECTION` 形态？ | 同上 | 每个文件的 `shape` 判定明确 |
| **T3** | `_class` 是一次性还是持续性的环境变迁 | ≥2 个相邻版本 | 决定它在报告里是「SCHEMA 告警」还是常规字段 |
| **T4** | 文件名模式（`logicalName` + `label` + `orderKey` 的提取正则） | Confluence 表 + 附件名 | 映射表人工验收通过；§4.3 四条护栏全绿 |
| **T5** | xlsx 表头行、主键列、「行」的语义（模型实体 / 属性定义 / 码值表） | xlsx 本体 | 轨迹的 `rowKey` 确定；「列增删 = 模型字段增删」是否成立有结论 |
| **T6** | `ignoreFields` 初始内容 | 实测噪声 | 先用「全部差异」视角度量，再决定是否填写 |
| **T7** | 相似度改名检测 + alias 边 | 真的出现类型②改名时 | 出现再做 |

**只有 T2 可能再次改动模型**（若存在 `shape=COLLECTION` 的文件，会重新引入「文档级对齐」这一层）。
建议 spike 第一步就验证 T2，其余 TBD 的结论都落在已锁定决策之内，不会引起返工。

---

## 14. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 时间戳 / `_id` 在高版本间 churn | 诚实视角下每个文档都显示为「已修改」，diff 失去可用性（**已实测确认噪声较高**） | 聚类 + 双视角；必要时按 T6 填 `ignoreFields`（diff 期生效、零成本） |
| 存在 `shape=COLLECTION` 的文件（T2） | 需重新引入文档级对齐，实体模型回补 | spike 先验；handler 契约已声明 `shape` 能力 |
| 文件名正则多剥 / 少剥 | 身份错乱，跨版本匹配失败 | 映射表人工验收 + 同版唯一性 + 跨版集合稳定性护栏 |
| 注释剥离必须由 lexer 完成 | 用正则会误伤字符串内的 `//` | 禁止正则预处理，必须真 lexer；解析覆盖率门槛 |
| 数组元素超 200 时 LCS 退化 | diff 耗时上升 | 降级为 hash 集合模式 + 告警 |
| 公式单元格无缓存值 | 大量 `None`，差异结论失真 | 导入期检测并告警（T5 一并确认） |
| 版本数增长 | 轨迹查询变慢 | 阈值约 30 版时加派生单元格索引（§11） |
| 第二场景文件更大（CUS cobol） | 「导入时物化」策略可能不适用 | handler 可切 lazy 模式；第 1 级短路与粒度无关 |

---

## 15. 附录

### 附录 A：实测发现 → 设计响应（证据记录）

对两个相邻版本的 JSON 文件做对比，发现噪声较高，包括以下 5 项。这些发现是 §5 各项机制的直接依据：

| # | 实测发现 | 根因 | 设计响应 |
|---|---|---|---|
| 1 | `_class` 等技术元数据批量加入 | 一个结构性变更 × N 个位置 = N 条「文档被修改」 | **变更聚类**（§5.3）：同签名合并为 1 条，`affected: N`；并区分 SCHEMA / DATA 级 |
| 2 | 格式、括号、注释变化 | 文件不是标准 JSON，且两版写法不同 | **方言 lexer + 值级比较**（§3.5、§5.1）：有效差异归零；raw 视角标记 `formattingOnly` |
| 3 | 子树移动被表示为大量删除和新增 | 无 move 检测 | **MOVED 检测**（§5.4）：按子树 hash 匹配 |
| 4 | 文件并非标准 JSON，不能直接使用 JSON Patch | 方言输入 + 位置型 patch 模型错配 | 自研**变更树**输出契约（§5.8、§8.2） |
| 5 | 字段路径比较把父节点和子节点分别计数 | 逐路径平铺计数，无层级语义 | **Merkle 变更树 + 浅层优先计数 + 后代上卷**（§5.2） |

> 一句总结：噪声的解药是**分层聚合**，不是字段过滤。`_class` 其实是**信号**——它是「导出工具或应用版本变了」
> 的结构性证据；问题从来不是它出现了，而是它以 N 条的形式出现。

### 附录 B：术语表

| 术语 | 含义 |
|---|---|
| `logicalName` | 从文件名归一化出的稳定文件身份，跨版本不变 |
| `originalFileName` | 当版实际文件名（Confluence 附件名），仅展示与审计 |
| `seq` | 数据集内单调整数版本号，展示为 `v7` |
| `label` | 业务版本号（如 `PDM-2024.09`），数据集内唯一 |
| `manifest` | 版本的文件清单（`logicalName`、sha256、大小、`shape`、`stats`） |
| `shape` | 文件形态：`SINGLE` / `COLLECTION` / `MATRIX` |
| `granularity` | handler 的差异粒度：`ENTITY` / `TEXT` |
| `dialect` | JSON 方言：`STRICT` / `SHELL` / `JSONC` / `AUTO` |
| `canon` | 规范化后的解析结果（派生、可重建） |
| Merkle 变更树 | 逐节点 hash 的变更树，用于浅层优先计数与子树移动检测 |
| 聚类 cluster | 同签名变更在跨文件、跨实体范围的合并结果 |
| MOVED | 子树在 hash 不变的前提下换了位置 |
| 有效差异 / 全部差异 | 双视角：应用忽略清单后的视图 / 完全诚实的视图 |

### 附录 C：明确不做的设计（防回潮清单）

以下机制在讨论中被逐一评估并**否决**，记录在此以免后续重新引入：

| 被否决的机制 | 否决理由 |
|---|---|
| 规则集合 + 规则版本 + `renormalize` / `reindex` 接口 | 把规则烘进存储，改规则要重建数据。改为「解析参数（读文件）+ diff 期比较」，改参数零成本 |
| 逐字段 hash / `entityHash` / 实体表 / `fields[]` 多键索引 | 「文件即单文档」后规模仅 49 KB，属过度设计；需要时可按 §11 阈值再加派生索引 |
| 内部版本号用 UUID | `ObjectId` + `seq` 已满足唯一性与可比性；UUID 让排序、URL、轨迹表头全面变差 |
| `logicalId` 必填 | 会把身份变成导入期状态，正是需要迁移与重算的那种状态；改为归一化 + 可选 alias 边 |
| 相似度改名检测前置实现 | 类型①由归一化按构造消除，②预期罕见；按需再补 |
| 文件拆分 / 合并语义 | 本阶段不做，仅保留报告类别位 `RESTRUCTURED` |
| JSON Patch (RFC 6902) 作为内部表示 | 方言输入不合法 + 位置型表达不了 move（附录 A #4） |
| 权限 / 角色模型 | 已有统一鉴权层，重复设计无收益；仅保留审计字段 |
| 字段轨迹的分布 / 聚合视图 | 需求明确为单实体 × 单字段的轨迹 (a) |

---

## 16. 参考

- 仓库内：[`docs/DESIGN.md`](DESIGN.md)（本仓库架构）、[`CONTRIBUTING.md`](../CONTRIBUTING.md)（提交与评审规范）
- 设计所依据的规范：Conventional Commits、RFC 6902（JSON Patch，**仅作对照，不采用**）
