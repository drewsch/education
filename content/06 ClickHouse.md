---
domain: подготовка к собеседованиям
tags:
  - собеседование/clickhouse
  - бд
status: to-review
order: 6
---
# ClickHouse — полный конспект

> Связанные темы: [[05 PostgreSQL]] (сравнение OLTP/OLAP, планы запросов) · [[01 Linux — ядро, ресурсы, метрики]] (диск, page cache, CPU) · [[02 Сеть — пропускная способность, фаерволы, узкие места]] · [[08 Kafka]] (загрузка из Kafka)

## Содержание

1. OLTP vs OLAP: зачем ClickHouse
2. Колоночное хранение и векторное выполнение
3. MergeTree: части, гранулы, засечки, разреженный первичный индекс
4. ORDER BY, PRIMARY KEY, PARTITION BY — как выбирать
5. Вставка: батчи, too many parts, async insert
6. Слияния (merges)
7. Семейство движков MergeTree: Replacing, Summing, Aggregating, Collapsing
8. Индексы пропуска данных, проекции
9. Типы данных и кодеки сжатия
10. Изменение и удаление данных: мутации, lightweight delete, TTL
11. Выполнение запроса, EXPLAIN, параллелизм, память
12. JOIN в ClickHouse
13. Materialized views и словари
14. Репликация, ClickHouse Keeper, шардирование, Distributed
15. Сеть и ClickHouse
16. Метрики и системные таблицы
17. Поиск узких мест — методика
18. Шпаргалка симптом → причина → решение
19. Вопросы на собеседовании

***

## 1. OLTP vs OLAP

| | OLTP (PostgreSQL) | OLAP (ClickHouse) |
|---|---|---|
| Типичный запрос | «заказ №42», «обнови статус» | «сумма продаж по регионам за год», «p99 latency по сервисам за сутки» |
| Строк на запрос | единицы–сотни | миллионы–миллиарды |
| Колонок на запрос | все колонки строки | 3–10 из сотен |
| Запись | одиночные INSERT/UPDATE, транзакции | **большие пачки** вставок, обновления редки и дороги |
| Хранение | строками (heap, страницы 8 КБ) | **колонками**, сжато |
| Конкурентность | тысячи коротких запросов/с | десятки–сотни тяжёлых запросов/с |
| Транзакции | ACID | нет многооператорных транзакций (атомарна вставка одного блока) |
| Индексы | B-tree на конкретные строки | разреженный индекс по диапазонам |

Где ClickHouse в стеке НТ: хранение метрик и логов (результаты тестов, трейсы OpenTelemetry, логи приложений), аналитика по результатам прогонов.

***

## 2. Колоночное хранение и векторное выполнение

### 2.1 Колонки

```
Строковое хранение (PostgreSQL):
  [id=1, ts=..., service='api', latency=12, status=200, url='/orders', ...]  ← одна строка подряд
  [id=2, ts=..., service='db',  latency=3,  status=200, url='/users',  ...]

Колоночное хранение (ClickHouse):
  ts.bin:      [ts1, ts2, ts3, ...]
  service.bin: ['api','db','api', ...]
  latency.bin: [12, 3, 45, ...]
  status.bin:  [200, 200, 500, ...]
```

Запрос `SELECT service, quantile(0.99)(latency) FROM requests WHERE ts > ... GROUP BY service` читает **только 3 колонки** из, скажем, 50. Это в ~15 раз меньше I/O, чем строковая БД.

**Сжатие:** в колонке однотипные и часто похожие данные (статусы 200, 200, 200, монотонные временные метки) → сжатие в 5–20 раз. Меньше байт с диска → быстрее.

### 2.2 Векторное выполнение

Данные обрабатываются **блоками** (по умолчанию до 65 536 строк, `max_block_size`) — массивами значений колонки, а не построчно. Цикл по массиву чисел компилятор превращает в SIMD-инструкции процессора (обработка 4–16 значений за такт). Отсюда миллиарды строк в секунду на сервер.

Аналогия с Java: разница между `for (Row r : rows) sum += r.latency` (виртуальные вызовы, объекты) и суммированием примитивного `long[]` — JIT векторизует второе.

***

## 3. MergeTree: части, гранулы, засечки

### 3.1 Части (parts)

Каждая вставка создаёт на диске **новую часть (part)** — отдельный каталог с файлами колонок, отсортированными по ключу `ORDER BY`. Части неизменяемы. Фоновые **слияния** объединяют маленькие части в большие.

```
/var/lib/clickhouse/data/db/requests/
  202510_1_1_0/        ← партиция 202510, блоки 1..1, уровень слияния 0 (свежая вставка)
  202510_2_2_0/
  202510_1_2_1/        ← результат слияния частей 1 и 2 (уровень 1); старые удалятся
  202510_3_10_2/
```

Имя части: `партиция_минБлок_максБлок_уровень[_версияМутации]`.

Форматы частей: **Wide** (каждая колонка — свой файл, для больших частей) и **Compact** (все колонки в одном файле, для маленьких частей — меньше файлов).

### 3.2 Гранулы и засечки

