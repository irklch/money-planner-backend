"""Защита от одновременной записи в две БД после переключения сервера (deploy/portable).

Основной сервер — тот, на чей IP указывает домен API. Перед стартом migrate/api/maintenance
контейнер сравнивает A-записи FENCE_DOMAIN с FENCE_EXPECTED_IP этого сервера:

- совпадает → старт;
- домен указывает на другой IP → отказ (exit 2): этот сервер больше не основной, например
  старая VM, которая снова включилась после переезда;
- DNS не отвечает → старт с предупреждением: отказ DNS не должен ронять единственный сервер.

FENCE_MODE=off — для проверки нового сервера до переключения DNS (docs/disaster-recovery.md).

    python -m app.jobs.fence_check
"""

import logging
import os
import socket
import sys

log = logging.getLogger("app.fence")

OK, FENCED = 0, 2


def resolve_ipv4(domain: str) -> set[str]:
    infos = socket.getaddrinfo(domain, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return {info[4][0] for info in infos}


def check(env: dict[str, str], resolver=resolve_ipv4) -> tuple[int, str]:
    mode = env.get("FENCE_MODE", "enforce")
    domain = env.get("FENCE_DOMAIN", "")
    expected = env.get("FENCE_EXPECTED_IP", "")
    if mode == "off":
        return OK, "fence_disabled"
    if mode != "enforce":
        return FENCED, f"FENCE_MODE must be enforce or off, got {mode!r}"
    if not domain or not expected:
        return FENCED, "FENCE_DOMAIN and FENCE_EXPECTED_IP are required when FENCE_MODE=enforce"
    try:
        addresses = resolver(domain)
    except OSError as e:
        return OK, f"dns_unavailable ({type(e).__name__}), starting anyway"
    if expected in addresses:
        return OK, "primary"
    return FENCED, f"{domain} -> {sorted(addresses)}, this server is {expected}: not primary, refusing"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    code, reason = check(dict(os.environ))
    (log.info if code == OK else log.error)("fence_check: %s", reason)
    return code


if __name__ == "__main__":
    sys.exit(main())
