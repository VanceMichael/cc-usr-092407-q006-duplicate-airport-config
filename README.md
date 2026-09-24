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
| `GET` | `/healthz` | 存活性：进程与数据库连接可用 |
| `GET` | `/readyz` | 就绪性：机场事实已登记且与装载配置一致，可接管流量 |

受影响航班查询支持 `airport`、`status`、`limit` 和 `offset` 参数。`status` 可取 `cancelled`、`delayed` 或 `pending_confirmation`。

## 配置装载与启动门禁

机场配置是计算的唯一事实来源，装载与启动遵循以下规则：

- `fixtures/airports.json` 中同一三字码出现两次即拒绝启动——无论两条记录
  字段冲突还是内容完全一致（语义重复），都抛出带 `airports[i]` 来源位置
  的确定性错误，绝不靠"最后一项覆盖前一项"消解歧义。
- `name`、`timezone` 必须是字符串，`reopen_buffer_minutes` 必须是非负整数，
  时区必须是有效的 IANA 名称；航班引用的机场代码必须存在于配置中。
- 装载成功后，全部机场定义（代码、名称、时区、恢复缓冲）计算出一个与
  文件记录顺序无关的 SHA-256 配置摘要（`airports_digest`）。
- 首次启动时摘要与机场事实登记到 SQLite（`airport_facts` 与
  `config_registry` 表）；此后每次启动都把当前配置与库中已登记事实、
  已有事件引用的机场作为一个整体核对。修改或删除已登记机场、事件引用
  了配置外的机场，都会以退出码 3 拒绝启动；配置本身无效以退出码 2 拒绝。
- 冲突实例不监听端口，不会接管流量；核对在单事务内完成，失败整体回滚，
  数据库保持旧实例可读的状态，不会留下半初始化数据。新增机场允许登记。
- 每个事件的处理结果都保存计算时使用的配置摘要（`config_digest`），
  事件查询与重放响应中均可核对。

`/healthz` 与 `/readyz` 分离：前者只表示进程活着，后者还要求库中登记的
机场事实摘要与当前装载配置一致。容器健康检查使用 `/readyz`，因此未通过
启动门禁的实例永远不会被标记为 healthy。

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

容器自检会从空卷构建并启动服务，验证输入校验、幂等重放、跨午夜计算、事件链变化、分页查询、容器重建后的数据保留、机场记录顺序变化后的摘要稳定性，以及冲突实例被启动门禁拒绝而旧实例继续服务，结束时清理测试资源。

原始契约与夹具也可单独校验：

```bash
docker compose up -d --build scaffold
docker compose exec scaffold sh scaffold/validate_inputs.sh
```
