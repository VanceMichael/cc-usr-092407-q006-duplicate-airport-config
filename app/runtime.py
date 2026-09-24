"""应用启动引导：配置装载、数据库门禁与就绪状态装配。

启动顺序是刻意安排的：

1. 先完整装载并校验机场与航班夹具。配置文件本身非法时，进程以退出码 2
   终止，且 **在此之前不打开、不创建任何数据库文件**——绝不产生半初始化库。
2. 再打开（必要时创建）数据库并运行 :meth:`Repository.gate`：schema 建立、
   迁移、机场事实登记与对账在单事务内完成。
3. 门禁通过 → 装配可服务流量的状态；门禁冲突（升级时配置与已登记事实矛盾）
   → 装配"存活但未就绪"的诊断状态：``/healthz`` 仍为 ok，``/readyz`` 与所有
   ``/api`` 请求返回 503，数据库内容不被修改，旧版本实例在同一卷上继续可读。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import AirportConfig, Config, load_airports, load_flights
from app.errors import ConfigConflictError
from app.repository import GateStatus, Repository
from app.server import AppState
from app.service import DisruptionService


@dataclass
class Runtime:
    state: AppState
    repo: Repository
    airport_config: AirportConfig | None
    gate_status: GateStatus | None
    conflicting: bool


def build_runtime(config: Config) -> Runtime:
    """按确定性顺序装配运行时。配置非法时直接抛出，不触碰数据库。"""
    airport_config = load_airports(config.fixtures_dir)
    flights = load_flights(config.fixtures_dir, airport_config)

    locations = {
        entry.airport.code: entry.location for entry in airport_config.entries()
    }

    repo = Repository(config.db_path)
    try:
        gate_status = repo.gate(
            airport_config.digest, airport_config.manifest(), locations
        )
    except ConfigConflictError as exc:
        # Old data stays on disk untouched and readable; this instance refuses
        # traffic but stays alive for diagnostics (/healthz ok, /readyz 503).
        state = AppState(service=None, gate_status=None, gate_error=exc)
        return Runtime(
            state=state,
            repo=repo,
            airport_config=None,
            gate_status=None,
            conflicting=True,
        )

    service = DisruptionService(repo, airport_config, flights)
    state = AppState(service=service, gate_status=gate_status, gate_error=None)
    return Runtime(
        state=state,
        repo=repo,
        airport_config=airport_config,
        gate_status=gate_status,
        conflicting=False,
    )