Внутри части строки отсортированы по `ORDER BY` и разбиты на **гранулы** — по `index_granularity = 8192` строк (и не больше `index_granularity_bytes = 10 МБ`).

```
Часть: 1 000 000 строк → 123 гранулы по 8192 строки

primary.idx (разреженный индекс): значение ключа ПЕРВОЙ строки каждой гранулы
  гранула 0:   (service='api',  ts='2025-10-01 00:00:00')
  гранула 1:   (service='api',  ts='2025-10-01 03:12:45')
  ...
  гранула 57:  (service='db',   ts='2025-10-01 00:00:00')
  ...

latency.mrk2 (засечки, marks): для каждой гранулы — где в сжатом файле latency.bin
             начинается её блок и смещение внутри распакованного блока
```

**Разреженный индекс:** не на каждую строку (как B-tree в PostgreSQL), а на каждую гранулу. Индекс миллиарда строк — ~120 тыс. записей, он **целиком в памяти**.

**Как идёт поиск:**

```
WHERE service = 'db' AND ts BETWEEN '2025-10-01 10:00' AND '2025-10-01 11:00'

1. Двоичный поиск по primary.idx → какие гранулы МОГУТ содержать нужные строки (например, 60–62)
2. По засечкам находим смещения этих гранул в файлах нужных колонок
3. Читаем и распаковываем только эти гранулы (3 × 8192 строк = 24 576 строк)
4. Фильтруем строки внутри гранул
```

Минимальная единица чтения — **гранула**. Даже для поиска одной строки читается минимум 8192 строки нужных колонок. Поэтому ClickHouse **плохо подходит для точечных запросов** по одной строке с высоким RPS.

***

## 4. ORDER BY, PRIMARY KEY, PARTITION BY

```sql
CREATE TABLE requests
(
    ts          DateTime64(3),
    service     LowCardinality(String),
    endpoint    LowCardinality(String),
    status      UInt16,
    latency_ms  Float32,
    trace_id    String,
    attrs       Map(String, String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (service, endpoint, ts)
TTL toDateTime(ts) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;
```

### 4.1 ORDER BY (ключ сортировки)

- определяет физический порядок строк в части → **как будут сжиматься** данные и **какие запросы пропускают гранулы**;
- по умолчанию PRIMARY KEY = ORDER BY;
- **порядок колонок:** от низкой кардинальности к высокой и по частоте использования в фильтрах: `(service, endpoint, ts)`. Фильтр `service = 'api'` использует индекс; фильтр только по `ts` — частично (ClickHouse умеет «generic exclusion search», но эффективность хуже); фильтр по `trace_id` (нет в ключе) — **полный скан** колонки.
- Высококардинальная колонка (UUID, trace_id) первой в ключе делает остальные колонки ключа бесполезными для фильтрации и ухудшает сжатие.

Это аналог **корреляции** в PostgreSQL ([[05 PostgreSQL]], раздел 6), только здесь порядок задаётся явно и поддерживается всегда.

### 4.2 PRIMARY KEY отдельно от ORDER BY

`PRIMARY KEY` может быть **префиксом** `ORDER BY` — тогда в памяти индекс меньше, а сортировка по полному ключу остаётся (нужно для Summing/Replacing движков).

### 4.3 PARTITION BY

- партиция — логическая группа частей; слияния происходят **только внутри партиции**;
- зачем: удаление старых данных `ALTER TABLE ... DROP PARTITION 202501` мгновенно; отсечение партиций по условию;
- **не** инструмент ускорения запросов в первую очередь — для этого ORDER BY;
- типично — по месяцу (`toYYYYMM`) или дню для очень больших объёмов;
- **слишком мелкие партиции** (по часу, по `user_id`) → тысячи партиций × части в каждой → «too many parts», медленные слияния, много файлов. Рекомендация: партиций сотни, не десятки тысяч.

***

## 5. Вставка

### 5.1 Правило: большими пачками

Каждый INSERT = новая часть = новые файлы на диске + будущая работа для слияний.

```
Плохо:  10 000 INSERT/с по 1 строке → 10 000 частей в секунду
Хорошо: 1 INSERT/с по 10 000–1 000 000 строк
Рекомендация: пачки от 10–100 тыс. строк, не чаще ~1 вставки в секунду на таблицу
```

**Ошибка «Too many parts»:**

```
DB::Exception: Too many parts (300). Merges are processing significantly slower than inserts
```

- `parts_to_delay_insert` (по умолчанию 1000 в новых версиях; раньше 150) — при превышении числа активных частей в партиции вставки **искусственно замедляются**;
- `parts_to_throw_insert` (3000; раньше 300) — вставки **отклоняются**.

На НТ это видно как рост latency вставки, затем ошибки. Метрики: `system.parts` (число активных частей по партициям), `MaxPartCountForPartition` в `system.asynchronous_metrics`.

### 5.2 Как батчить

1. **Буфер на стороне приложения** — копить в памяти и вставлять пачкой по размеру/времени.
2. **Async insert** (сервер сам копит мелкие вставки):
   ```sql
   SET async_insert = 1, wait_for_async_insert = 1;  -- ждать записи (надёжно) или 0 (быстрее, риск потери)
   -- async_insert_busy_timeout_ms, async_insert_max_data_size — когда сбрасывать буфер
   ```
