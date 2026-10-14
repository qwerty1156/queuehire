# Среда PostgreSQL-проверки

Использован неизменённый PostgreSQL 16.2 из pgserver 0.1.4. Версия получена
через SELECT version(), уровень изоляции — SHOW transaction_isolation.
Финальные pytest, миграции, сравнение строк и EXPLAIN выполнялись на живом
сервере с отдельными psycopg-соединениями и процессами worker.

Среда предоставляет только UID/GID 0. Обычный initdb отказал под root;
смена UID/nested user namespace недоступна. Для локального тестового процесса
использован LD_PRELOAD-адаптер: getuid/geteuid/getgid/getegid отображаются как
65534; владельцы 0 согласованно отображаются в stat/fstat/lstat/fstatat
и их glibc/64-bit вариантах. Kernel credentials остаются UID/GID 0.
Адаптер не используется приложением; PostgreSQL-бинарники, SQL, provider
и исходные acceptance-тесты не менялись.

Окончательная конфигурация PostgreSQL:

```text
listen_addresses = '127.0.0.1'
port = 55432
unix_socket_directories = ''
shared_memory_type = 'sysv'
default_transaction_isolation = 'read committed'
```

TCP использован из-за недоступности Unix-domain sockets. Сервер и тестовый
процесс запускались в одном exec-сеансе; сервер останавливался после проверки.
Тестовый URL: postgresql+psycopg://challenge@127.0.0.1:55432/postgres.

Первоначальный mmap-режим дал несогласованный подсчёт после большого upgrade:
снимок сохранял 1000 строк, отдельный count видел меньше. После перехода на
SysV независимые запросы согласуются: 1000 jobs, 334 failed, 1500 notes.
Полный набор повторён на SysV: 116 passed. Ранние mmap-замеры не используются
как окончательный результат.

На обычном компьютере можно воспроизвести через исходный Docker Compose
или установленный PostgreSQL под непривилегированным пользователем.
Команды pytest/compare_queries/explain_list есть в REPORT.md.
Адаптер, бинарники и рабочий кластер — местные инструменты проверки,
они не включены в submission.zip. Docker здесь отсутствует, сборка образа
и Compose-запуск не проверены.

