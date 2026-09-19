# 饮用水混配优化 API（Drinking Water Blending API）

基于 **Python + FastAPI + SciPy + SQLite** 的饮用水混配优化服务。用户提交目标体积、
钙 / 镁 / 碱度区间以及 2～6 种水源的浓度、可用量与单位成本，服务求解满足全部区间
与供给上限的**最低成本配比**；成本并列时取**按水源 ID 排序后用量向量字典序最小**
的方案；问题不可行时返回**各指标的可达范围**。支持以**幂等键**保存方案并按 ID 查询，
并可为已保存的最优方案计算**供给缩减预案**（调整量优先、成本次之、字典序收尾）。

## 快速开始（单条命令：安装 + 建库 + 启动）

```bash
docker compose up --build
```

该命令完成依赖安装、SQLite 建库（服务启动时自动执行 `CREATE TABLE IF NOT EXISTS`）
并启动服务。

- 服务地址：**http://localhost:8000**
- 交互式文档（Swagger UI）：**http://localhost:8000/docs**
- 健康检查：**http://localhost:8000/health**

不使用 Docker 时，本地等价的一条命令（Python ≥ 3.11）：

```bash
pip install -r requirements.txt && python -c "from app.db import init_db; init_db()" && uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 配置

均通过环境变量配置，默认值如下：

| 变量      | 默认值              | 说明                                   |
| --------- | ------------------- | -------------------------------------- |
| `DB_PATH` | `./data/blends.db`（容器内 `/data/blends.db`） | SQLite 数据库文件路径 |
| `HOST`    | `0.0.0.0`           | 监听地址（`python -m app.main` 时生效） |
| `PORT`    | `8000`              | 监听端口                               |

Compose 默认将宿主机 `8000` 映射到容器 `8000`，并通过命名卷 `blends-data` 持久化数据库。

## 单位约定

- 体积：m³（`target_volume`、`max_volume`、响应中的 `volume`）
- 浓度：mg/L（钙、镁；碱度按 mg/L CaCO₃ 计）
- 成本：任意货币单位 / m³

## API

### `POST /api/v1/blends/solve` — 只求解，不落库

```bash
curl -X POST http://localhost:8000/api/v1/blends/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "target_volume": 1000,
    "calcium":    {"min": 40, "max": 60},
    "magnesium":  {"min": 5,  "max": 20},
    "alkalinity": {"min": 100, "max": 200},
    "sources": [
      {"id": "W1", "max_volume": 600, "cost": 1.0, "calcium": 100, "magnesium": 30, "alkalinity": 250},
      {"id": "W2", "max_volume": 800, "cost": 2.0, "calcium": 20,  "magnesium": 5,  "alkalinity": 80}
    ]
  }'
```

最优响应（`200`）：

```json
{
  "status": "optimal",
  "plan_id": null,
  "total_volume": 1000.0,
  "total_cost": 1500.0,
  "blend": [
    {"source_id": "W1", "volume": 500.0, "cost": 500.0},
    {"source_id": "W2", "volume": 500.0, "cost": 1000.0}
  ],
  "indicators": {"calcium": 60.0, "magnesium": 17.5, "alkalinity": 165.0}
}
```

### `POST /api/v1/blends` — 求解并以幂等键保存

幂等键可放在请求体 `idempotency_key` 字段或 `Idempotency-Key` 请求头中（二者至少其一，
body 优先）：

```bash
curl -X POST http://localhost:8000/api/v1/blends \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-2026-0001' \
  -d '<同上请求体>'
```

- 首次创建返回 `201`，响应体含 `plan_id` 与 `created_at`；
- 以**相同键 + 相同请求**重发：返回 `200` 与已保存的方案，并带响应头
  `X-Idempotent-Replay: true`（不会重复求解或新建记录）；
- 以**相同键 + 不同请求**重发：返回 `409 Conflict`；
- 缺少幂等键：返回 `422`。

### `GET /api/v1/blends/{plan_id}` — 按 ID 查询已保存方案

```bash
curl http://localhost:8000/api/v1/blends/1
```

不存在时返回 `404`。

### `POST /api/v1/blends/{plan_id}/contingency` — 供给缩减预案

对**已保存且状态为 `optimal`** 的方案，在部分水源供给上限临时下调时重新计算配比。
请求体为「已有水源 ID → 新供给上限」的映射：至少包含一项，要求 `0 ≤ 新上限 ≤ 原上限`；
未列出的水源沿用原上限。**预案不会被保存，原方案也不做任何修改。**

目标体积、指标区间、水质浓度与单位成本全部保持不变，按以下层级目标重新优化：

1. **总调整量最小**：`Σ |新用量 − 原用量|` 最小；
2. **总成本最小**：在最小调整量的所有配比中最小化总成本；
3. **字典序收尾**：仍并列时，按水源 ID 升序排列的用量向量取字典序最小者。

```bash
# 先保存一个方案（plan_id 见响应，例如 1）
curl -X POST http://localhost:8000/api/v1/blends \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-2026-0001' \
  -d '<同上请求体>'

# 将 W1 的供给上限由 600 缩减到 400
curl -X POST http://localhost:8000/api/v1/blends/1/contingency \
  -H 'Content-Type: application/json' \
  -d '{"reductions": {"W1": 400}}'