3. **Kafka engine / ClickPipes / Vector / OpenTelemetry Collector** — читают поток и вставляют пачками ([[08 Kafka]]).
4. Таблица **Buffer** — устаревший способ, сейчас предпочитают async insert.

### 5.3 Формат и протокол вставки

- **Native-протокол** (порт 9000) или HTTP с форматом `RowBinary`/`Native` — эффективнее, чем `VALUES` и `JSONEachRow` (текст нужно разбирать).
- Сжатие на клиенте (LZ4) — меньше сетевого трафика.
- Вставка в **Distributed** таблицу → сервер-приёмник раскладывает данные по шардам (лишний сетевой хоп и асинхронная очередь); при большой нагрузке лучше вставлять в локальные таблицы шардов напрямую (раздел 14).

### 5.4 Дедупликация вставок

Для Replicated-таблиц ClickHouse запоминает хэши последних вставленных блоков (`replicated_deduplication_window = 1000`): повтор той же пачки (ретрай после таймаута) не создаст дубль. Поэтому **ретраи вставки должны отправлять ту же пачку с тем же разбиением**.

***

## 6. Слияния (merges)

Фоновые потоки объединяют мелкие части в крупные (сортировкой слиянием — данные в частях уже отсортированы).

Зачем:

- меньше частей → меньше файлов и быстрее запросы (каждый запрос открывает каждую подходящую часть);
- для Replacing/Summing/Aggregating движков — именно при слиянии происходит схлопывание строк.

Стоимость:

- CPU (распаковка, сортировка слиянием, сжатие) и **I/O** (данные перечитываются и переписываются несколько раз — write amplification);
- объём записи на диск на порядок больше объёма вставки.

```sql
-- Что сливается сейчас
SELECT database, table, elapsed, progress, num_parts, total_size_bytes_compressed, memory_usage
FROM system.merges;

-- Число активных частей по партициям
SELECT table, partition, count() AS parts, sum(rows) AS rows,
       formatReadableSize(sum(bytes_on_disk)) AS size
FROM system.parts WHERE active
GROUP BY table, partition ORDER BY parts DESC LIMIT 20;
```

Параметры: `background_pool_size` (потоки слияний, по умолчанию 16), `max_bytes_to_merge_at_max_space_in_pool` (максимальный размер слияния, 150 ГБ).

`OPTIMIZE TABLE t FINAL` — принудительное слияние всех частей партиции в одну. Очень дорого, не использовать как регулярную операцию под нагрузкой.

***

## 7. Семейство MergeTree

| Движок | Что делает при слиянии | Когда |
|--------|-----------------------|-------|
| `MergeTree` | ничего особенного | обычные события/логи |
| `ReplacingMergeTree(ver)` | оставляет **последнюю** версию строки с одинаковым ORDER BY | «обновляемые» сущности (состояние заказа) |
| `SummingMergeTree` | суммирует числовые колонки строк с одинаковым ключом | счётчики, предагрегация |
| `AggregatingMergeTree` | сливает **состояния** агрегатных функций (`AggregateFunction`) | инкрементальные агрегаты через MV |
| `CollapsingMergeTree(sign)` / `VersionedCollapsing` | схлопывает пары строк `sign=1` / `sign=-1` | изменение данных через «отмену» |

**Главная ловушка:** схлопывание происходит **только при слиянии**, которое случается когда-нибудь и не обязательно сводит всё в одну часть. Поэтому запрос может увидеть **дубли/несхлопнутые строки**:

```sql
SELECT * FROM orders_state WHERE order_id = 42;          -- может вернуть 3 версии
SELECT * FROM orders_state FINAL WHERE order_id = 42;     -- схлопывает на лету — дороже
SELECT argMax(status, version) FROM orders_state WHERE order_id = 42;  -- агрегацией
```

`FINAL` раньше был очень дорогим (однопоточное слияние в запросе); в новых версиях он параллелится и оптимизирован, но всё равно добавляет работу — проверять на НТ.

***

## 8. Индексы пропуска данных и проекции

### 8.1 Data skipping indexes

Для колонок вне ORDER BY. Хранят сводку по блоку из `GRANULARITY N` гранул и позволяют **пропустить** блок, если в нём точно нет нужного значения.

```sql
ALTER TABLE requests ADD INDEX idx_status status TYPE minmax GRANULARITY 4;
ALTER TABLE requests ADD INDEX idx_trace trace_id TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE requests ADD INDEX idx_url endpoint TYPE set(100) GRANULARITY 4;
ALTER TABLE requests MATERIALIZE INDEX idx_trace;   -- построить для уже существующих данных
```

| Тип | Хранит | Хорош для |
|-----|--------|-----------|
| `minmax` | мин. и макс. значение в блоке | колонки, коррелированные с ORDER BY (время, растущие ID) |
| `set(N)` | множество значений (до N) | мало различных значений в блоке |
| `bloom_filter` | вероятностный фильтр | поиск по `=`/`IN` для высококардинальных колонок (trace_id) |
| `tokenbf_v1`, `ngrambf_v1` | bloom по токенам/n-граммам | поиск в логах по словам/подстрокам |

