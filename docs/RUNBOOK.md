# Запуск и проверка

Команды выполняются из корня распакованного проекта.
Python 3.12; зависимости зафиксированы в requirements-lock.txt.

## Локальный SQLite

```bash
python -m pip install -r requirements-lock.txt
python -m alembic upgrade head
python -m scripts.seed --large
python -m uvicorn app.main:app --reload
```

Default DATABASE_URL — sqlite:///./queuehire.db.
Если переменная уже задана в системе, она имеет приоритет.
Для второго терминала нужно активировать то же виртуальное окружение
и установить те же переменные.

Документация API: http://127.0.0.1:8000/docs.
Нажми на конкретный endpoint → Try it out → введи X-API-Key.
POST создания также требует Idempotency-Key.

Пример payload:

```json
{"title":"Python Developer","text":"Python FastAPI PostgreSQL Docker"}
```

Обработать queued jobs одним проходом:

```bash
python -m app.worker --once
```

Непрерывный worker:

```bash
python -m app.worker
```

При `[TEMPORARY]` нужно три прохода, чтобы исчерпать три попытки.
При `[FAIL_ONCE]` второй проход успешен. При `[PERMANENT]` failure и возврат
стоимости происходят после первого прохода. Terminal/running job не обрабатывается
повторно; кнопка retry создаёт новый оплачиваемый цикл того же ID.

## PostgreSQL через Docker

```bash
docker compose up --build -d
docker compose run --rm api python -m scripts.seed --large
docker compose logs api worker
```

Остановить процессы без удаления данных:

```bash
docker compose down
```

Миграции запускает сервис migrate, затем стартуют API и worker.
Повторная сборка после изменения исходников:

```bash
docker compose up --build -d
```

Если migrate уже завершился в старом запуске, явно применить новые миграции:

```bash
docker compose run --rm migrate
```

## Тесты

Быстрый локальный запуск:

```bash
python -m pytest tests/test_smoke.py -q
python -m pytest tests/test_acceptance.py -q
python -m pytest -q
```

SQLite-тесты создают отдельные временные базы. Они не используют
queuehire.db и не меняют демонстрационные аккаунты.

Финальная проверка PostgreSQL:

```bash
docker compose run --rm -e TEST_DATABASE_URL=postgresql+psycopg://challenge:challenge@db:5432/challenge api python -m pytest -q
```

PostgreSQL fixture создаёт уникальную схему qh_test_<random>, применяет в неё
миграции, проводит проверку и удаляет только эту схему.
Учётная запись должна иметь право CREATE SCHEMA в тестовой базе.
Используй отдельную тестовую базу, например локальную базу из Compose.
При принудительном прерывании теста временная схема может остаться.

Для установленного PostgreSQL без Docker:

Linux/macOS:

```bash
export TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@localhost:5432/TEST_DB'
python -m pytest -q
```

PowerShell:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@localhost:5432/TEST_DB'
python -m pytest -q
```

Это шаблон адреса, USER/PASSWORD/TEST_DB заменяются твоими тестовыми значениями.
Пароли с особыми символами нужно кодировать для URL.

## Замер списка

На отдельной базе со seed --large:

```bash
python -m scripts.query_probe --limit 20
```

Скрипт покажет статус HTTP, elapsed time, число SQL, число загруженных объектов
Job/Note, количество элементов и total.
SQL выводятся без значений параметров. Время — локальный замер, не SLO.
Нужно сравнивать на одной машине с одинаковыми данными и размером страницы.

При PostgreSQL можно задать DATABASE_URL при запуске команды в shell
или вызвать query_probe через docker compose run --rm api.
EXPLAIN фактических SQL списка/счётчика (PostgreSQL, база после seed):

```bash
python -m scripts.explain_list --limit 20
python -m scripts.explain_list --status failed --limit 20
```

Воспроизводимое сравнение с исходным архивом, включая сохранность всех полей
1000 jobs и 1500 notes при upgrade 0001, и Alembic check:

```bash
python -m scripts.compare_queries '/path/QueueHire_Backend_Challenge(1).zip'
python -m scripts.compare_queries '/path/QueueHire_Backend_Challenge(1).zip' --postgres-url "$TEST_DATABASE_URL"
```

Первая команда использует временную SQLite-базу, вторая — собственную временную
схему в указанной PostgreSQL test DB. Итоговые протоколы записываются в
docs/final-validation. Исходный архив распаковывается в отдельную временную
папку; существующие базы/схемы/исходники не очищаются.

## Проверка обновления существующей базы

Автоматизированная проверка старых
данных (SQLite и, при TEST_DATABASE_URL, PostgreSQL):

```bash
python -m pytest tests/test_stage1_migrations.py -q
```

Следующие команды создания/seed схемы 0001 выполняются **из исходной версии**
архива. Новые ORM-модели рассчитаны на head и не предназначены для seed 0001.
После seed вернись в текущую версию, сохрани тот же DATABASE_URL и примени head.

Создай отдельную базу и примени только стартовую схему:

```bash
python -m alembic upgrade 0001
python -m scripts.seed --large
```

Сохрани число rows и несколько ID/полей job, note и ledger,
затем примени свои новые миграции:

```bash
python -m alembic upgrade head
```

Сверь сохранённые данные и запусти новые API-операции на этой базе.
В финальной сдаче приложи автоматизированную или воспроизводимую проверку.

## Упаковка

Создай REPORT.md и запусти:

```bash
python -m scripts.package_submission
```

Полученный submission.zip содержит разрешённые папки исходников,
конфигурацию, зависимости и отчёт. В README проекта отражай новые
зависимости и изменённые инструкции запуска.
