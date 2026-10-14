"""Проверка, какие порты Compose публикует наружу, по итоговой конфигурации (`docker compose config`).

    python3 deploy/portable/check_exposure.py            # оба режима на синтетическом .env (CI)
    python3 deploy/portable/check_exposure.py --env .env # режим из реального .env на сервере

Правила:
- тестовый режим (пустой COMPOSE_PROFILES): наружу не публикуется ничего, API — только 127.0.0.1;
- публичный режим (COMPOSE_PROFILES=public): наружу только caddy 80/443, API — только 127.0.0.1;
- PostgreSQL не публикуется ни в одном режиме;
- публичный режим без API_DOMAIN не собирается (Compose отказывает при интерполяции).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
BASE_ENV = {
    "API_DOMAIN": "api.example.test",
    "FENCE_EXPECTED_IP": "203.0.113.10",
    "INSTANCE_NAME": "mp-check",
    "POSTGRES_PASSWORD": "check",
    "JWT_SECRET": "check-jwt",
    "TOKEN_DERIVATION_SECRET": "check-derivation",
    "BACKUP_AGE_RECIPIENT": "age1check",
}


def compose_config(env_file: Path, profiles: str | None, override: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROFILES"}
    if profiles is not None:
        env["COMPOSE_PROFILES"] = profiles
    files = ["-f", str(HERE / "docker-compose.yml")] + (["-f", str(override)] if override else [])
    out = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), *files, "config", "--format", "json"],
        capture_output=True,
        text=True,
        env=env,
        cwd=HERE,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)


def published(cfg: dict) -> dict[str, list[tuple[str, str]]]:
    """service -> [(host_ip, published_port)]; пустой host_ip = все интерфейсы."""
    return {
        name: [(p.get("host_ip", ""), str(p["published"])) for p in svc.get("ports", []) if "published" in p]
        for name, svc in cfg["services"].items()
    }


def check(cfg: dict, public: bool) -> list[str]:
    problems = []
    ports = published(cfg)
    for svc, binds in ports.items():
        for host_ip, port in binds:
            external = host_ip not in LOOPBACK
            if svc == "caddy" and public and port in {"80", "443"}:
                continue
            if external:
                problems.append(f"{svc} publishes {host_ip or '0.0.0.0'}:{port} to the internet")
    if ports.get("db"):
        problems.append(f"db publishes ports {ports['db']}")
    if public and "caddy" not in cfg["services"]:
        problems.append("public mode without caddy")
    if not public and "caddy" in cfg["services"]:
        problems.append("caddy is active without COMPOSE_PROFILES=public")
    return problems


def write_env(path: Path, values: dict) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def main() -> int:
    failures = []
    if "--env" in sys.argv:
        env_file = Path(sys.argv[sys.argv.index("--env") + 1]).resolve()
        profiles = next(
            (
                ln.split("=", 1)[1].strip()
                for ln in env_file.read_text().splitlines()
                if ln.startswith("COMPOSE_PROFILES=")
            ),
            "",
        )
        cfg = compose_config(env_file, profiles)
        mode = "public" if profiles == "public" else "test"
        failures += [f"[{mode}] {p}" for p in check(cfg, public=mode == "public")]
        print(f"{mode}: services={sorted(cfg['services'])} published={published(cfg)}")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            # Сервисы читают env_file: .env рядом с compose; в проверке подменяем его синтетическим.
            override = Path(tmp) / "override.yml"
            app_services = ("migrate", "api", "maintenance", "backup")
            override.write_text(
                "services:\n"
                + "".join(f"  {s}: {{env_file: !override [{env_file}]}}\n" for s in app_services),
            )
            for mode, profiles in (("test", ""), ("public", "public")):
                write_env(env_file, {**BASE_ENV, "COMPOSE_PROFILES": profiles})
                cfg = compose_config(env_file, profiles, override)
                failures += [f"[{mode}] {p}" for p in check(cfg, public=mode == "public")]
                print(f"{mode}: services={sorted(cfg['services'])} published={published(cfg)}")
            write_env(
                env_file,
                {**{k: v for k, v in BASE_ENV.items() if k != "API_DOMAIN"}, "COMPOSE_PROFILES": "public"},
            )
            try:
                compose_config(env_file, "public", override)
                failures.append("[public] config accepted without API_DOMAIN")
            except RuntimeError:
                print("public without API_DOMAIN: rejected")
    for f in failures:
        print("FAIL", f)
    print("exposure check:", "FAILED" if failures else "ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
