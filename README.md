# 离线实验室插件发布与安装方案服务

面向**无法联网的实验室内网**：集中托管插件安装包，发布插件版本并按
**目录修订（catalog revision）** 求解满足全部**传递依赖**的完整安装方案。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Uvicorn。
版本规则遵循 [PEP 440](https://peps.python.org/pep-0440/)（解析器为本仓库
`app/version.py` 的标准库实现，离线无第三方依赖也能跑核心求解器）。

---

## 1. 一键启动

```bash
cp .env.example .env        # 按需改口令/端口
docker compose up -d --build
```

首次启动自动完成：

1. PostgreSQL 容器初始化数据库（`POSTGRES_DB/USER/PASSWORD`）；
2. API 启动时自动建表并写入 **第 0 号目录修订（空目录）**；
3. 安装包持久化到命名卷 `packages`，数据库持久化到卷 `pgdata`。

健康检查与交互式文档：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | <http://localhost:8080/health> |
| Swagger UI | <http://localhost:8080/docs> |
| API 前缀 | `http://localhost:8080/api/v1` |

停止 / 清空数据：

```bash
docker compose down          # 停止
docker compose down -v       # 停止并删除数据库与安装包卷
```

### 不用 Docker（本机直跑）

```bash
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg2://plugindb:plugindb@localhost:5432/plugindb'
export PACKAGE_DIR=/var/lib/plugin-packages
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 2. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 由下列分项拼接 | SQLAlchemy/psycopg2 连接串 |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `db` / `5432` | 数据库地址（compose 网络） |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `plugindb` | 数据库凭据 |
| `PACKAGE_DIR` | `/data/packages` | 安装包内容寻址存储根目录（按 sha256 前两位分桶） |
| `MAX_UPLOAD_BYTES` | `1073741824`（1 GiB） | 单次上传大小上限 |
| `API_TOKEN` | 空 | 非空时写接口（发布/撤销/求解）要求 `Authorization: Bearer <token>`；读接口始终开放 |
| `API_PORT` | `8080` | 宿主机映射端口（compose） |

---

## 3. 领域规则

### 发布

`POST /api/v1/plugins/{plugin}/versions`（multipart/form-data）

- `metadata`：JSON 字符串；`package`：安装包文件。
- 服务端重新计算 **SHA-256**，与提交摘要不符 → **422 拒绝**，不落库不落盘。
- 版本号、依赖范围、运行环境范围逐一校验；**范围语法无效 → 422 拒绝**。
- 同一插件名下，**同名同版本发布后不可改写**（即使之后撤销也不行）→ 重复发布 **409**。
- 发布成功即产生一个**新的目录修订号**（单调递增）。

`metadata` 示例：

```json
{
  "plugin": "data-toolkit",
  "version": "3.0.0",
  "sha256": "……64 位十六进制……",
  "runtimes": [{"name": "python", "spec": ">=3.10"}],
  "dependencies": [{"plugin": "numpy", "spec": ">=2,<3"}],
  "conflicts": ["legacy-runtime"]
}
```

版本范围支持 `==`（含 `1.2.*` 前缀）、`!=`、`>`、`>=`、`<`、`<=`、
`~=`（兼容版本）、`===`，逗号表示交集；裸版本 `1.2.3` 等价 `==1.2.3`，
空字符串 / `*` 表示不限。

### 求解

`POST /api/v1/solutions`

```json
{
  "revision": 4,
  "runtimes": [{"name": "python", "spec": "3.11.*"}],
  "targets": {"data-toolkit": ">=3", "numpy": ""}
}
```

- `revision` 可省略，表示在**当前最新目录修订**上求解；给定修订号则在该历史修订上求解。
- 只从该修订内的**未撤销版本**中选择；求满足全部**传递依赖**的完整方案：
  - 每个插件**只能选一个版本**，多个来源的依赖范围取**交集**；
  - 候选版本必须兼容提交的**每一个**运行环境；
  - **互斥项不得共存**（任一方向声明即剪枝）；
  - 依赖图必须无环；**检测到依赖环返回 422 并给出环路**，例如
    `a -> b -> c -> a`；
  - 选择策略确定（版本降序 + MRV），**同一请求 + 同一目录修订必得同一方案**；
    成功方案按 `(revision, 请求规范化哈希)` 唯一保存，重复请求返回 `cached=true`。
- **无解**：返回 **409** 与结构化冲突明细（冲突插件、命中的约束、可用版本、
  运行环境阻塞项），**不保存任何部分方案**。
- **依赖环**：返回 **422**，`detail.conflicts[0].cycle` 为环路径。
- 求解事务运行在 **REPEATABLE READ** 快照上，整次求解只读同一目录修订；
  并发发布/撤销不会让一份方案混用不同修订。并发提交同一方案由数据库
  唯一约束去重，后提交者读取已保存方案返回。

响应示例：

```json
{
  "solution_id": 7,
  "revision": 4,
  "cached": false,
  "request_hash": "8c1f…",
  "selection": {
    "numpy":         {"version": "2.1.0", "sha256": "…", "package_url": "/api/v1/plugins/numpy/versions/2.1.0/download", "dependencies": [], "runtimes": [{"name":"python","spec":">=3.10"}], "conflicts": [], "revoked": false},
    "data-toolkit":  {"version": "3.0.0", "…": "…"}
  },
  "install_order": ["numpy", "data-toolkit"],
  "created_at": "2026-09-30T08:00:00Z"
}
```

`install_order` 为依赖拓扑序（被依赖者先安装）。

### 撤销

`POST /api/v1/plugins/{plugin}/versions/{version}/revoke`

- 撤销产生新目录修订；**仅影响之后的求解**。
- 历史方案永不删除，仍可用 `GET /api/v1/solutions/{id}` 查询；
  其中被撤销的选中项带 `"revoked": true`。
- 对旧修订号重放请求仍得到与当时一致的方案。
- 已撤销版本再次撤销 → 409。

---

## 4. HTTP API 一览（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plugins/{name}/versions` | 发布版本（multipart：`metadata` + `package`），201/409/422 |
| POST | `/plugins/{name}/versions/{ver}/revoke` | 撤销版本 |
| POST | `/solutions` | 求解并保存方案；409 无解 / 422 依赖环 |
| GET  | `/solutions/{id}` | 查询历史方案（含当前撤销标记） |
| GET  | `/revisions` | 目录修订列表（最新在前） |
| GET  | `/revisions/{id}` | 单个修订 |
| GET  | `/plugins?revision=N` | 插件与版本清单（默认最新修订，仅未撤销） |
| GET  | `/plugins/{name}/versions/{ver}` | 版本详情（含撤销状态） |
| GET  | `/plugins/{name}/versions/{ver}/download` | 下载安装包（响应头 `X-SHA256`、`X-Revoked`） |
| GET  | `/health` | 健康检查（不带前缀） |

错误响应统一为 `{"detail": ...}`；求解失败时 `detail` 为：

```json
{
  "detail": "不存在满足全部约束的安装方案",
  "code": "unsatisfiable",
  "revision": 5,
  "request_hash": "…",
  "conflicts": [
    {"type": "no_matching_version", "plugin": "numpy",
     "available": ["1.26.4"],
     "required_by": [{"plugin": "data-toolkit", "spec": ">=2,<3"}],
     "runtime_blocked": []}
  ]
}
```

冲突条目的 `type`：`no_matching_version`（范围/运行环境无可用版本）、
`missing_plugin`（插件或其依赖在目录中不存在）、`version_conflict`
（已选版本不满足新约束的交集）、`mutex_conflict`（互斥共存）、
`dependency_cycle`（依赖环，422）。

---

## 5. 调用示例

启动服务后任选其一：

```bash
# bash + curl：发布 4 个版本、求解、缓存、无解、撤销、历史查询、旧修订重放、下载
BASE=http://localhost:8080 bash examples/api-examples.sh

# 纯标准库 Python（离线机器无需安装 requests）
python3 examples/api_client.py --base http://localhost:8080
# 启用了 API_TOKEN 时： --token "$API_TOKEN"
```

单个 curl 速览：

```bash
# 发布（元数据与包分开提交；sha256 由服务端复核）
printf 'payload' > /tmp/dtk.bin
SHA=$(sha256sum /tmp/dtk.bin | awk '{print $1}')
curl -X POST http://localhost:8080/api/v1/plugins/data-toolkit/versions \
  -F "metadata={\"plugin\":\"data-toolkit\",\"version\":\"3.0.0\",
      \"sha256\":\"$SHA\",
      \"runtimes\":[{\"name\":\"python\",\"spec\":\">=3.10\"}],
      \"dependencies\":[{\"plugin\":\"numpy\",\"spec\":\">=2,<3\"}],
      \"conflicts\":[]};type=application/json" \
  -F "package=@/tmp/dtk.bin"

# 求解
curl -X POST http://localhost:8080/api/v1/solutions \
  -H 'Content-Type: application/json' \
  -d '{"runtimes":[{"name":"python","spec":"3.11.*"}],
       "targets":{"data-toolkit":">=3"}}'
```

---

## 6. 测试

核心版本规则、求解器与发布/撤销业务规则不依赖任何第三方库：

```bash
python3 tests/test_solver.py        # 19 用例：PEP440、传递依赖、互斥、环、回溯、运行环境、确定性
python3 tests/test_catalog_flow.py  # 5 用例：发布校验/不可改写/摘要拒绝/快照/撤销语义/请求哈希（内存仓储）
```

API 端到端测试（需要 FastAPI/SQLAlchemy 环境，可用内存 SQLite，无需起 PostgreSQL）：

```bash
pip install -r requirements.txt httpx
python3 tests/test_api.py
```

覆盖：摘要不符拒绝、非法范围拒绝、同名同版本不可改写、传递求解与缓存、
409 冲突明细不落部分方案、422 环、撤销只影响后续求解且历史方案带标记、
下载校验头、互斥端到端等。

---

## 7. 目录结构

```
app/
  main.py        # FastAPI 入口、建表与初始修订
  config.py      # 环境变量配置
  database.py    # 引擎 / 会话
  models.py      # ORM：修订、插件、版本、依赖、运行环境、互斥、方案
  schemas.py     # Pydantic 请求/响应模型与入参校验
  version.py     # PEP 440 版本与范围（标准库实现，无第三方依赖）
  solver.py      # 确定性回溯求解器（MRV + 冲突回溯 + 环检测）
  catalog.py     # 纯领域逻辑：发布校验、SHA-256、请求规范化哈希
  repository.py  # ORM 持久化：发布/撤销事务、修订快照、方案存取
  service.py     # 求解编排：解析、缓存、序列化、撤销标记
  routers.py     # HTTP 路由（PostgreSQL REPEATABLE READ 求解事务）
examples/        # curl / Python 调用示例
tests/           # 求解器单测、业务流内存测试、API 集成测试
docker-compose.yml  Dockerfile  requirements.txt
```

## 8. 设计说明：目录修订与并发

- `catalog_revisions` 是一张只追加的修订表（自增主键即修订号）。
- 每个版本记录 `published_revision` 与 `revoked_revision`：
  在修订 R 可见 ⇔ `published_revision <= R < revoked_revision`（未撤销则右端为空）。
- 求解在 **REPEATABLE READ** 事务中先确定修订号，再加载快照、求解、写方案；
  与并发的发布/撤销事务完全隔离，方案不会跨修订拼装。
- `solutions(revision_id, request_hash)` 唯一：相同输入在相同修订上复用
  同一方案，既保证“同请求同结果”，也自然消化并发重复求解。
