# 依赖图发布回滚 API（FastAPI + SQLite）

本地可运行的服务节点依赖图发布系统：客户端提交服务节点及依赖关系，创建带版本号的发布批次；
系统按**拓扑层级**生成执行计划，接收每个节点的成功/失败回报；节点失败时**仅回滚本批次已成功的节点**
并返回**可重试的断点**；同时拒绝**循环依赖**和**跨版本 / 乱序回报**。

## 一条命令启动

```bash
docker compose up -d --build
```

启动后：

- API 地址：`http://localhost:8000`
- 健康检查：`GET http://localhost:8000/health`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- OpenAPI Schema：`http://localhost:8000/openapi.json`
- SQLite 数据文件：容器内 `/data/releases.db`，通过命名卷 `release-data` 持久化

镜像构建阶段完成 Python 依赖安装；容器启动时由应用自动完成 SQLite 初始化（创建数据目录、建表、开启 WAL），无需手动初始化。

停止 / 清理：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时删除数据卷（清空所有发布记录）
```

## 配置

通过环境变量配置（见 `docker-compose.yml`，可用 `-e` 或 `.env` 覆盖）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | 服务监听地址 |
| `APP_PORT` | `8000` | 服务监听端口（改端口时同步修改 compose 的端口映射） |
| `DB_PATH` | `/data/releases.db` | SQLite 数据库文件路径，其目录会自动创建 |

## 本地裸机运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./data/releases.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 核心语义

**执行计划（拓扑分层）**

- 依赖边 `from -> to` 表示 `to` 依赖 `from`，`from` 必须先执行。
- 系统用 Kahn 算法分层：level 0 无依赖可先执行，level N 的所有前置节点都位于更低层级。
- 同一 level 内节点互不依赖，可并行回报；跨 level 必须按顺序。
- 存在环时创建批次返回 `422 cycle_detected`，并给出环上节点。

**回报与并发控制**

- 回报必须携带与批次一致的 `version`，否则返回 `409 version_mismatch`（拒绝跨版本回报）。
- 节点的所有前置节点均为 `success` 后才允许回报，否则返回 `409 out_of_order`（拒绝乱序回报）。
- 已回报过的节点不可重复回报（`409 already_reported`）。
- 批次失败回滚后不可继续回报，需先调用 `/retry` 续发（`409 release_aborted`）。

**失败回滚与断点重试**

- 任一节点回报 `failed`：批次标记为 `partially_rolled_back`（此前无成功节点时为 `failed`）。
- 回滚范围**仅限本批次已成功的节点**（历史批次完全不受影响），按**逆拓扑顺序**回滚：
  层级高的先回滚，同层按成功时间倒序；失败节点本身保持 `failed`。
- 回报响应中返回可重试断点 `checkpoint`：含 `/retry` 地址、`restart_level` 与 `restart_nodes`
  （调用重试后最先可执行的节点，即最低待执行层级）。
- `POST /retry` 把所有 `failed` / `rolled_back` 节点重置为 `pending`，从最低待执行层级继续，
  已经成功过的节点（如发生在“部分成功后才失败”的重试场景）保留成功状态，不重复执行。

节点状态：`pending` → `success` / `failed`；成功节点失败回滚后变为 `rolled_back`，重试重置回 `pending`。
批次状态：`pending` / `running` / `succeeded` / `failed` / `partially_rolled_back`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/releases` | 创建发布批次（提交节点 + 依赖，自动拓扑分层，拒绝环） |
| `GET` | `/api/releases?limit=50` | 发布批次列表 |
| `GET` | `/api/releases/{id}` | 查询发布状态（节点状态、进度、next_nodes） |
| `GET` | `/api/releases/{id}/plan` | 查询拓扑分层执行计划 |
| `POST` | `/api/releases/{id}/report` | 节点成功 / 失败回报（跨版本、乱序返回 409） |
| `POST` | `/api/releases/{id}/retry` | 从断点续发 |
| `GET` | `/health` | 健康检查 |

错误响应统一格式：`{"error": {"code": "...", "message": "...", ...}}`。

## 端到端示例

```bash
# 1) 创建批次：a、b 是底层，c 依赖 a/b，d 依赖 c
curl -s -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "2026.09.20-001",
    "nodes": [
      {"name": "svc-a"}, {"name": "svc-b"},
      {"name": "svc-c"}, {"name": "svc-d"}
    ],
    "dependencies": [
      {"from_node": "svc-a", "to_node": "svc-c"},
      {"from_node": "svc-b", "to_node": "svc-c"},
      {"from_node": "svc-c", "to_node": "svc-d"}
    ]
  }'
# -> 计划分层: level0=[svc-a,svc-b]  level1=[svc-c]  level2=[svc-d]

# 2) 查看执行计划
curl -s http://localhost:8000/api/releases/<ID>/plan

# 3) 回报 level0 成功（同层可并行/乱序回报）
curl -s -X POST http://localhost:8000/api/releases/<ID>/report \
  -H 'Content-Type: application/json' \
  -d '{"node":"svc-a","status":"success","version":"2026.09.20-001"}'

# 4) 乱序回报会被拒绝（svc-b 尚未成功，svc-c 前置不齐）
#    -> 409 {"error":{"code":"out_of_order", ...}}

# 5) 跨版本回报会被拒绝
#    {"node":"svc-b","status":"success","version":"9999-old"}
#    -> 409 version_mismatch

# 6) svc-c 失败：svc-a/svc-b 被逆序回滚，响应含 checkpoint
curl -s -X POST http://localhost:8000/api/releases/<ID>/report \
  -H 'Content-Type: application/json' \
  -d '{"node":"svc-c","status":"failed","version":"2026.09.20-001",
       "error_code":"DEPLOY_TIMEOUT","error_message":"健康检查超时"}'
# -> status=partially_rolled_back
#    rolled_back_nodes=[{svc-b},{svc-a}]（同层按成功时间倒序）

# 7) 从断点重试：节点重置为 pending，next_nodes 重新变为 [svc-a, svc-b]
curl -s -X POST http://localhost:8000/api/releases/<ID>/retry

# 8) 查询状态
curl -s http://localhost:8000/api/releases/<ID>
```

循环依赖示例（返回 `422`）：

```bash
curl -i -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"bad","nodes":[{"name":"x"},{"name":"y"}],
       "dependencies":[{"from_node":"x","to_node":"y"},{"from_node":"y","to_node":"x"}]}'
```

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、启动初始化、统一错误处理
│   ├── config.py      # 环境变量配置
│   ├── database.py    # SQLite 连接、建表、事务
│   ├── models.py      # 请求模型
│   ├── errors.py      # 业务异常
│   └── service.py     # 拓扑分层 / 批次 / 回报 / 回滚 / 重试
├── requirements.txt
├── smoke_test.py      # 端到端冒烟测试（pip install httpx 后 python3 smoke_test.py）
├── Dockerfile         # 构建期安装依赖
├── docker-compose.yml # 一条命令启动 + 数据卷持久化
└── README.md
```

## 测试

```bash
pip install -r requirements.txt httpx   # httpx 仅 TestClient 需要
python3 smoke_test.py
```

覆盖：循环依赖拒绝、拓扑分层、跨版本回报拒绝、乱序回报拒绝、重复回报拒绝、
失败逆序回滚本批次已成功节点、断点重试后走完全程、部分回滚、历史批次隔离、重复初始化幂等。
