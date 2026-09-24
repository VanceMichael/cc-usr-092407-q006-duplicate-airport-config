# 机场中断影响服务

本项目提供纯后端机场中断影响服务。服务接收机场关闭、延长关闭和恢复开放事件，计算受影响的既有航班与旅客，将事件链和计算结果保存到 SQLite，并通过 HTTP 接口提供查询。

示例中的机场、航班时刻和旅客数量均为合成数据。运行期间不会请求外部航班、地图或通知服务。

## 目录

- `contracts/disruption-event.schema.json`：中断事件输入契约。
- `fixtures/airports.json`：机场时区与恢复缓冲时间。
- `fixtures/flights.json`：确定性的航班计划数据，包含跨午夜样例。
- `app/`：Python 3.12 标准库实现的业务服务。
- `tests/`：计算、校验、存储和 HTTP 集成测试。
- `scripts/docker_selftest.sh`：容器化黑盒自检入口。
- `compose.yaml`：输入校验容器、业务服务和 SQLite 持久卷。

## 启动

```bash
docker compose up -d --build --wait
curl -s http://127.0.0.1:8080/healthz
docker compose down
```

SQLite 默认位于容器内的 `/data/disruptions.db`，由 `disruption-data` 卷保存。`DB_PATH`、`HOST`、`PORT` 和 `FIXTURES_DIR` 均可通过环境变量调整。

本地运行只需要 Python 3.12：

```bash
python3 -m unittest discover -s tests
DB_PATH=./data/disruptions.db PORT=8080 python3 -m app
```

## 接口

所有请求和响应均为 JSON，错误统一使用以下结构：

```json
{"error": {"code": "unknown_airport", "message": "...", "details": {}}}
```

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/events` | 提交关闭、延长或恢复事件 |
| `GET` | `/api/v1/events/{event_id}` | 查询事件、处理状态和影响结果 |
| `GET` | `/api/v1/airports/{AIRPORT}/summary` | 查询机场影响汇总 |
| `GET` | `/api/v1/flights/affected` | 分页查询当前受影响航班 |
| `GET` | `/healthz` | 存活性：进程与数据库可打开（独立于启动门禁） |
| `GET` | `/readyz` | 就绪性：配置门禁已通过、本实例可接管流量 |

`/healthz` 与 `/readyz` 刻意分离：升级时若新配置与数据库中已登记的机场事实冲突，新实例**保持存活**（`/healthz` 200）以便诊断，但**不报告就绪、不处理任何 `/api` 请求**（均为 503），编排器据此不向其转发流量，旧实例在同一卷上继续可读。容器与 compose 的健康检查探测 `/readyz`。

受影响航班查询支持 `airport`、`status`、`limit` 和 `offset` 参数。`status` 可取 `cancelled`、`delayed` 或 `pending_confirmation`。

## 事件规则

- 所有输入时间必须携带时区，比较前统一转换为 UTC。
- `airport.closed` 创建事件链；未知结束时间可将 `effective_until` 设为 `null`。
- `airport.extended` 通过 `supersedes_event_id` 延长尚未结束的事件链。
- `airport.reopened` 结束事件链，机场在自身恢复缓冲时间结束后重新运行。
- 时间窗口采用左闭右开语义，恰好落在恢复时刻的航班不受影响。
- 同一航班分别按出发机场的计划起飞时刻和到达机场的计划到达时刻判断。
- 无法改时或所需延误超过上限的航班标记为 `cancelled`；可在上限内改时的航班标记为 `delayed`；结束时间未知时标记为 `pending_confirmation`。
- 跨午夜依据受影响机场的本地时区判定。
- `event_id` 是幂等键。相同内容重试返回原结果；相同标识携带不同内容时返回 `409 event_conflict`。
- 事件链版本必须递增，且不能继续延长已经恢复开放的事件链。

## 配置完整性与启动门禁

机场定义（`fixtures/airports.json`）在装载时严格校验，绝不依赖字典"最后一项获胜"：

- 同一三字码出现两次即错误，即使两行完全相同；冲突行会同时给出两处来源位置
  （`<文件路径>[<数组下标>]`），覆盖名称、时区、恢复缓冲差异。
- 名称必须是非空字符串（不做隐式 `str()` 转换）；`reopen_buffer_minutes`
  必须是非负整数（布尔、浮点、负数、字符串都被拒绝）；时区必须是可加载的 IANA 名称。
- 两条记录用不同字符串写出**语义相同**的时区（IANA 链接别名，如 `Singapore`
  与 `Asia/Singapore`、`Zulu` 与 `Etc/UTC`）会报 `semantic_timezone_duplicate`，
  时区先归一化到规范名。
- 所有问题一次收集并按 `(location, field, issue)` 排序，错误结果与文件行序无关。
- 配置、航班引用作为一个整体核对：航班引用未知机场同样带位置报错。

机场集合生成顺序无关的规范摘要（按三字码排序、时区用规范名）的 SHA-256 `config_digest`；
调整夹具行序或改用别名拼写都不改变摘要。启动时：

1. 先完整校验配置。配置文件本身非法时进程以退出码 2 终止，**此前不打开、不创建
   任何数据库文件**。
2. 再在**单个 `BEGIN IMMEDIATE` 事务**内完成 schema 建立/迁移、机场事实登记与对账。
   任一步失败整体回滚，不产生半初始化数据库；v1 库升级冲突时文件保持 v1，旧实例仍可打开。
3. 已登记的机场事实、配置与库内事件/影响引用的代码被整体核对。发现定义被修改或
   机场被删除即门禁失败：本实例存活但不就绪、拒绝接管流量，且不重写任何已登记事实。
4. `busy_timeout` 让多进程/多容器同时挂同一卷启动时在写锁上排队，先提交者登记
   唯一定义，后来者对齐到它；携带不同定义的实例恰好一个被登记，其余收到
   `config_conflict`，不存在覆盖。

每条事件结果（`POST` 响应与 `GET /events/{id}`）都在 `config` 字段中保存
`config_digest` 与计算所用的机场定义；完整规范摘要以内容寻址方式存于
`config_revisions`，机场事实存于 `airport_registry`。

## 编译检查

```bash
python3 -m compileall -q app
```

## 验证

单元与集成测试：

```bash
python3 -m unittest discover -s tests -v
```

完整容器自检：

```bash
scripts/docker_selftest.sh
```

容器自检会从空卷构建并启动服务，验证输入校验、幂等重放、跨午夜计算、事件链变化、分页查询、容器重建后的数据保留，以及启动门禁：行序打乱的第二实例对齐到同一摘要、携带冲突定义的第二实例存活但 503 且不改动数据库，结束时清理测试资源。

原始契约与夹具也可单独校验：

```bash
docker compose up -d --build scaffold
docker compose exec scaffold sh scaffold/validate_inputs.sh
```