**Важно:** это не B-tree. Если искомое значение встречается в каждом блоке (случайное распределение), индекс ничего не пропустит, а время на его проверку добавится. Работают только когда значения «кучкуются».

### 8.2 Проекции

Проекция — дополнительная копия данных части, отсортированная по-другому или предагрегированная; ClickHouse сам выбирает её для подходящих запросов.

```sql
ALTER TABLE requests ADD PROJECTION by_trace (SELECT * ORDER BY trace_id);
ALTER TABLE requests MATERIALIZE PROJECTION by_trace;
-- теперь WHERE trace_id = '...' читает проекцию с индексом по trace_id
```

Цена: место на диске и работа при вставке и слиянии.

***

## 9. Типы данных и кодеки

### 9.1 Типы

- Использовать **минимальные** типы: `UInt16` для статуса, `UInt32` для ID до 4 млрд, `Float32` где хватает точности, `DateTime` (секунды) вместо `DateTime64`, если миллисекунды не нужны.
- **`LowCardinality(String)`** — словарное кодирование для строк с небольшим числом значений (до ~10 тыс.: сервис, эндпоинт, страна, статус). Хранит номера вместо строк → меньше места и быстрее GROUP BY/фильтры.
- **`Nullable`** — отдельная колонка-маска NULL, лишняя работа. Предпочитать значения по умолчанию.
- `Enum8/16`, `Map`, `Array`, `Tuple`, `JSON` (новый тип с динамическими подколонками).

### 9.2 Кодеки

```sql
ts         DateTime64(3) CODEC(Delta, ZSTD(1)),     -- растущее время: хранить разницу
counter    UInt64        CODEC(DoubleDelta, LZ4),   -- монотонный счётчик
value      Float64       CODEC(Gorilla, LZ4),       -- метрики с плавным изменением
message    String        CODEC(ZSTD(3))             -- текст логов
```

| Кодек | Идея | Для |
|-------|------|-----|
| `LZ4` (по умолчанию) | быстрое сжатие | общее, когда важна скорость |
| `ZSTD(level)` | лучше сжатие, дороже CPU | холодные данные, текст |
| `Delta` | хранить разность с предыдущим | монотонные значения |
| `DoubleDelta` | разность разностей | равномерный шаг (временные ряды) |
| `Gorilla` | XOR соседних float | метрики |
| `T64` | обрезка неиспользуемых бит целых | небольшие целые |

```sql
-- Степень сжатия по колонкам
SELECT name, type,
       formatReadableSize(data_compressed_bytes)   AS compressed,
       formatReadableSize(data_uncompressed_bytes) AS uncompressed,
       round(data_uncompressed_bytes / data_compressed_bytes, 1) AS ratio
FROM system.columns WHERE table = 'requests' ORDER BY data_compressed_bytes DESC;
```

***

## 10. Изменение и удаление данных

| Способ | Как работает | Цена |
|--------|-------------|------|
| `ALTER TABLE ... UPDATE/DELETE WHERE` (мутация) | **асинхронно переписывает все затронутые части целиком** | очень дорого: изменение одной строки переписывает часть на гигабайты; очередь мутаций (`system.mutations`) |
| `DELETE FROM t WHERE ...` (lightweight delete) | помечает строки скрытой маской `_row_exists`, физически удаляются при слияниях | быстро, но запросы фильтруют маску; много удалений — деградация |
| `ALTER TABLE ... DROP PARTITION` | удаляет каталог партиции | мгновенно |
| `TTL` | удаление/перемещение данных старше срока при слияниях | фоновая работа |
| ReplacingMergeTree / Collapsing | «обновление» вставкой новой версии | дешёвая вставка, схлопывание при слияниях, FINAL при чтении |

**Вывод:** ClickHouse — не для частых обновлений строк. Мутации под нагрузкой забивают I/O и очередь слияний.

```sql
SELECT database, table, mutation_id, command, create_time, parts_to_do, is_done, latest_fail_reason
FROM system.mutations WHERE NOT is_done;
```

***

## 11. Выполнение запроса, EXPLAIN, параллелизм, память

### 11.1 Как выполняется SELECT

```
1. Анализ, выбор частей (по партициям) и гранул (по primary key, skip-индексам)
2. Чтение нужных колонок нужных гранул: max_threads потоков (по умолчанию = число ядер)
   параллельно читают разные диапазоны гранул
3. PREWHERE: сначала читаются колонки условия, фильтруются строки,
   и только для подходящих читаются остальные колонки (ClickHouse делает это сам)
4. Агрегация в каждом потоке → слияние результатов
5. Сортировка, LIMIT, отдача клиенту
```

**Один запрос использует все ядра.** Поэтому 10 тяжёлых параллельных запросов на 32 ядрах конкурируют за CPU — у каждого фактически ~3 ядра. Пропускная способность по числу тяжёлых запросов ограничена; лимиты `max_concurrent_queries` (по умолчанию 100), `max_threads` на пользователя/профиль, квоты.

### 11.2 EXPLAIN

