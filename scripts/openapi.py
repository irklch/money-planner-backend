"""Экспорт и проверка контракта OpenAPI.

python scripts/openapi.py          # перезаписать openapi.json
python scripts/openapi.py --check  # CI: упасть, если openapi.json устарел
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402

TARGET = pathlib.Path(__file__).resolve().parents[1] / "openapi.json"


def render() -> str:
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != content:
            print("openapi.json устарел: выполните `python scripts/openapi.py` и закоммитьте результат")
            return 1
        print("openapi.json актуален")
        return 0
    TARGET.write_text(content, encoding="utf-8")
    print(f"written {TARGET.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
