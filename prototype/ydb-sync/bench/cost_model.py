"""Пересчёт стоимости инфраструктуры по MAU из результатов benchmark.

    python -m bench.cost_model [bench/results/local_benchmark.json]  → bench/results/cost_model.md

Каждая величина помечена источником:
  [И]  измерено в прототипе (локальная YDB): RU чтения, строки/байты, размер хранения;
  [Ф]  RU по официальной формуле из измеренных строк/байт (записи локальная YDB не тарифицирует);
  [Д]  допущение нагрузочной модели или цена из architecture-local-first.md (06–07.10.2026);
  [П]  прогноз, не измерялось (auth, контейнеры, холодные старты в облаке).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

PRICE = {  # [Д] ₽ с НДС, architecture-local-first.md / infrastructure-research.md
    "ru_per_million": 24.64,
    "ru_free": 1_000_000,
    "storage_gb_hour": 0.0342,
    "storage_free_gb": 1.0,
    "container_vcpu_hour": 5.69,
    "container_gb_hour": 3.79,
    "container_per_million": 18.97,
    "container_free_vcpu_h": 5,
    "container_free_gb_h": 10,
    "container_free_calls": 1_000_000,
    "gateway_per_million": 142.3,
    "gateway_free": 100_000,
    "fixed": 24.0,  # Lockbox 20 + Registry 4
    "provisioned_ru_s_hour": 0.0184,
    "export_ru_per_mb": 128,
    "cold_storage_gb_hour": 0.00176,
}
PROFILES = {  # [Д] §2 документа: на MAU с аккаунтом в месяц
    "base": {"pulls": 20, "pushes": 15, "records_per_push": 8, "auth": 25},
    "heavy": {"pulls": 60, "pushes": 45, "records_per_push": 8, "auth": 75},
}
DEVICES_PER_USER = 1.5  # [Д] каждая отправленная запись читается pull-ом каждого устройства (включая своё)
NEW_DEVICE_SHARE = 0.05  # [Д] доля MAU в месяц, восстанавливающих историю на новом телефоне
NEW_ACCOUNT_SHARE = 0.05  # [Д] доля MAU в месяц, выгружающих локальную историю при регистрации
HISTORY_RECORDS = 1000  # [Д] средний размер истории при восстановлении/первой выгрузке
AUTH_RU = 8  # [П] refresh/me — не прототипировались, оценка документа
REQUEST_SECONDS = 0.1  # [Д] минимальная единица тарификации контейнера
COLD_START_S_PER_MAU = {0: 0, 100: 20, 1000: 5, 10000: 1, 50000: 0.5}  # [П] документ
MAUS = [0, 100, 1000, 10000, 50000]


def _ops(bench: dict) -> dict[str, dict]:
    return {o["op"]: o for o in bench["operations"]}


def unit_costs(bench: dict) -> dict[str, float]:
    ops = _ops(bench)
    st = bench["storage"]
    rec = st["sync_records"]
    idx = st["sync_records/by_version/indexImplTable"]
    log = st["sync_mutations"]
    nocover = (
        bench["variants"].get("storage_index_without_cover", {}).get("sync_records/by_version/indexImplTable")
    )
    return {
        # [И] pull: заголовок YDB = формула; 1 RU на строку sync_state + 1 RU на запись страницы
        "pull_fixed_ru": ops["pull_0_no_changes"]["ru_header"],
        "pull_per_record_ru": (ops["pull_100"]["ru_header"] - ops["pull_0_no_changes"]["ru_header"]) / 100,
        # [Ф] push: формула из фактических строк/байт
        "push_fixed_ru": ops["push_8"]["ru_formula"]
        - 8 * (ops["push_100"]["ru_formula"] - ops["push_8"]["ru_formula"]) / 92,
        "push_per_record_ru": (ops["push_100"]["ru_formula"] - ops["push_8"]["ru_formula"]) / 92,
        "restore_1000_ru": ops["initial_download_1000"]["ru_header"],  # [И]
        "upload_1000_ru": ops["initial_upload_1000"]["ru_formula"],  # [Ф]
        # [И] байт на запись: таблица + индекс; строка журнала мутаций (живёт 30 дней)
        "bytes_per_record_cover": rec["bytes"] / rec["rows"] + idx["bytes"] / idx["rows"],
        "bytes_per_record_nocover": rec["bytes"] / rec["rows"]
        + (nocover["bytes"] / nocover["rows"] if nocover else math.nan),
        "bytes_per_log_row": log["bytes"] / log["rows"],
    }


def monthly(
    mau: int, profile: str, u: dict[str, float], years: float = 1.0, cover: bool = True
) -> dict[str, float]:
    p = PROFILES[profile]
    records_pushed = p["pushes"] * p["records_per_push"]
    pull_ru = p["pulls"] * u["pull_fixed_ru"] + DEVICES_PER_USER * records_pushed * u[
        "pull_per_record_ru"
    ] * (1 if cover else 2)  # без COVER — lookup в основную таблицу: ×2 на запись (измерено: 203 vs 102)
    push_ru = p["pushes"] * u["push_fixed_ru"] + records_pushed * u["push_per_record_ru"]
    init_ru = (
        (
            NEW_DEVICE_SHARE * u["restore_1000_ru"] * (1 if cover else 2)
            + NEW_ACCOUNT_SHARE * u["upload_1000_ru"]
        )
        * HISTORY_RECORDS
        / 1000
    )
    auth_ru = p["auth"] * AUTH_RU
    ru_per_mau = pull_ru + push_ru + init_ru + auth_ru
    ru = mau * ru_per_mau
    ru_cost = max(0.0, ru - PRICE["ru_free"]) / 1e6 * PRICE["ru_per_million"]

    bytes_rec = u["bytes_per_record_cover" if cover else "bytes_per_record_nocover"]
    data_gb = mau * (records_pushed * 12 * years * bytes_rec + records_pushed * u["bytes_per_log_row"]) / 1e9
    storage_cost = max(0.0, data_gb - PRICE["storage_free_gb"]) * PRICE["storage_gb_hour"] * 720

    requests = mau * (p["pulls"] + p["pushes"] + p["auth"])
    cold_s = mau * COLD_START_S_PER_MAU.get(mau, 0.5)
    vcpu_h = (requests * REQUEST_SECONDS + cold_s) / 3600
    gb_h = vcpu_h * 0.5
    containers = (
        max(0.0, vcpu_h - PRICE["container_free_vcpu_h"]) * PRICE["container_vcpu_hour"]
        + max(0.0, gb_h - PRICE["container_free_gb_h"]) * PRICE["container_gb_hour"]
        + max(0.0, requests - PRICE["container_free_calls"]) / 1e6 * PRICE["container_per_million"]
    )
    gateway = max(0.0, requests - PRICE["gateway_free"]) / 1e6 * PRICE["gateway_per_million"]
    # Еженедельный экспорт в Object Storage, 4 копии в холодном классе (документ §5, сноска 4).
    backup = (
        (
            4 * data_gb * 1024 * PRICE["export_ru_per_mb"] / 1e6 * PRICE["ru_per_million"]
            + 4 * data_gb * PRICE["cold_storage_gb_hour"] * 720
        )
        if mau >= 1000
        else 0.0
    )
    # Выделенная пропускная способность: средняя нагрузка ×2 запас, переполнение — on-demand 10%.
    ru_s = math.ceil(2 * ru / (30 * 86400)) if ru else 0
    provisioned = ru_s * 720 * PRICE["provisioned_ru_s_hour"] + 0.1 * ru / 1e6 * PRICE["ru_per_million"]
    total = PRICE["fixed"] + ru_cost + storage_cost + containers + gateway + backup
    return {
        "ru_per_mau": ru_per_mau,
        "pull_ru": pull_ru,
        "push_ru": push_ru,
        "init_ru": init_ru,
        "auth_ru": auth_ru,
        "ru_million": ru / 1e6,
        "ru_cost": ru_cost,
        "data_gb": data_gb,
        "storage_cost": storage_cost,
        "containers": containers,
        "gateway": gateway,
        "backup": backup,
        "total": total,
        "total_provisioned": total - ru_cost + provisioned if mau >= 50000 else total,
    }


def render(bench: dict) -> str:
    u = unit_costs(bench)
    lines = [
        "# Стоимость по MAU (пересчёт из локального benchmark)",
        "",
        "Метки: **[И]** измерено, **[Ф]** формула из измеренных строк/байт, **[Д]** допущение/цена из "
        "документа, **[П]** прогноз без измерения. Цены — architecture-local-first.md (07.10.2026).",
        "",
        "## Удельные величины",
        "",
        "| Величина | Значение | Источник |",
        "|---|---:|---|",
        f"| pull: фиксированная часть, RU | {u['pull_fixed_ru']:.0f} | [И] |",
        f"| pull: на запись, RU | {u['pull_per_record_ru']:.2f} | [И] |",
        f"| push: фиксированная часть, RU | {u['push_fixed_ru']:.1f} | [Ф] |",
        f"| push: на мутацию, RU | {u['push_per_record_ru']:.2f} | [Ф] |",
        f"| восстановление 1000 записей, RU | {u['restore_1000_ru']:.0f} | [И] |",
        f"| первая выгрузка 1000 записей, RU | {u['upload_1000_ru']:.0f} | [Ф] |",
        f"| хранение: запись + covering-индекс, байт | {u['bytes_per_record_cover']:.0f} | [И] |",
        f"| хранение: запись + индекс без COVER, байт | {u['bytes_per_record_nocover']:.0f} | [И] |",
        f"| хранение: строка журнала мутаций (30 дней), байт | {u['bytes_per_log_row']:.0f} | [И] |",
        f"| refresh/me, RU | {AUTH_RU} | [П] |",
        "",
    ]
    for profile in PROFILES:
        rows = {m: monthly(m, profile, u) for m in MAUS}
        lines += [
            f"## Профиль {profile} (через 1 год хранения истории), ₽/мес",
            "",
            "| MAU | " + " | ".join(f"{m:,}".replace(",", " ") for m in MAUS) + " |",
            "|---|" + "---:|" * len(MAUS),
        ]
        for key, title in [
            ("ru_per_mau", "RU на MAU в месяц [И+Ф+П]"),
            ("ru_million", "RU всего, млн"),
            ("ru_cost", "YDB RU"),
            ("data_gb", "Объём YDB, ГБ [И]"),
            ("storage_cost", "YDB хранение"),
            ("containers", "Контейнеры [Д/П]"),
            ("gateway", "API Gateway [Д]"),
            ("backup", "Экспорт бэкапов [Д]"),
            ("total", "**Итого**"),
            ("total_provisioned", "Итого с выделенной пропускной способностью"),
        ]:
            fmt = "{:.2f}" if key in ("ru_million", "data_gb") else "{:.0f}"
            lines.append(f"| {title} | " + " | ".join(fmt.format(rows[m][key]) for m in MAUS) + " |")
        r = rows[10000]
        lines += [
            "",
            f"Разбивка RU на MAU ({profile}): pull {r['pull_ru']:.0f} [И], push {r['push_ru']:.0f} [Ф], "
            f"восстановление/выгрузка {r['init_ru']:.0f} [И/Ф], auth {r['auth_ru']:.0f} [П].",
            "",
        ]
    lines += [
        "## Covering-индекс против индекса без COVER (base, 50 000 MAU)",
        "",
        "| Горизонт хранения | COVER, ₽/мес | без COVER, ₽/мес |",
        "|---|---:|---:|",
    ]
    for years in (1, 3):
        c = monthly(50000, "base", u, years, cover=True)
        n = monthly(50000, "base", u, years, cover=False)
        lines.append(f"| {years} год(а) | {c['total']:.0f} | {n['total']:.0f} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else RESULTS / "local_benchmark.json"
    md = render(json.loads(path.read_text()))
    (RESULTS / "cost_model.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