```sql
EXPLAIN indexes = 1
SELECT service, count() FROM requests
WHERE service = 'api' AND ts >= now() - INTERVAL 1 HOUR GROUP BY service;
--   ReadFromMergeTree (default.requests)
--   Indexes:
--     MinMax     Keys: ts         Parts: 3/120   Granules: 9800/1200000
--     Partition  Keys: toYYYYMM(ts)  Parts: 3/3
--     PrimaryKey Keys: service, ts   Granules: 48/9800        ← отлично: прочитаем 48 гранул
--     Skip       Name: idx_status ...

EXPLAIN PIPELINE ...   -- конвейер обработчиков и число потоков
EXPLAIN PLAN actions = 1 ...
```

Главное число — **сколько гранул из скольких** будет прочитано. Если `Granules: 1200000/1200000` — условие не использует ключ, полный скан.

После выполнения — в `system.query_log`: `read_rows`, `read_bytes`, `result_rows`, `memory_usage`, `query_duration_ms`, `ProfileEvents` (подробные счётчики: `SelectedMarks`, `ReadCompressedBytes`, `OSReadBytes` — с диска, `OSReadChars` — включая page cache).

### 11.3 Память

ClickHouse держит в памяти хэш-таблицы GROUP BY, правые части JOIN, буферы сортировки.

| Настройка | Смысл |
|-----------|-------|
| `max_memory_usage` | лимит памяти на запрос (по умолчанию 10 ГБ в профиле default) → `Memory limit (for query) exceeded` |
| `max_memory_usage_for_user` | на все запросы пользователя |
| `max_server_memory_usage` / `max_server_memory_usage_to_ram_ratio` | на весь сервер (по умолчанию 0.9 RAM) |
| `max_bytes_before_external_group_by` | после этого GROUP BY сбрасывается на диск |
| `max_bytes_before_external_sort` | то же для ORDER BY |

GROUP BY по высококардинальному ключу (`trace_id`, `user_id` при миллиардах) — главный пожиратель памяти. Решения: внешняя агрегация, приближённые функции (`uniq` вместо `uniqExact`, `quantile` (приближённый, на выборке) вместо `quantileExact`), предагрегация в MV.

### 11.4 Кэши

- **Mark cache** — засечки в памяти (`mark_cache_size`, 5 ГБ). Промахи → чтение `.mrk` с диска на каждый запрос.
- **Uncompressed cache** — распакованные блоки (по умолчанию выключен для запросов; полезен при частых коротких запросах к одним данным).
- **Query cache** (новые версии) — результаты запросов.
- **Page cache ОС** — основной кэш сжатых данных; ClickHouse активно на него полагается.

***

## 12. JOIN в ClickHouse

### 12.1 Как работает по умолчанию

ClickHouse по умолчанию делает **hash join**: **правая** таблица целиком загружается в память в хэш-таблицу, левая читается потоком.

```sql
-- Правильно: большая слева, маленькая справа
SELECT r.endpoint, s.owner, count()
FROM requests AS r                    -- миллиарды строк — левая (поток)
JOIN services AS s ON s.name = r.service   -- сотни строк — правая (в память)
GROUP BY r.endpoint, s.owner;
```

Если поставить большую таблицу справа — `Memory limit exceeded` или очень медленно. **В отличие от PostgreSQL, планировщик исторически не переставлял таблицы** (в новых версиях есть автоматическая перестановка для простых случаев — но полагаться на это не стоит).

### 12.2 Алгоритмы (`join_algorithm`)

| Алгоритм | Как | Когда |
|----------|-----|-------|
| `hash` | правая таблица в хэш-таблицу | правая влезает в память |
| `parallel_hash` | хэш-таблица разбита по потокам | быстрее при большой правой, больше памяти |
| `grace_hash` | разбивает обе стороны на корзины, сбрасывает на диск | правая не влезает в память |
| `partial_merge` / `full_sorting_merge` | сортировка и слияние | огромные таблицы, мало памяти; обе стороны отсортированы по ключу |
| `direct` | поиск в правой таблице по ключу напрямую | правая — словарь или Join/EmbeddedRocksDB-движок |
| `auto` | начинает с hash, переключается на merge при нехватке памяти | — |

### 12.3 Альтернативы JOIN

- **Словари** (`dictGet`) — справочник в памяти, поиск O(1), обновляется по расписанию (раздел 13.2). Для обогащения фактов справочниками это в разы быстрее JOIN.
- **`IN (подзапрос)`** вместо JOIN, когда нужны только фильтрация.
- **Денормализация** — хранить нужные поля сразу в широкой таблице: в ClickHouse это нормальная практика.

### 12.4 JOIN и IN в распределённых запросах

На Distributed-таблицах подзапрос справа выполняется **на каждом шарде**:

```
SELECT ... FROM dist_requests WHERE user_id IN (SELECT user_id FROM dist_users WHERE ...)
→ каждый из N шардов сам выполнит подзапрос по dist_users, а тот разойдётся по N шардам
→ N² запросов
GLOBAL IN / GLOBAL JOIN → подзапрос выполняется один раз на инициаторе,
  результат пересылается всем шардам по сети (сеть! размер результата!)
```

***

## 13. Materialized views и словари

### 13.1 Materialized View в ClickHouse — это триггер на вставку

