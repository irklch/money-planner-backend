"""Чтение файла выписки строго в памяти (без временных файлов на диске).

Поддерживаемые форматы по контракту: .xlsx (python-calamine) и .csv (csv + charset-normalizer).
"""

import csv
import io

from charset_normalizer import from_bytes
from python_calamine import CalamineWorkbook

from app.modules.imports.parsing.base import Sheet, StatementError, Table

ZIP_MAGIC = b"PK\x03\x04"
SUPPORTED_EXTENSIONS = {".xlsx": "xlsx", ".csv": "csv"}
MAX_ROWS = 50_000


def detect_format(filename: str | None) -> str:
    name = (filename or "").lower().strip()
    for ext, fmt in SUPPORTED_EXTENSIONS.items():
        if name.endswith(ext):
            return fmt
    raise StatementError("unsupported_format")


def read_table(data: bytes, fmt: str) -> Table:
    if fmt == "xlsx":
        return _read_xlsx(data)
    return _read_csv(data)


def _read_xlsx(data: bytes) -> Table:
    if not data.startswith(ZIP_MAGIC):
        raise StatementError("corrupted_file")
    try:
        wb = CalamineWorkbook.from_filelike(io.BytesIO(data))
        sheets = []
        for name in wb.sheet_names:
            rows = wb.get_sheet_by_name(name).to_python(skip_empty_area=False)
            sheets.append(Sheet(name=name, rows=[list(r) for r in rows[:MAX_ROWS]]))
    except Exception:  # calamine бросает разные типы ошибок для битых файлов
        raise StatementError("corrupted_file") from None
    if not sheets:
        raise StatementError("corrupted_file")
    return Table(sheets=sheets, fmt="xlsx")


def _read_csv(data: bytes) -> Table:
    if not data.strip():
        raise StatementError("empty_statement")
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise StatementError("corrupted_file")
    best = from_bytes(data).best()
    if best is None:
        raise StatementError("corrupted_file")
    text = str(best)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    try:
        rows = []
        for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
            if i >= MAX_ROWS:
                break
            rows.append(row)
    except csv.Error:
        raise StatementError("corrupted_file") from None
    return Table(sheets=[Sheet(name="csv", rows=rows)], fmt="csv")
