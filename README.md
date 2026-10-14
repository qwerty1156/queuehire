# QueueHire — multi-tenant backend repair case study

**Portfolio case:** repair of an existing FastAPI backend, with tenant isolation,
transactional billing, idempotency, concurrent workers, retries and data-preserving migrations.

**Provenance:** the starter was supplied as an AI-generated technical challenge.
This repository contains the corrected submission, developed with AI assistance;
it is not a from-scratch commercial product. The AI provider is simulated locally.

Historical PostgreSQL results are documented in REPORT.md. They are evidence from the
submission environment, not a claim that every checkout has just passed those checks.
Known limits: an unexpected provider exception can leave a job running; worker crash
recovery is outside the original contract; deep OFFSET pagination can be expensive.

Это учебная симуляция работы с унаследованным backend, созданная специально
для проверки результата разработки с ИИ. Это не код реальной компании.

Ты — разработчик, которому передали сервис анализа вакансий. API запускается,
но клиенты жалуются на утечку данных, списания кредитов и поведение очереди.
Нужно исправить сервис и добавить ручной повтор обработки.

**Начни с [TASK.md](TASK.md). Там полный контракт и правила сдачи.**

## Исправленная версия

Реализован весь контракт: изоляция компаний, атомарное создание/оплата,
конкурентный worker, фильтрация/пагинация списка и оплачиваемый ручной retry.
Перед запуском примени `python -m alembic upgrade head`: добавлены `0002` и
`0003`, исходная `0001` сохранена. Зависимости приложения не изменились.

Фактическая проверка на PostgreSQL 16.2: **116 passed**, без failed/skipped.
Список: 4 SQL вместо 23, 0 полных ORM-объектов Job вместо 1000.
Команды, миграции, EXPLAIN и ограничения: [REPORT.md](REPORT.md).

ИИ разрешён без ограничений. Писать код вручную не требуется.
Оценивается работающий результат, миграции, проверки и обоснованность решения.

Проект намеренно компактнее предварительных 3–5 тысяч строк: здесь нет
искусственного увеличения объёма. Сложность создают транзакции, изоляция
клиентов и конкурентная обработка, а не размер репозитория.

## Быстрый запуск без Docker

Нужен Python 3.12. SQLite позволяет сразу начать; финальные проверки
конкурентности выполняются на PostgreSQL.

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Если политика PowerShell мешает активации, используй
`.\.venv\Scripts\python.exe` вместо `python`; менять политику не обязательно.

macOS / Linux:

```bash
source .venv/bin/activate
```

Далее на любой системе:

```bash
python -m pip install -r requirements-lock.txt
python -m alembic upgrade head
python -m scripts.seed --large
python -m uvicorn app.main:app --reload
```

API: http://127.0.0.1:8000/docs

В отдельном терминале с тем же окружением:

```bash
python -m app.worker --once
```

Демонстрационные ключи: `demo-alpha-key`, `demo-beta-key`.
Они работают только в локальной базе, созданной seed-скриптом.
Секреты реальных сервисов и оплата AI-провайдера не нужны.

Для чистого старта используй новый путь в `DATABASE_URL`; seed не перезаписывает
существующие аккаунты. Файл `.env.example` — пример: автоматической загрузки
`.env` нет. Переменные задаются через shell или Compose.

## Запуск с PostgreSQL

Нужны Docker и Docker Compose:

```bash
docker compose up --build -d
docker compose run --rm api python -m scripts.seed --large
```

Compose сам применяет миграции перед запуском API и worker.
Порты доступны только на локальном компьютере.

Подробнее: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Проверки

```bash
python -m pytest tests/test_smoke.py -q
python -m pytest tests/test_acceptance.py -q
python -m pytest -q
```

Исходные smoke/acceptance/PostgreSQL-тесты сохранены без изменений.
Дополнительные регрессии проверяют инварианты баланса, циклы оплаты,
ошибки записи, конкурентные запросы/процессы и сохранение старых данных.

PostgreSQL-тесты запускаются отдельно с `TEST_DATABASE_URL`.
Без этой переменной они пропускаются; это не подтверждение конкурентности.
Полный запуск описан в runbook. CI настроен на полный набор с PostgreSQL.

## Структура

| Путь | Назначение |
|---|---|
| `app/routers/` | HTTP-контракт |
| `app/services/` | Баланс, создание заданий, списки |
| `app/worker.py` | Фоновая обработка |
| `app/provider.py` | Локальная имитация AI-провайдера |
| `app/models.py` | ORM-модели |
| `migrations/` | Alembic и стартовая схема |
| `tests/` | Smoke, acceptance, PostgreSQL |
| `scripts/` | Seed, измерение SQL, упаковка |
| `docs/` | Инциденты, контракт, инструкции и оценка |

Исходное задание сохранено в TASK.md и docs/CONTRACT.md для воспроизводимой оценки.