В отличие от PostgreSQL ([[05 PostgreSQL]], раздел 19.2), MV в ClickHouse **не пересчитывает** весь запрос. Это **триггер**: при каждом INSERT в исходную таблицу запрос MV выполняется над **вставляемым блоком**, результат пишется в целевую таблицу.

```sql
CREATE TABLE requests_1m
(
    minute   DateTime,
    service  LowCardinality(String),
    cnt      AggregateFunction(count),
    p99      AggregateFunction(quantile(0.99), Float32)
)
ENGINE = AggregatingMergeTree ORDER BY (service, minute);

CREATE MATERIALIZED VIEW requests_1m_mv TO requests_1m AS
SELECT toStartOfMinute(ts) AS minute, service,
       countState() AS cnt, quantileState(0.99)(latency_ms) AS p99
FROM requests GROUP BY minute, service;

-- Чтение: «доагрегировать» состояния
SELECT minute, service, countMerge(cnt), quantileMerge(0.99)(p99)
FROM requests_1m WHERE minute > now() - INTERVAL 1 DAY GROUP BY minute, service;
```

Следствия:

- данные, вставленные до создания MV, в неё не попадут (нужен ручной backfill);
- каждая MV **замедляет вставку** в исходную таблицу (она выполняется синхронно в INSERT);
- ошибка в MV → ошибка INSERT в исходную таблицу.

### 13.2 Словари

```sql
CREATE DICTIONARY services_dict
(name String, owner String, tier UInt8)
PRIMARY KEY name
SOURCE(POSTGRESQL(host 'pg' port 5432 user 'ro' password '...' db 'cmdb' table 'services'))
LAYOUT(HASHED())
LIFETIME(MIN 300 MAX 600);   -- обновлять раз в 5–10 минут

SELECT dictGet('services_dict', 'owner', service) AS owner, count()
FROM requests GROUP BY owner;
```

Источник — PostgreSQL, MySQL, HTTP, файл, ClickHouse-таблица. Обновление словаря = периодический запрос к источнику ([[05 PostgreSQL]] получит нагрузку).

***

## 14. Репликация, Keeper, шардирование

### 14.1 Репликация

```sql
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/requests', '{replica}')
```

- Реплики равноправны (**мультимастер**): писать можно в любую.
- Координация через **ClickHouse Keeper** (или ZooKeeper): журнал операций (какие части вставлены, какие слить), очереди задач реплик.
- Реплика, получившая вставку, пишет часть локально и регистрирует её в Keeper; остальные **скачивают часть** с неё по сети (interserver HTTP, порт 9009).
- Слияния назначаются через журнал и выполняются каждой репликой самостоятельно (или одна сливает, остальные скачивают — `always_fetch_merged_part`).
- По умолчанию вставка подтверждается после записи на **одну** реплику; `insert_quorum = 2` — ждать подтверждения от двух (дольше, надёжнее).

**Keeper — частое узкое место:** каждая вставка в Replicated-таблицу = несколько операций в Keeper. Много мелких вставок × много таблиц → Keeper не успевает; потеря кворума Keeper → таблицы становятся **read-only** (`Table is in readonly mode`).

```sql
SELECT database, table, is_readonly, absolute_delay, queue_size, inserts_in_queue, merges_in_queue
FROM system.replicas;

SELECT * FROM system.replication_queue ORDER BY create_time LIMIT 20;  -- что застряло
```

### 14.2 Шардирование и Distributed

```sql
-- На каждом шарде — локальная таблица requests_local (ReplicatedMergeTree)
-- Поверх — «зонтичная» таблица без данных:
CREATE TABLE requests_dist AS requests_local
ENGINE = Distributed('cluster_name', 'db', 'requests_local', cityHash64(trace_id));
```

**Чтение через Distributed:**

```
Клиент → узел-инициатор → запрос уходит на одну реплику каждого шарда
       ← каждый шард возвращает ЧАСТИЧНЫЙ результат (состояния агрегатов)
       → инициатор сливает и отдаёт клиенту
```

**Вставка через Distributed:** инициатор раскладывает строки по шардам по ключу шардирования и **асинхронно** отправляет (очередь на диске) — при проблемах сети очередь копится (`system.distribution_queue`); `insert_distributed_sync = 1` — синхронно.

**Ключ шардирования:** равномерный (`cityHash64(trace_id)`, `rand()`) или по сущности, чтобы связанные данные были на одном шарде (тогда JOIN/GROUP BY по этой сущности локальны — `distributed_group_by_no_merge`, `optimize_skip_unused_shards`).

***

## 15. Сеть и ClickHouse

### 15.1 Порты

| Порт | Назначение |
|------|-----------|
| 8123 | HTTP-интерфейс (curl, JDBC через HTTP, Grafana-плагин) |
| 9000 | Native TCP (clickhouse-client, native-драйверы, межсерверные распределённые запросы) |
| 9009 | Interserver HTTP — **репликация** (скачивание частей между репликами) |
| 9181 / 2181 | ClickHouse Keeper / ZooKeeper клиентский порт |
| 9234 | Keeper Raft (между узлами Keeper) |
| 9363 | встроенный Prometheus endpoint (если включён) |
| 8443 / 9440 | HTTPS / Native TLS |

