# REPORT — QueueHire

Выполнены все шесть пунктов. Окончательный полный прогон: **116 passed, 0 failed,
0 skipped**, с настоящим PostgreSQL 16.2 и SQLite. Внешняя оценка по рубрике
ещё не проводилась.

## Исправления

| Инцидент | Причина | Изменение | Проверка |
|---|---|---|---|
| QH-101 | Поиск только по ID | HTTP-операции ищут (tenant_id, id); чужой/отсутствующий ID одинаково 404 до состояния/баланса | Read/note/retry, все состояния и баланс чужого tenant=0; снимок всех данных неизменен |
| QH-102 | Оплата до replay, глобальный key, нет сравнения payload | Уникальность (tenant_id, key), точное сравнение title/text до оплаты, 201/200/409 | Replay при 0, другой tenant, регистр/пробелы/порядок JSON, terminal job и replay после retry |
| QH-103 | Ранний commit баланса, устаревший ORM-баланс, гонки | Блокировка счёта БД; условное арифметическое UPDATE; job/charge/balance одним commit | 8 одинаковых/разных конкурентных запросов, DB-trigger отказ, ключ после отката, ledger sum=balance |
| QH-104 | Terminal/running обрабатывались заново, ошибки не различались, повторный refund | Захват queued атомарным UPDATE, одна попытка, permanent сразу failed, temporary до MAX_ATTEMPTS; failed/refund атомарны | Два процесса вызывают provider один раз; stale session, активный running, duplicate delivery, одновременные refunds и отказ refund-trigger |
| QH-105 | Фильтр после Python-пагинации, неверный total, N+1 | Общий SQL-фильтр count/page, LIMIT/OFFSET в БД, заданный порядок, summary-проекция и count заметок | 4 SQL включая auth, 0 ORM Job/Note; все статусы, ties, offset за концом, note_count=0, PostgreSQL EXPLAIN |
| QH-106 | Retry отсутствовал | Собственный failed → queued, новый billing_cycle, сохранённый cost, charge и сброс состояния одной транзакцией | 8 retry одного job; разные jobs за последние кредиты; DB-отказ; 3 цикла с отдельными refunds |

## Запуск

