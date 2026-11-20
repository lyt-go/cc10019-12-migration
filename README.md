# 用户标签画像与行为分析平台

面向用户行为埋点采集、标签体系构建、人群圈选与行为特征分析的数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 事件埋点 | `event_track` | 埋点事件 | 事件编号、事件名称、事件分类 |
| 标签定义 | `tag_define` | 标签规则 | 标签编号、标签名称、标签层级 |
| 标签计算 | `tag_compute` | 计算任务 | 任务编号、关联标签、计算范围 |
| 人群圈选 | `crowd_select` | 圈选条件 | 条件编号、条件名称、标签组合 |
| 行为路径 | `behavior_path` | 路径模型 | 模型编号、起始事件、目标事件 |
| 留存计算 | `retention_calc` | 留存配置 | 配置编号、初始行为、回访行为 |
| 用户分群 | `user_segment` | 分群定义 | 分群编号、分群名称、基准行为 |
| 生命周期价值 | `lifetime_value` | LTV模型 | 模型编号、用户分群、价值指标 |
| 漏斗分析 | `funnel_analyze` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 事件清洗 | `event_cleanse` | 清洗规则 | 规则编号、事件名称、校验字段 |
| 会话切割 | `session_split` | 切割策略 | 策略编号、策略名称、超时阈值 |
| 画像卡片 | `profile_card` | 卡片配置 | 卡片编号、卡片名称、展示标签 |
| 属性字典 | `attr_dict` | 属性定义 | 属性编号、属性名称、数据类型 |
| 交集分析 | `overlap_analysis` | 交集任务 | 任务编号、人群A、人群B |
| 标签下钻 | `tag_drill` | 下钻配置 | 配置编号、父标签、子标签集 |
| 事件聚合 | `event_aggregate` | 聚合配置 | 配置编号、事件名称、聚合维度 |
| 标签快照 | `tag_snapshot` | 快照记录 | 快照编号、快照日期、用户基数 |
| 行为热力 | `behavior_heatmap` | 热力配置 | 配置编号、行为事件、时间范围 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用

## 枚举字段版本升级（属性字典 → 标签计算）

属性字典里 `data_type=enum` 的字段分两个口径：

- **第 1 版**：枚举值是采集端上报的显示文字（如 `普通`），条件直接按文字比较。
- **第 2 版**：枚举值是稳定代码（如 `NORMAL`），显示文字只用于展示。

升级按字段定义上的兼容映射 `compat_map`（`display` + `source` → `code`）把存量值迁到第 2 版。
同一个显示名在不同采集来源可能对应不同代码，因此**迁移与切换都要求唯一可证的原值解释**：

| 情况 | 处理 |
| --- | --- |
| 来源命中唯一兼容映射 | 迁移为该代码 |
| 记录缺采集来源 | 不迁移（`blocked_missing_source`），但仍留痕 |
| 同一来源命中多个代码 | 阻断迁移（`ambiguous`） |
| 来源没有该显示名的映射 | 阻断迁移（`unmapped`），可显式排除后重跑 |

### 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/attr-dict/{attr_id}/migration` | 迁移结果明细：逐条样本的代码、状态与依据 |
| `POST /api/attr-dict/{attr_id}/migration/apply` | 按明细把字段定义升级到第 2 版（原子操作） |
| `GET /api/attr-dict/migrations` | 已落库的迁移明细 |
| `POST /api/tag-compute/{task_id}/run` | 执行计算，可带 `{"field_versions": {"ATTR-005": 2}}`，`{"*": 2}` 表示所有枚举字段 |
| `POST /api/tag-compute/{task_id}/field-versions` | 只登记目标字段版本 |
| `POST /api/tag-compute/{task_id}/replay` | 按任务当时记录的字段版本回查 |
| `GET /api/tag-compute/runs?task_id=...` | 运行快照：每轮实际启用的字段版本与状态 |

### 计算口径

- 任务首次执行时把实际启用的字段版本写入任务、结果产物和运行快照。
- 已有成功口径的任务再执行时按记录回查，不随字段定义漂移；提交 `field_versions` 才算切换。
- 切换是**整轮原子**的：目标标签及其子标签的条件里有任一字面量无法唯一解析，本轮即失败，
  不写结果产物、不覆盖已生效版本，任务上的 `requested_field_versions` 保留待人工处置。
- 第 2 版口径下，缺来源的属性值保持未知并计入 `unmapped_user_count`，不会被算成任何代码。

前端「属性字典」页可查看迁移明细并发起升级；「标签计算」页可选择任务执行回查或切到第 2 版，
并查看每轮运行快照。