Фаерволы между узлами кластера должны пропускать 9000, 9009 и порты Keeper. Ошибка в правиле для 9009 → вставки проходят, но реплики не синхронизируются (растёт `absolute_delay`, `queue_size`).

### 15.2 Сетевая нагрузка

- **Репликация:** каждая вставленная часть копируется на остальные реплики **по сети**, а результаты слияний — тоже (если реплики скачивают слитые части). Вставка 200 МБ/с сжатых данных при 2 репликах → +200 МБ/с трафика между репликами плюс трафик слияний.
- **Distributed SELECT:** шарды отправляют инициатору промежуточные результаты. `GROUP BY` по высококардинальному ключу → каждый шард отправляет миллионы групп → инициатор упирается в сеть и память. `GLOBAL IN/JOIN` → пересылка таблицы всем шардам.
- **Результаты клиенту:** `SELECT *` миллиарда строк по HTTP в `JSON` — сеть и сериализация. Использовать `Native`/`RowBinary`, сжатие (`enable_http_compression=1`, заголовок `Accept-Encoding`), агрегировать на сервере.
- **Сжатие межсерверного трафика:** данные передаются сжатыми блоками (`network_compression_method = lz4`, можно `zstd` при узкой сети).

### 15.3 Соединения и таймауты

- HTTP-интерфейс: keep-alive (`keep_alive_timeout` = 10 с по умолчанию в новых версиях; если балансировщик держит соединение дольше — редкие обрывы).
- `max_connections` (4096), `max_concurrent_queries`.
- Таймауты: `connect_timeout`, `receive_timeout`, `send_timeout` (300 с), для распределённых — `connect_timeout_with_failover_ms`.
- Долгие запросы за балансировщиком с idle timeout 60 с обрываются, если сервер не шлёт данные: `send_progress_in_http_headers=1` держит соединение «живым».

***

## 16. Метрики и системные таблицы

### 16.1 Системные таблицы

| Таблица | Что |
|---------|-----|
| `system.query_log` | все запросы: длительность, прочитано строк/байт, память, ошибки, ProfileEvents |
| `system.processes` | выполняющиеся сейчас запросы (`KILL QUERY WHERE query_id = ...`) |
| `system.parts` | части таблиц: число, размер, уровень |
| `system.merges` | идущие слияния |
| `system.mutations` | мутации |
| `system.replicas`, `system.replication_queue` | состояние репликации |
| `system.distribution_queue` | очередь асинхронных вставок Distributed |
| `system.metrics` | текущие значения (запросы, соединения, потоки) |
| `system.events` | накопительные счётчики |
| `system.asynchronous_metrics` | периодически вычисляемые: память, `MaxPartCountForPartition`, нагрузка CPU, диски |
| `system.part_log` | история создания/слияния/удаления частей |
| `system.errors` | счётчики ошибок по кодам |

### 16.2 Полезные запросы

```sql
-- Топ запросов по суммарному времени за час
SELECT normalized_query_hash, any(query) AS q, count() AS calls,
       round(avg(query_duration_ms)) AS avg_ms,
       quantile(0.99)(query_duration_ms) AS p99_ms,
       formatReadableSize(avg(read_bytes)) AS avg_read,
       formatReadableSize(max(memory_usage)) AS max_mem
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 1 HOUR
GROUP BY normalized_query_hash ORDER BY sum(query_duration_ms) DESC LIMIT 20;

-- Ошибки запросов
SELECT exception_code, any(exception), count()
FROM system.query_log WHERE type IN ('ExceptionBeforeStart','ExceptionWhileProcessing')
  AND event_time > now() - INTERVAL 1 HOUR
GROUP BY exception_code ORDER BY count() DESC;

-- Сколько частей создаётся в минуту (признак мелких вставок)
SELECT toStartOfMinute(event_time) AS m, table, countIf(event_type = 'NewPart') AS new_parts,
       countIf(event_type = 'MergeParts') AS merges
FROM system.part_log WHERE event_time > now() - INTERVAL 1 HOUR
GROUP BY m, table ORDER BY m;

-- Отношение прочитанного с диска к общему чтению (page cache)
SELECT sum(ProfileEvents['OSReadBytes']) AS from_disk, sum(ProfileEvents['OSReadChars']) AS total_read
FROM system.query_log WHERE event_time > now() - INTERVAL 1 HOUR AND type = 'QueryFinish';
```

### 16.3 Метрики для Prometheus (`/metrics` на порту 9363 или exporter)