Среда: Python 3.12.14, Ubuntu 24.04.3, SQLite, PostgreSQL 16.2,
READ COMMITTED. JOB_COST=3, MAX_ATTEMPTS=3; дополнительно сохранённая цена 7.
Зависимости приложения сохранены в исходном requirements-lock.txt.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m alembic upgrade head
python -m scripts.seed --large
python -m uvicorn app.main:app --reload
```

В другом терминале с теми же переменными: `python -m app.worker --once`
или `python -m app.worker`. Windows-активация описана в README.
Существующая база обновляется с прежним DATABASE_URL через upgrade head,
без удаления базы/volume. Seed 0001 выполняется из исходной версии.

PostgreSQL через исходный Compose: `docker compose up --build -d`;
явное обновление миграций: `docker compose run --rm migrate`.
Docker/Compose здесь не запускались, Docker отсутствует. Приложение, SQL,
миграции и тесты реально проверены без Docker.

## Результаты проверок

| Команда/версия | Фактический результат |
|---|---|
| Исходный архив: python -m pytest -q | 24 passed, 19 failed, 5 PostgreSQL skipped |
| Промежуточный этап 1–3, полный прогон | 67 passed, 7 failed по списку/retry, 12 PostgreSQL skipped |
| Окончательно без TEST_DATABASE_URL: python -m pytest -q --tb=short | **89 passed, 27 skipped**, 0 failed |
| Окончательно с PostgreSQL: та же команда | **116 passed**, 0 failed, 0 skipped, 21.07 с |

Полный прогон с отдельной PostgreSQL test DB:

```bash
export TEST_DATABASE_URL='postgresql+psycopg://challenge:challenge@127.0.0.1:5432/challenge'
python -m pytest -q --tb=short
```

Нужно право CREATE SCHEMA; тесты создают/удаляют только собственные временные
схемы. [Полный протокол](docs/final-validation/tests_postgres.txt).
Одно предупреждение — DeprecationWarning Starlette/AnyIO.

Исходные smoke/acceptance/PostgreSQL-тесты, conftest, schemas, provider и 0001
сохранены байт-в-байт. Дополнительные регрессии проверяют наблюдаемые инварианты,
DB-side ошибки и реальные конкурентные потоки/отдельные процессы. Подмены
analyze для подсчёта вызовов находятся только в tests.
Особенности PostgreSQL-среды описаны в [TEST_ENVIRONMENT.md](docs/TEST_ENVIRONMENT.md).

## Данные и миграции

Цепочка: **0001 → 0002 → 0003**, 0001 не изменена.
0002 заменяет глобальную уникальность на (tenant_id, idempotency_key), добавляет
jobs.billing_cycle и nullable ledger.billing_cycle с уникальностью
(job_id, billing_cycle, kind). 0003 добавляет два составных индекса списка.

Проверены пустые базы и upgrade 0001 с данными на обоих движках. В большом
наборе: 2 tenant, 2 ключа, **1000 jobs, 1500 notes, 2 ledger**. Сравнивались
все исходные поля каждой строки, сохранились ID/тексты/результаты/даты/связи.
Отдельный тест сохраняет старый duplicate refund, отрицательный баланс,
Unicode/пробелы и ненулевой JSON-result.

Alembic check на обновлённых SQLite и PostgreSQL:
**No new upgrade operations detected**.
[PostgreSQL](docs/final-validation/upgrade_postgres.txt),
[SQLite](docs/final-validation/upgrade_sqlite.txt).
Исторический ledger получает NULL в новом поле: старые циклы не угадываются.
Downgrade 0002 отказывается восстанавливать глобальную уникальность при
совпадающих ключах разных компаний вместо потери/переименования jobs.

Сверка старых балансов: сравнить balance с SUM(ledger.amount) по tenant;
разобрать charges/refunds по job и журналам поддержки. Утверждённую
корректировку внести отдельной adjustment и изменением баланса одной
транзакцией, сохранив старые проводки. Миграция не исправляет старые суммы молча.

## Производительность

Одинаковая база до/после, исходный seed --large: 1000 jobs, 1500 notes;
alpha, limit=20, offset=0, без фильтра. Одинаковая расширенная query_probe
инструментация, исходное и исправленное приложение.

| Движок | SQL до → после | ORM Job до → после | Все ORM до → после | Время до → после |
|---|---:|---:|---:|---:|
| SQLite | **23 → 4** | **1000 → 0** | 1032 → 2 | 0.0362 → 0.0291 с |
| PostgreSQL | **23 → 4** | **1000 → 0** | 1032 → 2 | 0.0587 → 0.0357 с |

Два ORM-объекта после — ApiKey/Tenant. SQL: две авторизации, один count,
один summary/count-notes запрос. text/result/notes не загружаются.
Время — измерение этой машины; главный результат — SQL и ORM count.
При status=failed, offset=40 исходная версия давала 6 элементов/total=1000,
исправленная на обоих движках — **20/total=334**. Total дополнительно сверяется
с независимым снимком исходных строк.

Воспроизведение замеров, сохранности всех полей и Alembic check:

```bash
python -m scripts.compare_queries '/path/QueueHire_Backend_Challenge(1).zip'
python -m scripts.compare_queries '/path/QueueHire_Backend_Challenge(1).zip' --postgres-url "$TEST_DATABASE_URL"
```

Команды используют собственные временные базы/схемы. SQL/замеры сохранены
в docs/final-validation/before_* и after_*.

EXPLAIN после ANALYZE, фактические production-запросы:
[без фильтра](docs/final-validation/explain_all_postgres.txt),
[failed](docs/final-validation/explain_failed_postgres.txt).

- Без фильтра: Limit → Index Scan Backward using ix_jobs_tenant_created_id,
  actual rows=20, Execution Time=0.126 ms.
- Failed: Limit → Index Scan Backward using ix_jobs_tenant_status_created_id,
  actual rows=20, Execution Time=0.137 ms.
- Notes: Bitmap Index Scan по ix_notes_job_id и Bitmap Heap Scan, loops=20.
- Count: Seq Scan на небольшом наборе; failed оставляет 334 и исключает 666.
  Выбор индекса не принуждается.

(tenant_id, created_at, id) поддерживает порядок страницы с tenant-фильтром;
(tenant_id, status, created_at, id) — порядок с фильтром состояния и префикс
для count. Обратный scan даёт DESC по датам/ID. Notes(job_id) нужен для count.
Отдельное воспроизведение на seeded PostgreSQL DATABASE_URL:

```bash
python -m scripts.explain_list --limit 20
python -m scripts.explain_list --status failed --limit 20
```

## Решения и ограничения

Создание блокирует счёт SQL UPDATE balance=balance; оплату выполняет
UPDATE balance=balance-cost WHERE balance>=cost; возврат — balance+cost.
Устаревший ORM-баланс не используется. Цена — сериализация создания одного
tenant; PostgreSQL блокирует строку, SQLite — запись базы.

Worker/retry арбитраж выполняют условным UPDATE в БД, без Python Lock.
Worker фиксирует running перед analyze и job/refund одним commit.
Retry принимает только failed, увеличивает billing_cycle, сбрасывает
attempts/error/result и списывает сохранённую стоимость тем же commit.
Новые charge/refund имеют номер цикла и ограничение уникальности.

Принудительная смерть после running оставляет job running. Lease/reaper и
crash recovery не реализованы, внешнее exactly-once не гарантируется.
Отказ финальной записи/refund откатывает финальную транзакцию, оставляя running.
Неожиданная ошибка провайдера вне двух объявленных типов также требует ручного
разбора и сверки внешнего вызова/ledger.

Count/page списка — два SELECT при READ COMMITTED; отдельная гарантия общего
snapshot при одновременных изменениях не добавлена. Фильтрация, total и пустые
страницы проверены на фиксированных данных.

## Процесс работы

Активное время на момент подготовки отчёта: **примерно 61 минут в двух этапах**.
Первый ≈21 мин; остальное — завершение и PostgreSQL-проверки. Включены чтение,
подготовка среды, реализация, выполнение/разбор проверок; ожидание между этапами
не включено.

Инструменты: ChatGPT/Codex текущего сеанса, Python/shell, SQLAlchemy, Alembic,
pytest, FastAPI TestClient; pgserver предоставил настоящие PostgreSQL-бинарники.
Дополнительные зависимости тестовой среды не добавлены в приложение.

Выдачи: 1 промежуточный ZIP, 1 полная сдача; внешней оценки пока нет.
Всего 11 запусков pytest в двух этапах, включая baseline и повтор после
смены shared-memory режима.

Ключевые решения: держать идемпотентность под блокировкой оплаты; передавать
владение через условный UPDATE; разделять новые ledger-циклы, сохраняя историю;
проецировать summary и пагинацию в SQL.

