# 科研样品分装追溯 API（FastAPI + SQLite）

面向实验室样品管理的**取样 / 分装 / 合并**追溯服务。所有写操作在数据库事务中执行，
自动维护样品谱系 DAG，校验父样品余量、禁止环路，并通过唯一**操作号**提供幂等保证。

## 一键启动

```bash
docker compose up --build
```

启动后访问：

- API 服务地址： **http://localhost:8000**
- 交互式文档（Swagger UI）： http://localhost:8000/docs
- 健康检查： http://localhost:8000/health

数据通过 Docker 命名卷 `sample-data` 持久化到容器内 `/data/samples.db`，容器重建不丢数据。

停止服务：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据卷
```

> 不使用 Docker 时：`pip install -r requirements.txt`，然后
> `uvicorn app.main:app --host 0.0.0.0 --port 8000`
> （可用环境变量 `SAMPLE_DB_PATH` 指定数据库文件位置）。

## 领域模型

| 概念 | 说明 |
|---|---|
| 样品 `sample` | 有唯一编号、初始体积、**当前剩余余量**和单位 |
| 取样 `sample` 操作 | 登记一个根样品（来源记录），`sample_code` 必须不存在 |
| 分装 `aliquot` 操作 | 从一个父样品取出部分余量，产生一个新子样品，父余量扣减 |
| 合并 `merge` 操作 | 消耗 2 个以上父样品的指定余量（默认全部投完），产生一个新子样品 |
| 操作号 `op_no` | 全局唯一，**同一操作号 + 相同载荷重复提交返回原结果（幂等）** |
| 谱系边 `lineage` | `parent → child`，记录消耗量与来源操作号，构成禁止环路的有向无环图（DAG） |

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/operations` | 提交单个取样/分装/合并操作 |
| POST | `/api/v1/operations/batch` | 批量提交，**整批一个事务，全成功或全回滚** |
| POST | `/api/v1/operations/validate` | 预检（dry-run）：校验余量与冲突，**不写库** |
| GET | `/api/v1/operations/{op_no}` | 按操作号查询原始记录与结果（幂等回放内容） |
| GET | `/api/v1/samples/{code}` | 查询样品当前余量等信息 |
| GET | `/api/v1/samples/{code}/remaining?requested=` | 余量查询 / 充足性校验 |
| GET | `/api/v1/samples/{code}/lineage?direction=` | 回溯完整来源 / 去向（`sources`、`destinations`、`both`） |
| GET | `/health` | 健康检查 |

## 快速示例

### 1. 取样（登记根样品）

```bash
curl -X POST http://localhost:8000/api/v1/operations \
  -H 'Content-Type: application/json' \
  -d '{"op_type":"sample","op_no":"OP-S-001","sample_code":"ROOT-A","volume":100,"unit":"mL"}'
```

### 2. 分装（从 ROOT-A 取 30mL 到 ALQ-1）

```bash
curl -X POST http://localhost:8000/api/v1/operations \
  -H 'Content-Type: application/json' \
  -d '{"op_type":"aliquot","op_no":"OP-A-001",
       "parent_code":"ROOT-A","child_code":"ALQ-1",
       "volume":30,"unit":"mL"}'
# result: ROOT-A 余量 70，ALQ-1 余量 30
```

### 3. 合并（ALQ-1 全部 30mL + ROOT-B 的 20mL → MIX-1）

```bash
curl -X POST http://localhost:8000/api/v1/operations \
  -H 'Content-Type: application/json' \
  -d '{"op_type":"merge","op_no":"OP-M-001",
       "parent_codes":["ALQ-1","ROOT-B"],
       "volumes":[30,20],
       "child_code":"MIX-1","unit":"mL"}'
# 不传 volumes 时默认各父样品“全部余量”投完
```

### 4. 幂等：重复提交相同操作号

用**完全相同的请求体**再次提交 `OP-S-001`：

- HTTP `200`，响应中 `"replayed": true`，`result` 为首次结果；余量不会重复增加。
- 若相同 `op_no` 但载荷不同 → HTTP `409`，错误原因为 `op_no_conflict`。

### 5. 批量操作

```bash
curl -X POST http://localhost:8000/api/v1/operations/batch \
  -H 'Content-Type: application/json' \
  -d '{"operations":[
    {"op_type":"sample","op_no":"B1","sample_code":"S1","volume":100,"unit":"mL"},
    {"op_type":"aliquot","op_no":"B2","parent_code":"S1","child_code":"A1","volume":30,"unit":"mL"},
    {"op_type":"merge","op_no":"B3","parent_codes":["A1","S1"],"volumes":[30,20],"child_code":"M1","unit":"mL"}
  ]}'
```