```

最优响应（`200`）：

```json
{
  "status": "optimal",
  "plan_id": 1,
  "total_volume": 1000.0,
  "original_total_cost": 1500.0,
  "total_cost": 1600.0,
  "cost_delta": 100.0,
  "total_adjustment": 200.0,
  "reductions": {"W1": 400.0},
  "blend": [
    {"source_id": "W1", "volume": 400.0, "cost": 400.0,  "volume_delta": -100.0,
     "original_volume": 500.0, "original_max_volume": 600.0, "effective_max_volume": 400.0},
    {"source_id": "W2", "volume": 600.0, "cost": 1200.0, "volume_delta": 100.0,
     "original_volume": 500.0, "original_max_volume": 800.0, "effective_max_volume": 800.0}
  ],
  "indicators": {"calcium": 52.0, "magnesium": 15.0, "alkalinity": 148.0}
}
```

- `total_adjustment`：各水源用量差绝对值之和（总调整量）；
- `cost_delta`：新成本 − 旧成本（非负：调整量相同时取成本最小者）；
- 每条 `blend` 同时给出新旧用量（`volume` / `original_volume`）、用量差
  `volume_delta`、原上限 `original_max_volume` 与生效上限 `effective_max_volume`；
- 缩减后仍可行但无需调整时（新上限不低于当前用量），`total_adjustment` 与
  `cost_delta` 均为 `0`。

缩减后问题不可行时（例如总可用水量已不足目标体积）返回 `200` 及原因，
**不保存预案、不修改原方案**：

```json
{
  "status": "infeasible",
  "plan_id": 1,
  "message": "total available volume 900 is below the target 1000; calcium: ...",
  "reductions": {"W1": 100.0},
  "achievable_ranges": { "volume": {"requested": 1000.0, "max": 900.0}, "..." : {} }
}
```

错误码：

| 情况 | 状态码 |
| ---- | ------ |
| `plan_id` 不存在 | `404` |
| 原方案状态不是 `optimal`（含不可行方案） | `422` |
| 映射为空、水源 ID 不存在于原方案、新上限为负或超过原上限 | `422` |

### 不可行响应

问题不可行时（求解与保存接口均）返回 `200` 及结构化说明，例如钙下限 70 超过可达上限：

```json
{
  "status": "infeasible",
  "message": "calcium: requested minimum 70 exceeds the achievable maximum 68",
  "achievable_ranges": {
    "calcium":    {"min": 36.0, "max": 68.0, "requested_min": 70.0, "requested_max": 90.0},
    "magnesium":  {"min": 10.0, "max": 20.0, "requested_min": 5.0,  "requested_max": 20.0},
    "alkalinity": {"min": 114.0, "max": 182.0, "requested_min": 100.0, "requested_max": 200.0}
  }
}
```

可达范围指：在供给上限与目标体积约束下（若总供给不足目标体积，则改用全部可用水量），
各指标在混配水中可实现的浓度区间（对每个指标分别求解 min/max 两个 LP 得到的投影范围）。
总可用水量不足目标体积时，额外返回 `achievable_ranges.volume.max`。不可行方案同样会被保存，
可按 ID 查询。

## 规则与语义

- **优化模型**：线性规划（SciPy `linprog`，HiGHS 求解器）。变量为各水源取水量
  `x_i`；目标 `min Σ cost_i·x_i`；约束 `Σx_i = 目标体积`、
  `lo_j·V ≤ Σ conc_ij·x_i ≤ hi_j·V`（钙/镁/碱度）、`0 ≤ x_i ≤ max_volume_i`。
- **成本并列**：在所有最优解中，按水源 ID 升序排列的用量向量取**字典序最小**者
  （依次最小化 x₁, x₂, …；通过一系列 LP 精确实现）。水源 ID 为字符串，按字典序排序。
- **区间缺省**：`calcium` / `magnesium` / `alkalinity` 任一区间可省略，表示该指标不设限。
- **供给缩减预案**：`POST /api/v1/blends/{plan_id}/contingency` 读取已保存 `optimal`
  方案的原请求与配比，以新上限替换相应 `x_i ≤ max_volume_i` 后分层求解——
  先最小化 `Σ d_i`（偏差线性化：`x_i − x0_i ≤ d_i`、`x0_i − x_i ≤ d_i`、`d_i ≥ 0`），
  再以等式固定最小调整量并最小化成本，最后在并列解中对按 ID 升序的用量向量做字典序最小化；
  不可行时返回与求解接口一致的可达范围说明。仅计算、不落库、不改原方案。
- **校验**：水源数量 2～6、ID 唯一、`min ≤ max`、数值非负（目标体积为正）；不满足返回 `422`。
  预案接口额外校验：方案存在（否则 `404`）、原方案状态为 `optimal`、映射非空、
  每个新上限满足 `0 ≤ 新上限 ≤ 原上限` 且水源 ID 来自原方案（否则 `422`）。
- **幂等**：以幂等键唯一索引存储；请求体（不含键本身）做 SHA-256 指纹比对，防止键被
  不同请求复用。

## 测试

```bash
pip install -r requirements-dev.txt && pytest
```

共 32 个用例，覆盖求解器（最优性、字典序并列、不可行范围、供给缩减的调整量优先 /
成本次之 / 字典序收尾与不可行判定）与 API（幂等回放 / 冲突、按 ID 查询、参数校验、
预案接口的重算结果、不落库不改原方案、404 / 422 错误路径等）。

## 项目结构

```
app/
  main.py     # FastAPI 路由与生命周期（启动时自动建库）
  solver.py   # SciPy 线性规划：最低成本 + 字典序并列处理 + 不可行可达范围
  models.py   # Pydantic 请求/响应模型与校验
  db.py       # SQLite 持久化（幂等键唯一约束、原子写入）
  config.py   # 环境变量配置（DB_PATH / HOST / PORT）
tests/        # pytest 单元与端到端测试
Dockerfile    # python:3.12-slim 镜像，非 root 运行，含健康检查
docker-compose.yml
```
