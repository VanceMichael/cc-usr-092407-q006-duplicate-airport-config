"""启动门禁：配置装载、机场事实核对与服务装配。

启动顺序保证不产生半初始化状态：

1. 先在内存中完整装载并校验配置与航班夹具——任何重复机场代码、
   非字符串名称、无效缓冲或语义重复的时区记录都会在此失败，此时
   数据库文件尚未被触碰；
2. 然后打开数据库（初始化是单事务，失败会清掉新建的空库文件）；
3. 最后把当前配置与库中已登记的机场事实、已有事件引用的机场作为
   一个整体核对——冲突时事务回滚并以非零码退出，旧实例保持可读，
   新实例不会监听端口，也就不会接管流量。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Config, airports_digest, load_airports, load_flights
from app.errors import AppError
from app.models import Airport, Flight
from app.repository import Repository
from app.service import DisruptionService

# 进程退出码：0 正常；2 配置/夹具无效；3 配置与库中机场事实冲突。
EXIT_CONFIG_ERROR = 2
EXIT_CONFIG_CONFLICT = 3


@dataclass
class AppContext:
    config: Config
    airports: dict[str, Airport]
    flights: dict[str, Flight]
    config_digest: str
    repo: Repository
    service: DisruptionService

    def close(self) -> None:
        self.repo.close()


def bootstrap(config: Config) -> AppContext:
    """执行完整启动门禁；任一阶段失败都抛出 AppError 且不写坏数据库。"""
    airports = load_airports(config.fixtures_dir)
    flights = load_flights(config.fixtures_dir, airports)
    digest = airports_digest(airports)
    repo = Repository(config.db_path)
    try:
        repo.register_airport_facts(airports, digest)
    except AppError:
        repo.close()
        raise
    service = DisruptionService(repo, airports, flights, config_digest=digest)
    return AppContext(
        config=config,
        airports=airports,
        flights=flights,
        config_digest=digest,
        repo=repo,
        service=service,
    )
