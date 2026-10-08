"""Прототип sync-протокола Money Planner на YDB. Изолирован от `app/`, production-код не импортирует."""

# Состав: models (контракт) → resolve (правила конфликтов) → engine (алгоритм) → store_* (хранилища) → api.