批量按数组顺序在一个事务中模拟执行（后一步能看到前一步的余量/新样品）：

- 全部合法 → 顺序提交，返回每步结果；
- 存在冲突 → **整批回滚**，响应体 `errors` 数组返回**每一个冲突**，
  并带 `index`（批次中的位置，从 0 开始）、`op_no`、`sample_code` 等定位字段。

### 6. 预检而不落库

`POST /api/v1/operations/validate` 与批量请求体相同，只做 dry-run：
返回每项是否合法、模拟结果与全部冲突，不写任何数据，可用于提交前表单校验。

### 7. 回溯来源 / 追出去向

```bash
# MIX-1 的完整来源（祖先根样品 + 每条来源路径）
curl "http://localhost:8000/api/v1/samples/MIX-1/lineage?direction=sources"

# ROOT-A 的全部去向
curl "http://localhost:8000/api/v1/samples/ROOT-A/lineage?direction=destinations"
```

响应包含：

- `nodes`：涉及的全部样品及当前余量；
- `edges`：谱系边（`parent` / `child` / `consumed_volume` / `op_no`，可直接画图）；
- `roots` / `leaves`：最原始来源样品 / 最末端样品；
- `paths`：从根到中心（来源）或从中心到末端（去向）的**完整路径列表**，
  例如 `[["ROOT-B","MIX-1"],["ROOT-A","ALQ-1","MIX-1"]]`。

### 8. 余量校验

```bash
curl "http://localhost:8000/api/v1/samples/ROOT-A/remaining?requested=60"
# {"code":"ROOT-A","remaining":70.0,"unit":"mL","sufficient":true,"requested":60.0}
```

## 冲突与错误定位

业务冲突统一返回 HTTP `409`，响应体：

```json
{
  "detail": "操作冲突",
  "errors": [
    {
      "reason": "insufficient_volume",
      "index": 0,
      "op_no": "OP-A-002",
      "sample_code": "ROOT-A",
      "message": "父样品 ROOT-A 余量 70.0 mL 不足，需分装 999.0",
      "remaining": 70.0,
      "requested": 999.0
    }
  ]
}
```

冲突原因 `reason`：

| reason | 含义 | 附带定位 |
|---|---|---|
| `op_no_conflict` | 操作号已存在但载荷不一致 | `op_no`、`index` |
| `duplicate_op_no` | 同一批次内操作号重复 | `index`、`op_no` |
| `sample_already_exists` | 取样/输出样品编号已存在 | `sample_code` |
| `duplicate_output` | 同一批次内多个操作创建同一样品 | `sample_code` |
| `parent_not_found` | 父样品不存在 | `sample_code` |
| `insufficient_volume` | 父样品余量不足 | `remaining`、`requested` |
| `unit_mismatch` | 单位与父样品不一致 | `expected_unit`、`provided_unit` |
| `cycle_detected` | 新边会形成环路 | `cycle_path`（闭合样品编号路径）、`parent_in_cycle` |

环路检测在「库中已有边 ∪ 本批新增边」构成的图上做后代闭包判断，
`cycle_path` 给出实际形成环的样品链，便于定位，例如
`["MIX-1","ROOT-A","ALQ-1","MIX-1"]` 表示新边 `MIX-1 → ROOT-A`
与已有路径 `ROOT-A → ALQ-1 → MIX-1` 构成闭环。

## 事务与一致性说明

- 每个（或每批）请求在单个 SQLite `BEGIN IMMEDIATE` 事务中执行，先**整批模拟校验**
  （余量、单位、样品存在性、环路、操作号），收集全部冲突后才写入；任一冲突整批回滚。
- 开启 `foreign_keys`、WAL 日志与 busy_timeout；谱系边带外键，历史操作不可变
  （只追加 operations / lineages，余量字段在 samples 上就地更新）。
- 浮点体积比较使用 1e-9 容差，避免浮点误差误判余量不足。
- 每个请求独立连接，请求结束自动关闭。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
pytest -q
```

测试覆盖：取样/分装/合并主流程、幂等回放与操作号冲突、余量不足定位、
单位不一致、自环与多级环路（含环路径）、来源/去向完整路径、
批量成功/多冲突收集/整批回滚/批内环路、dry-run 不落库、请求体校验，共 22 个用例。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、异常处理、依赖注入
│   ├── services.py    # 核心引擎：事务、余量扣减、环路检测、幂等、批量
│   ├── lineage.py     # 递归 CTE 谱系回溯、余量校验
│   ├── schemas.py     # Pydantic 请求/响应模型（op_type 判别联合）
│   ├── database.py    # SQLite 连接与表结构
│   └── config.py      # 配置（数据库路径、容差）
├── tests/             # pytest 端到端测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