| Метрика | Что смотреть |
|---------|--------------|
| `ClickHouseMetrics_Query` | одновременные запросы |
| `ClickHouseProfileEvents_Query`, `_FailedQuery`, `_InsertQuery` (rate) | RPS, ошибки |
| `ClickHouseProfileEvents_InsertedRows`, `_InsertedBytes` | скорость вставки |
| `ClickHouseAsyncMetrics_MaxPartCountForPartition` | **число частей** — главный индикатор проблем вставки |
| `ClickHouseMetrics_BackgroundMergesAndMutationsPoolTask` | загрузка пула слияний |
| `ClickHouseProfileEvents_DelayedInserts`, `_RejectedInserts` | вставки замедлены/отклонены (too many parts) |
| `ClickHouseMetrics_ReadonlyReplica` | реплики в read-only (Keeper) |
| `ClickHouseAsyncMetrics_ReplicasMaxAbsoluteDelay` | лаг репликации |
| `ClickHouseMetrics_MemoryTracking` | память сервера |
| `ClickHouseProfileEvents_SelectedMarks`, `_SelectedRows` | объём чтения |
| `ClickHouseMetrics_DistributedFilesToInsert` | очередь Distributed-вставок |
| `ClickHouseMetrics_ZooKeeperRequest`, `_ZooKeeperSession` | нагрузка на Keeper |

***

## 17. Поиск узких мест — методика

```
1. Вставка или чтение?
   Вставка: DelayedInserts/RejectedInserts, MaxPartCountForPartition, часть в минуту (part_log),
            очередь Distributed, Keeper (latency, сессии), MV на таблице (каждая тормозит INSERT)
   Чтение:  query_log — какие запросы занимают суммарно больше всего времени

2. Для медленного запроса
   EXPLAIN indexes=1 → сколько гранул читаем; если почти все — условие не по ключу
   read_rows/result_rows огромное → нужен другой ORDER BY, skip-индекс, проекция, MV
   memory_usage большой → GROUP BY по высокой кардинальности / большой правый JOIN
   OSReadBytes большой → данные не в page cache, диск

3. Ресурсы сервера — [[01 Linux — ядро, ресурсы, метрики]]
   CPU: запросы используют все ядра — сколько их одновременно? слияния съедают CPU?
   Диск: слияния и мутации (write amplification), iostat, PSI io
   Память: MemoryTracking против лимитов, OOM
   Сеть: репликация (9009), распределённые запросы, выдача результатов

4. Фон
   system.merges, system.mutations, replication_queue — не забита ли очередь
```

***

## 18. Шпаргалка: симптом → причина → решение

| Симптом | Причина | Решение |
|---------|---------|---------|
| `Too many parts`, вставки замедляются | мелкие частые INSERT, мелкие партиции | батчи 10–100 тыс.+ строк, async_insert, партиция по месяцу |
| Запрос читает все гранулы | фильтр не по префиксу ORDER BY | пересмотреть ORDER BY, skip-индекс, проекция |
| `Memory limit exceeded` на JOIN | большая таблица справа | маленькую справа, словарь, grace_hash |
| `Memory limit exceeded` на GROUP BY | высокая кардинальность | внешняя агрегация, приближённые функции, предагрегация MV |
| Дубли в ReplacingMergeTree | слияние ещё не произошло | FINAL, argMax, GROUP BY |
| Диск постоянно занят записью | слияния / мутации | крупнее вставки, отказаться от частых ALTER UPDATE |
| `Table is in readonly mode` | потеря связи с Keeper | состояние Keeper, сеть, нагрузка на Keeper |
| Растёт лаг реплик | порт 9009 закрыт / сеть / очередь слияний | фаервол, сеть, replication_queue |
| Распределённый запрос медленный при быстрых шардах | инициатор сливает большие промежуточные результаты, GLOBAL JOIN | агрегировать ближе к шардам, ключ шардирования, сеть |
| Вставки «теряются» при Distributed | асинхронная очередь не доставлена | distribution_queue, insert_distributed_sync |
| Точечные запросы по ID медленные при высоком RPS | гранула 8192 строки, нет B-tree | это не OLTP — кэш/другая БД, bloom_filter, проекция |
| Разовые мутации висят часами | ALTER UPDATE переписывает части | lightweight delete, Replacing-движок, DROP PARTITION |

***

## 19. Вопросы на собеседовании

1. Чем OLAP отличается от OLTP? Почему колоночное хранение быстрее для аналитики?
2. Что такое часть, гранула, засечка? Почему индекс в ClickHouse разреженный?
3. Как выбрать ORDER BY? Почему высококардинальная колонка первой — плохо?
4. Чем PARTITION BY отличается от ORDER BY? Почему мелкие партиции вредны?
5. Почему вставлять нужно большими пачками? Что такое «Too many parts»? Что такое async insert?
6. Что такое слияния и сколько они стоят?
7. Чем ReplacingMergeTree отличается от MergeTree? Почему в нём бывают дубли? Что делает FINAL?
8. Какие бывают data skipping indexes? Когда они бесполезны?
9. Зачем LowCardinality и кодеки Delta/DoubleDelta/Gorilla?
10. Почему UPDATE/DELETE в ClickHouse дорогие? Какие альтернативы?
11. Как работает JOIN в ClickHouse и почему важен порядок таблиц? Что такое GLOBAL JOIN?
12. Чем materialized view в ClickHouse отличается от PostgreSQL?
13. Как устроена репликация и зачем Keeper? Что будет при потере кворума Keeper?
14. Как работает Distributed-таблица при чтении и при вставке?
15. Какая сетевая нагрузка возникает в кластере ClickHouse и какие порты нужны?
16. Как найти медленный запрос и понять, почему он медленный?
