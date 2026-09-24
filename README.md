# 地震灾害科学协同服务

这是一个面向地震台网与应急指挥中心的模块化后端，集中管理地震事件、台站观测、震情计算、灾情协同、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 震情档案：登记地震事件、震源参数和台站观测，保留计算输入摘要。
- 科学计算：提供震级、距离和烈度的确定性计算，以及可恢复后台任务。
- 参数治理：烈度计算参数升级前必须经过预演、审批和原子发布，预演不改动生效数据，重复确认与过期候选安全失败，全程审计。
- 灾情协同：管理灾情报告、公告、部门责任和跨部门办理状态。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 烈度参数预演与发布

生效计算参数保存在 `seismic_param_versions` 表中，全局有且仅有一个 `active` 版本（由部分唯一索引强制），系统初始化时自动播种基线版本 `baseline-2026.1`（`gmpe-2026.1 / 步长10km / 半径100km / pga权重0.01 / 质量阈值0.6`）。变更必须走以下流程，全程写入 `audit_events`：

1. **发起预演**（需 `seismic.params.rehearse`，隐含读权限）：`POST /api/seismic/param-rehearsals`，提交候选参数集（含模型版本、网格步长、半径、PGA 权重、质量阈值）和代表性事件（最多 50 个；不传则自动选取含观测的已发布/在审事件）。服务对每个事件分别用基线与候选参数做**纯内存**计算，生成报告：
   - 输入差异：变更键、基线/候选逐键值与双方指纹；
   - 结果差异：逐事件变更点数、最大/平均烈度差、基线与候选的四分区（东北/东南/西北/西南）烈度摘要；
   - 预计影响范围：受影响事件数（含其中已发布事件数）、受影响分区数、切换后需重算的已完成计算任务数；
   - 权限检查：发起人能否审批/发布，以及当前具备审批权、发布权的角色清单。
2. **审批**（需 `seismic.params.approve`）：`POST /api/seismic/param-rehearsals/{id}/approve|reject`。状态机为 `pending → approved/rejected`，重复审批返回 409。
3. **发布**（需 `seismic.params.publish`，与审批职责分离）：`POST /api/seismic/param-rehearsals/{id}/publish`。在单个 `BEGIN IMMEDIATE` 事务内旧版本置 `retired`、新版本置 `active`、预演置 `published`，并再次校验候选指纹与基线指纹；未批准、已发布、已驳回、已过期（默认 72 小时，惰性过期）以及发布时生效版本已被他人切换的情况一律 409 安全失败。发布后新入队计算默认采用新生效版本的模型与网格参数。
4. **查询**：`GET /api/seismic/params/active`（最终发布版本）、`GET /api/seismic/params/versions`（所有版本）、`GET /api/seismic/param-rehearsals`（预演状态列表）、`GET /api/seismic/param-rehearsals/{id}`（含候选参数与报告）、`GET /api/seismic/param-rehearsals/{id}/report`（报告摘要）。

预演状态持久化在 SQLite 中但不存在任何后台自动发布任务；服务重启后未完成预演仍是 `pending`/`approved`，绝不会变成已发布，过期判定在下次访问时进行并写审计。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、事件与台站观测、烈度计算、后台任务去重与领取，以及数据库时间格式；烈度参数治理覆盖报告四要素、预演不改动生产数据、审批/发布职责分离、重复确认与过期安全失败、旧基线安全失败、重启不自动发布和各阶段审计。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  seismic/         地震事件、台站观测和科学计算服务
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
