---
domain: подготовка к собеседованиям
tags:
  - собеседование/jvm
status: to-review
order: 10
---
# JVM — Конспект для собеседования

> Связанные темы: [[01 Linux — ядро, ресурсы, метрики]] · [[02 Сеть — пропускная способность, фаерволы, узкие места]] · [[11 Go]] и [[12 Python]] (сравнение рантаймов) · [[13 Kubernetes, OpenShift, Docker]] (CPU limits и JVM) · [[05 PostgreSQL]] (HikariCP)

> Полный конспект по JVM, GC, профилированию и диагностике для Senior Performance Engineer.

***

## 1. Структура памяти JVM

```
JVM Memory
├── Heap (управляется GC)
│   ├── Young Generation
│   │   ├── Eden Space       ← новые объекты создаются здесь
│   │   ├── Survivor S0      ← пережившие Minor GC
│   │   └── Survivor S1      ← пережившие Minor GC
│   └── Old Generation       ← долгоживущие объекты (age > threshold)
│
└── Non-Heap
    ├── Metaspace            ← метаданные классов (с Java 8, вместо PermGen)
    ├── Code Cache           ← JIT скомпилированный нативный код
    ├── Thread Stacks        ← стек каждого потока
    └── Direct Memory        ← NIO буферы (ByteBuffer.allocateDirect)

```

### Что где хранится

```
Eden Space:
  new Object() → всегда сюда
  Большинство объектов умирают здесь же (short-lived)

Survivor S0/S1:
  Объекты пережившие Minor GC
  Перекладываются между S0↔S1 при каждом Minor GC
  age увеличивается на 1 при каждом копировании

Old Generation:
  age > tenuring threshold (дефолт 15)
  Большие объекты (Humongous в G1) — сразу сюда
  Долгоживущие: кэши, пулы соединений, static поля

Metaspace:
  Метаданные классов, bytecode методов
  Растёт динамически (в отличие от PermGen с фиксированным лимитом)
  ClassLoader leak → Metaspace растёт без остановки

Code Cache:
  JIT компилирует горячий bytecode в нативный код → хранится здесь
  Если переполнится → JIT отключается → деградация к интерпретации

Thread Stack:
  Каждый поток имеет свой стек (~512 КБ - 2 МБ)
  Хранит: локальные переменные, параметры методов, return address
  1000 потоков × 1 МБ = 1 ГБ только на стеки!
  StackOverflowError = стек переполнен (бесконечная рекурсия)

```

***

## 2. Аллокация объектов

Аллокация — выделение памяти под новый объект.

```
Object obj = new Object();        // аллокация ~16 байт в Eden
byte[] arr = new byte[3_000_000]; // 3 МБ → Humongous в G1

```

TLAB (Thread Local Allocation Buffer) — каждый поток имеет свой буфер в Eden. Аллокация = просто сдвиг указателя. Очень быстро, без синхронизации между потоками.

GC давление (GC pressure) — много аллокаций → Eden быстро заполняется → частые Minor GC → GC занимает CPU → меньше времени на полезную работу.

***

## 3. GC — основы

### Stop-The-World (STW)

Пауза когда все потоки приложения остановлены. HTTP запросы не обрабатываются, ничего не происходит — JVM занята GC.

```
Запрос пришёл:     10:00:00.000
STW началась:      10:00:00.050 → всё стоп
STW кончилась:     10:00:00.250 → продолжили
Ответ отправлен:   10:00:00.260

Latency = 260ms вместо 10ms — из-за GC паузы

```

Почему нельзя GC без STW: если поток создаст новую ссылку пока GC уже проверил объект — GC не знает о ней и может удалить живой объект. Даже ZGC не обходится без коротких STW пауз.

### Minor GC (Young Collection)

Срабатывает когда Eden заполнен. Копирующий алгоритм:

```
Eden:  [A][B][C][D][E][ пусто ]  ← заполнен
S0:    [X(age=1)][Y(age=1)]
S1:    [ пусто ]

GC Roots → помечаем живые: A, C, E
Копируем: A(age=1), C(age=1), E(age=1), X(age=2), Y(age=2) → S1
Eden и S0 → полностью очищены

```

Быстро (несколько ms) — нет фрагментации, живые объекты компактно копируются.

### Major/Full GC

Срабатывает когда Old Generation заполнен. Mark-Sweep-Compact:

```
Mark:    помечаем все живые объекты в Old
Sweep:   мёртвые помечаем как свободные
Compact: сдвигаем живые объекты в начало, обновляем все ссылки

```

Пауза: сотни ms — секунды. Old Generation большой, нужно обновить все ссылки.

### Humongous объекты (G1GC)

Объект > 50% размера региона G1:

```
G1HeapRegionSize = 4 МБ
byte[] arr = new byte[3_000_000]; // 3 МБ > 2 МБ → Humongous

```

Проблемы: выделяются сразу в Old, не перемещаются при обычном GC, короткоживущие Humongous → Old заполняется → Full GC.

Решение: `-XX:G1HeapRegionSize=16m` → порог Humongous = 8 МБ.

***

## 4. GC алгоритмы

### G1GC — дефолт Java 9+

Делит heap на равные регионы (1-32 МБ). Регионы помечаются динамически как Eden/Survivor/Old/Humongous/Free.

Фазы:

```
1. Young Collection (Minor GC) — STW 10-200ms
   Очищает Eden и Survivor регионы

2. Concurrent Marking — параллельно с приложением
   Initial Mark (STW < 1ms)    → помечаем GC Roots
   Concurrent Mark (без STW)   → обходим граф объектов
   Remark (STW < 10ms)         → финализируем маркировку
   Cleanup                     → считаем % мусора в регионах

3. Mixed GC — STW
   Очищает Young + Old регионы с наибольшим % мусора
   "Garbage First" = сначала регионы с max мусором

4. Full GC — аварийный режим (избегать!)
   STW, однопоточный, весь heap
   Происходит если аллокация быстрее маркировки

```

G1MixedGCCountTarget — на сколько Mixed GC разбить очистку Old:

```
Old: 100 регионов, MixedGCCountTarget=8:
  8 Mixed GC × 12 регионов = маленькие паузы, их больше

MixedGCCountTarget=2:
  2 Mixed GC × 50 регионов = паузы больше, их меньше

```

Как узнать что аллокация быстрее маркировки:

```
В GC логах: [Full GC (Allocation Failure)]
В jstat: O (Old) растёт при каждом цикле и не падает, FGC растёт

Решение:
  -XX:InitiatingHeapOccupancyPercent=35  (начать маркировку раньше)
  -XX:ConcGCThreads=6                    (больше потоков на маркировку)
  -Xmx16g                                (больше места)

```

### ZGC — Java 15+ production ready

Паузы < 1ms независимо от размера heap.

Colored Pointers — метаданные состояния хранятся в самом указателе:

```
[unused | Marked0 | Marked1 | Remapped | адрес объекта]

```

Load Barriers — при каждом чтении ссылки JVM проверяет и при необходимости обновляет указатель (self-healing).

Фазы ZGC:

```
Pause Mark Start     STW < 1ms   помечаем GC Roots
Concurrent Mark      без STW     обходим граф, load barriers помогают
Pause Mark End       STW < 1ms   финализируем маркировку
Concurrent Prepare   без STW     выбираем регионы, строим forwarding tables
Pause Relocate Start STW < 1ms   перемещаем Root-объекты
Concurrent Relocate  без STW     перемещаем всё остальное

Суммарный STW: 3 паузы × <1ms = <3ms

```

Self-healing:

```
GC переместил Object A: 0x1000 → 0x5000
Поток читает старый указатель 0x1000:
  Load barrier → forwarding table → обновляет на 0x5000
  Следующее чтение уже без barrier overhead

```

Минус: CPU overhead \~15-20% на load barriers и concurrent потоки.

SoftMaxHeapSize — мягкий лимит:

```
-Xmx32g -XX:SoftMaxHeapSize=28g

Нормальная нагрузка: heap 20 ГБ → ZGC расслаблен
Пик: heap растёт → пересёк 28 ГБ → ZGC агрессивнее собирает
     если не успевает → heap растёт до Xmx=32g
     если превысил → OOM

Зачем буфер 4 ГБ: в K8s memory limit=32Gi
  heap Xmx=32g → non-heap (Metaspace, Code Cache, stacks) не влезает
  → OOM killer убьёт контейнер
  SoftMaxHeapSize=28g → 4 ГБ буфер для non-heap → безопасно

```

### Сравнение GC

```
GC           Паузы        Throughput   Когда использовать
─────────────────────────────────────────────────────────
Serial       Секунды      Низкий       Маленькие приложения, 1 ядро
Parallel     Сотни ms     Высокий      Batch, throughput важнее latency
G1           10-200ms     Высокий      Большинство продакшен приложений
ZGC          < 1ms        Средний      Latency-critical, heap > 8 ГБ
Shenandoah   < 10ms       Средний      Альтернатива ZGC в OpenJDK

```

***

## 5. JIT компилятор и Code Cache

Два уровня компиляции:

```
Интерпретация (медленно): bytecode → JVM выполняет каждую инструкцию

C1 (уровень 1-3, быстрая компиляция):
  Порог: ~2000 вызовов
  Компилирует быстро, базовые оптимизации
  Быстрее интерпретации, профилирует метод

C2 (уровень 4, оптимизирующая компиляция):
  Порог: ~10000 вызовов
  Компилирует медленно, агрессивные оптимизации:
  inlining, loop unrolling, escape analysis, vectorization
  Максимальная скорость нативного кода

Прогрев (1-5 мин после старта): C1/C2 активно работают → CPU высокий → норма
После прогрева: методы скомпилированы → C1/C2 почти не активны

```

Code Cache:

```
-XX:ReservedCodeCacheSize=256m  # дефолт 240 МБ

Симптом заполнения: "CodeCache is full. Compiler has been disabled."
→ JIT отключается → деградация к интерпретации
→ throughput падает, CPU снижается

```

Деоптимизация:

```
C2 скомпилировал с предположением "метод всегда получает String"
Вдруг пришёл Integer → предположение нарушено
→ made not entrant → откат → повторная компиляция

Симптом в PrintCompilation: много "made not entrant" на одном методе
Причины: полиморфизм в hot path, instanceof в цикле

```

Escape Analysis:

```
void process() {
    Point p = new Point(1, 2); // p не "убегает" из метода
    return p.x + p.y;
}
// C2: Point не убегает → поля в стеке (Stack Allocation)
//     или объект исключается (Scalar Replacement)
// → нет аллокации в heap → нет GC давления

```

***

## 6. ClassLoader и Metaspace leak

ClassLoader загружает байткод классов в JVM. Класс выгружается только когда его ClassLoader становится недостижимым.

ClassLoader Leak возникает при горячем редеплое (без перезапуска сервера):

```
WebAppClassLoader_1 загружает 500 классов v1
Редеплой → WebAppClassLoader_2 загружает 500 классов v2
CL_1 должен умереть → классы выгрузиться

НО если где-то ссылка на CL_1:
  ThreadLocal в пуле потоков держит объект загруженный CL_1
  static Map держит объект CL_1
  JDBC Driver зарегистрирован через CL_1

→ CL_1 не умирает → 500 классов в Metaspace навсегда
→ 10 редеплоев = 5000 классов → OOM: Metaspace

```

Типичные источники:

```
// ThreadLocal без очистки — самая частая причина
static ThreadLocal<MyObject> tl = new ThreadLocal<>();
tl.set(new MyObject()); // поток в пул → ThreadLocal жив → CL жив

// Фикс: ОБЯЗАТЕЛЬНО
try {
    tl.set(new MyObject());
    // работа
} finally {
    tl.remove();
}

```

Диагностика:

```
jstat -gcutil <pid> 1000
# M (Metaspace) растёт при каждом редеплое и не падает

# Heap dump → MAT → SELECT * FROM java.lang.ClassLoader
# → много CL объектов → leak

```

***

## 7. Thread dump

### Как снять

```
jstack <pid> > /tmp/td_$(date +%s).txt    # основной способ
jstack -F <pid>                            # принудительно (процесс завис)
kill -3 <pid>                              # печатает в stdout
curl localhost:8080/actuator/threaddump    # Spring Actuator

# Снимать 3 дампа с интервалом 10 сек — видишь паттерн, не момент

```

### Структура записи потока

```
"http-nio-8080-exec-23" #105 daemon prio=5 os_tid=0x00007f...
   java.lang.Thread.State: BLOCKED (on object monitor)
        at com.example.OrderService.processOrder(OrderService.java:87)
        - waiting to lock <0x00000006c2a4f310> (a com.example.OrderService)
        - locked <0x00000006c1b3e220> (a com.example.Cache)

"exec-23"         → имя потока
os_tid=0x00007f   → ID потока в OS (для корреляции с top -H)
BLOCKED           → состояние
waiting to lock   → хочет захватить монитор
locked            → уже держит этот монитор

```

### os\_tid — для поиска CPU-грузящего потока

```
top -H -p <pid>          # смотришь какой PID грузит CPU
printf "%x\n" 12345      # конвертируем в hex → 3039
grep "3039" dump.txt     # находим поток в дампе

```

### Состояния потоков

```
RUNNABLE       → выполняется (может быть в native IO — не значит что грузит CPU)
BLOCKED        → ждёт synchronized монитор
WAITING        → Object.wait() или LockSupport.park() — ждёт бесконечно
TIMED_WAITING  → Thread.sleep() или wait(timeout) — ждёт с таймаутом

```

### Что искать

```
1. Deadlock — JVM пишет в конце дампа:
   Found one Java-level deadlock:
   "Thread-A": waiting to lock <0x123> held by "Thread-B"
   "Thread-B": waiting to lock <0x456> held by "Thread-A"

2. Много BLOCKED на одном мониторе:
   exec-1..exec-40: BLOCKED waiting to lock <0x123>
   exec-7: RUNNABLE - locked <0x123>
     at com.mysql.jdbc.Connection.query(...)  ← медленный DB запрос!
   Решение: убрать synchronized / оптимизировать запрос

3. Поток завис в native (timeout не настроен):
   exec-5: RUNNABLE
     at java.net.SocketInputStream.socketRead0(Native Method)

4. WAITING — норма для потоков пула, ждут задачи

```

***

## 8. Диагностика утечек памяти

### Алгоритм

```
Full GC часто, паузы большие
         ↓
jstat -gcutil <pid> 1000
Old (O) не уменьшается после GC?
├── Не уменьшается → утечка
└── Уменьшается → heap мал → увеличить Xmx
         ↓
jmap -histo <pid> | head -30  (быстро, без STW)
Смотришь что растёт по количеству
         ↓
jmap -dump:live,format=b,file=/tmp/heap.hprof <pid>
         ↓
Eclipse MAT:
  Dominator Tree → кто занимает retained heap
  Leak Suspects → автоматический анализ

```

### Типичные утечки

```
// 1. Static коллекция без удаления
static Map<String, Object> cache = new HashMap<>();
// только put, никогда remove → бесконечный рост

// 2. ThreadLocal без remove()
threadLocal.set(largeObject);
// поток вернулся в пул → ThreadLocal жив → объект жив

// 3. Listeners без unsubscribe
eventBus.subscribe(this);
// объект не нужен, но eventBus держит ссылку → не собирается GC

// 4. Кэш без eviction — используй Caffeine/Guava Cache с размером

```

***

## 9. Инструменты диагностики

### jstat — мониторинг GC

```
jstat -gcutil <pid> 1000  # каждую секунду

# S0  S1    E     O     M    YGC  YGCT  FGC  FGCT
# 0.0 45.2  72.3  95.1  85.2 142  8.5   12   42.3

# O (Old) постоянно растёт → утечка или heap мал
# FGC > 0 → Full GC был → проблема
# M (Metaspace) растёт при редеплоях → ClassLoader leak

```

### jmap — анализ heap

```
jmap -histo <pid> | head -30      # гистограмма (быстро, без STW)
jmap -heap <pid>                   # статистика heap (JDK 8; в 9+ → jhsdb jmap --heap --pid / jcmd GC.heap_info)
jmap -dump:format=b,file=/tmp/heap.hprof <pid>       # полный dump
jmap -dump:live,format=b,file=/tmp/heap.hprof <pid>  # только живые (STW!)

```

### jstack — thread dump

```
jstack <pid> > /tmp/td.txt
grep "java.lang.Thread.State" td.txt | sort | uniq -c | sort -rn
grep -A 5 "BLOCKED" td.txt
grep -A 20 "Found.*deadlock" td.txt

```

### PrintSafepointStatistics

```
-XX:+PrintSafepointStatistics
-XX:PrintSafepointStatisticsCount=1

# ⚠️ JDK 8. В JDK 9+ флаг удалён → -Xlog:safepoint* (см. раздел 14)
# Логирует каждую STW паузу — не только GC:
# RevokeBias        → снятие biased locking
# Deoptimize        → деоптимизация скомпилированного кода
# GetAllStackTraces → кто-то вызвал jstack!
# ICBufferFull      → inline cache переполнен

# wait_to_block большой → поток долго не доходит до safepoint
# Причина: tight loop без safepoint poll или native код

```

### PrintCompilation

```
-XX:+PrintCompilation

# 1234  42  3  com.example.Service::process (156 bytes)  → C1
# 1235  42  4  com.example.Service::process (156 bytes)  → C2
# 1236  42  3  made not entrant  Service::process        → деоптимизация!

# Уровни: 1-3 = C1, 4 = C2 (максимум)
# made not entrant = метод инвалидирован → деоптимизация

```

### Итого инструменты

```
Инструмент          Когда использовать
──────────────────────────────────────────────────────────
jstack              CPU высокий / latency / deadlock
jmap -histo         Что занимает память (быстро, без STW)
jmap -dump          Утечка памяти → анализ в MAT
jstat -gcutil       Мониторинг GC в реальном времени
Eclipse MAT         Анализ heap dump, поиск утечки
VisualVM/JConsole   GUI диагностика

```

***

## 10. Все важные JVM параметры

### Память

```
-Xms4g              # начальный heap (ставь = Xmx → нет resize под нагрузкой)
-Xmx8g              # максимальный heap (правило: live set × 3)
-Xss512k            # стек потока (снижай при 1000+ потоков)
-XX:MetaspaceSize=256m       # начальный Metaspace
-XX:MaxMetaspaceSize=512m    # максимум (без лимита → OOM при ClassLoader leak)
-XX:ReservedCodeCacheSize=256m # Code Cache для JIT

```

### Выбор GC

```
-XX:+UseG1GC         # G1 — дефолт Java 9+
-XX:+UseZGC          # ZGC — паузы < 1ms
-XX:+UseParallelGC   # Parallel — batch, throughput
-XX:+UseShenandoahGC # Shenandoah — альтернатива ZGC

```

### G1GC параметры

```
-XX:MaxGCPauseMillis=200
# STW пауза в ms — мягкий hint
# G1 подбирает сколько регионов очищать чтобы уложиться
# Если ставишь 50ms → G1 чаще собирает меньше регионов

-XX:G1HeapRegionSize=4m
# Размер региона: 1-32 МБ, степень двойки
# Объект > 50% региона → Humongous (проблема!)
# Много больших объектов → увеличь до 8-16m

-XX:InitiatingHeapOccupancyPercent=45
# При каком % Old начинать Concurrent Marking
# Если Full GC → снизь до 35 (начинать маркировку раньше)
# Слишком низкий → маркировка слишком частая, CPU overhead

-XX:G1NewSizePercent=20
# Минимум % heap под Young регионы (сколько регионов пометить Eden)
# Дефолт 5% — мало для high-throughput
# Больше → реже Minor GC, больше места для аллокаций

-XX:G1MaxNewSizePercent=60
# Максимум % heap под Young регионы

-XX:G1MixedGCCountTarget=8
# На сколько Mixed GC разбить очистку Old регионов
# Больше → пауза каждого меньше, их больше
# Меньше → пауза каждого больше, их меньше

-XX:ConcGCThreads=4
# Потоков для Concurrent Marking
# Если marking не успевает → увеличь

-XX:ParallelGCThreads=8
# Потоков для STW фаз (Minor GC, Mixed GC)

```

### ZGC параметры

```
-XX:SoftMaxHeapSize=28g
# Мягкий лимит: ZGC старается держать heap ниже
# При Xmx=32g: SoftMaxHeapSize=28g → 4 ГБ буфер для non-heap
# В K8s: защита от OOM killer

-XX:ZCollectionInterval=5
# Принудительный GC каждые N сек при отсутствии давления

-XX:ConcGCThreads=4
# Потоков для concurrent фаз (marking, relocation)

-XX:+ZUncommit
# Возвращать неиспользуемую память OS (важно в K8s)

-XX:ZUncommitDelay=300
# Задержка перед возвратом памяти OS (секунды)

```

### Логи и диагностика

```
# GC логи — ОБЯЗАТЕЛЬНО в продакшене
-Xlog:gc*:file=/logs/gc.log:time,uptime,level,tags:filecount=10,filesize=50m

# Heap dump при OOM — ОБЯЗАТЕЛЬНО в продакшене
-XX:+HeapDumpOnOutOfMemoryError
-XX:HeapDumpPath=/tmp/heap.hprof

# Safepoint статистика
-XX:+PrintSafepointStatistics
-XX:PrintSafepointStatisticsCount=1

# JIT компиляция (временно для диагностики)
-XX:+PrintCompilation

# Все JVM флаги с текущими значениями
-XX:+PrintFlagsFinal

```

### Производительность JIT

```
-XX:+TieredCompilation          # дефолт Java 8+ (C1→C2)
-XX:CompileThreshold=10000      # порог вызовов для C2
-XX:+UseStringDeduplication     # только G1, снижает heap на 10-25%
-XX:+OptimizeStringConcat       # дефолт Java 9+

```

***

## 11. Типовые конфигурации

### Latency-critical (финтех, биржа)

```
-Xms4g -Xmx4g
-XX:+UseZGC
-XX:SoftMaxHeapSize=3500m
-XX:ConcGCThreads=2
-XX:+HeapDumpOnOutOfMemoryError
-XX:HeapDumpPath=/tmp/heap.hprof
-Xlog:gc*:file=/logs/gc.log:time,uptime:filecount=5,filesize=20m

```

### Высокий throughput (batch)

```
-Xms8g -Xmx8g
-XX:+UseG1GC
-XX:MaxGCPauseMillis=500
-XX:G1HeapRegionSize=8m
-XX:InitiatingHeapOccupancyPercent=35
-XX:ConcGCThreads=4
-XX:ParallelGCThreads=8
-XX:+HeapDumpOnOutOfMemoryError
-Xlog:gc*:file=/logs/gc.log:time,uptime:filecount=10,filesize=50m

```

***

## 12. Алгоритм диагностики

```
Симптом                     Первый шаг              Инструмент
──────────────────────────────────────────────────────────────────────
CPU 100%                    Какой поток?             top -H -p <pid>
                            os_tid → jstack          grep hex в дампе

Latency выросла,            Thread dump              jstack × 3, интервал 10s
CPU низкий                  BLOCKED на чём?          Много BLOCKED = lock contention

Full GC часто               GC логи                  jstat -gcutil
                            Old не уменьшается?      heap dump → MAT

OOM: Java heap space        Heap dump                jmap -dump:live
                            Что растёт?              MAT → Dominator Tree

OOM: Metaspace              ClassLoader leak?        jmap -histo | grep ClassLoader
                            Редеплои без рестарта?   Много CL объектов → leak

Паузы не от GC              Safepoint лог            PrintSafepointStatistics
                            wait_to_block большой?   Tight loop без safepoint poll

Throughput упал             Code Cache полон?        Логи: "CodeCache is full"
                            JIT отключился?          -XX:ReservedCodeCacheSize=512m

```

***

## 13. Быстрая шпаргалка для собеса

```
Вопрос                              Ответ
────────────────────────────────────────────────────────────────────
Что такое STW                       Все потоки остановлены пока GC работает
                                    Latency = время обработки + STW пауза

Почему нельзя GC без STW            Поток создаст ссылку пока GC уже
                                    проверил объект → GC удалит живой объект

Minor vs Full GC                    Minor = Young, быстро (ms)
                                    Full = весь heap, медленно (сотни ms - сек)

G1 vs ZGC                           G1: паузы 10-200ms, высокий throughput
                                    ZGC: паузы <1ms, CPU overhead +15-20%

Humongous объект                    >50% размера региона G1
                                    В Old, не перемещается → Full GC
                                    Решение: увеличить G1HeapRegionSize

Full GC (Allocation Failure)        Аллокация быстрее маркировки
                                    Снизить IHOP / увеличить ConcGCThreads

Old постоянно растёт                Утечка памяти
                                    jmap -dump → MAT → Dominator Tree

Metaspace растёт                    ClassLoader leak
                                    ThreadLocal без remove() при редеплое

Code Cache full                     JIT отключился → деградация
                                    -XX:ReservedCodeCacheSize=512m

made not entrant в PrintCompilation Деоптимизация — полиморфизм в hot path

Time To Safepoint высокий           Поток в tight loop или native коде

C1/C2 грузят CPU постоянно          Норма при прогреве (1-5 мин)
                                    После прогрева = деоптимизации

SoftMaxHeapSize (ZGC)               Мягкий лимит — буфер для non-heap
                                    В K8s: Xmx=32g, Soft=28g → 4 ГБ буфер

```

***

## 14. Современные инструменты: jcmd, JFR, async-profiler, NMT

> ⚠️ Часть инструментов из разделов 9–10 устарела в современных JDK:
> - `-XX:+PrintSafepointStatistics` удалён в JDK 9+ → использовать `-Xlog:safepoint*=info` (JDK 9+), в JDK 17+ ещё `-Xlog:safepoint+stats`.
> - `jmap -heap` удалён в JDK 9+ → `jhsdb jmap --heap --pid <pid>` или `jcmd <pid> GC.heap_info`.
> - Biased locking (событие `RevokeBias` в safepoint-логах) удалён в JDK 18 — в новых JDK этих пауз нет.
> - `-XX:+PrintGCDetails` → `-Xlog:gc*`.

### 14.1 jcmd — один инструмент вместо многих

```bash
jcmd                                   # список JVM-процессов
jcmd <pid> VM.flags                    # фактические флаги
jcmd <pid> VM.system_properties
jcmd <pid> GC.heap_info                # состояние кучи
jcmd <pid> GC.class_histogram | head   # аналог jmap -histo
jcmd <pid> GC.heap_dump /tmp/heap.hprof
jcmd <pid> Thread.print                # thread dump (аналог jstack)
jcmd <pid> Thread.dump_to_file -format=json /tmp/threads.json   # JDK 21: включая виртуальные потоки
jcmd <pid> VM.native_memory summary    # Native Memory Tracking (нужен -XX:NativeMemoryTracking=summary)
jcmd <pid> JFR.start duration=120s filename=/tmp/rec.jfr settings=profile
jcmd <pid> Compiler.codecache          # заполненность Code Cache
```

### 14.2 JFR (Java Flight Recorder)

Встроенный в JVM профилировщик-самописец с накладными расходами ~1–2 % — можно держать включённым в проде.

```
-XX:StartFlightRecording=duration=300s,filename=/tmp/rec.jfr,settings=profile
```

Что записывает: сэмплы CPU по методам, аллокации (по местам выделения), GC-события и паузы, блокировки (`JavaMonitorEnter` — кто и сколько ждал монитор), ожидания сокетов и файлов (`SocketRead` дольше порога — с адресом и стеком!), safepoint-ы, компиляцию JIT, исключения. Анализ — JDK Mission Control (JMC) или `jfr print`/`jfr summary`.

Для НТ: снять JFR на 2–5 минут на пике нагрузки — один файл отвечает на вопросы «где CPU», «кто аллоцирует», «где ждут блокировки» и «какие сетевые вызовы медленные».

### 14.3 async-profiler

Сэмплирующий профилировщик без проблемы **safepoint bias** (обычные Java-профилировщики снимают стек только в safepoint-ах и искажают картину). Видит и Java, и нативный код, и ядро.

```bash
asprof -d 30 -e cpu   -f /tmp/cpu.html   <pid>   # CPU flame graph
asprof -d 30 -e alloc -f /tmp/alloc.html <pid>   # аллокации
asprof -d 30 -e lock  -f /tmp/lock.html  <pid>   # конкуренция за блокировки
asprof -d 30 -e wall  -f /tmp/wall.html  <pid>   # wall-clock: включая ожидание (off-CPU)
```

`wall`-режим показывает, где потоки **ждут** (БД, HTTP, блокировки) — главный режим для «CPU низкий, а latency высокая».

### 14.4 Native Memory Tracking

`-XX:NativeMemoryTracking=summary` (накладные расходы ~5–10 % памяти/CPU — на время диагностики) → `jcmd <pid> VM.native_memory summary`: сколько занимают Heap, Class (Metaspace), Thread (стеки), Code, GC, Internal, Symbol, Other (direct buffers). Отвечает на вопрос «heap 4 ГБ, а RSS процесса 7 ГБ — куда ушло 3 ГБ?».

***

## 15. Метрики JVM (Micrometer / Prometheus)

Spring Boot Actuator + Micrometer отдают метрики в формате Prometheus (`/actuator/prometheus`).

| Метрика | Что | На что смотреть |
|---------|-----|-----------------|
| `jvm_memory_used_bytes{area="heap",id=...}` | использование по пулам (G1 Eden Space, G1 Old Gen, Metaspace, CodeHeap...) | Old Gen после GC растёт → утечка |
| `jvm_memory_max_bytes`, `jvm_memory_committed_bytes` | лимит и выделенное | |
| `jvm_gc_pause_seconds_{count,sum,max}{action,cause}` | паузы GC | max пауз, частота; `cause="Allocation Failure"`, `"G1 Humongous Allocation"` |
| `jvm_gc_memory_allocated_bytes_total` (rate) | **скорость аллокаций** | главная причина частых GC |
| `jvm_gc_memory_promoted_bytes_total` (rate) | перенос в Old | высокая → объекты живут дольше Young |
| `jvm_gc_live_data_size_bytes` | живые данные в Old после GC | рост = утечка |
| `jvm_gc_overhead` (если есть) | доля CPU на GC | > 10 % — проблема |
| `jvm_threads_live_threads`, `jvm_threads_states_threads{state}` | потоки по состояниям | много BLOCKED → contention |
| `jvm_classes_loaded_classes` | загруженные классы | рост → ClassLoader leak |
| `jvm_buffer_memory_used_bytes{id="direct"}` | direct buffers (Netty, NIO) | рост → утечка off-heap |
| `process_cpu_usage`, `system_cpu_usage` | CPU процесса и системы | |
| `process_files_open_files` | открытые FD | |
| `executor_*` | пулы потоков (`ThreadPoolTaskExecutor`) — active, queued | очередь растёт → пул мал |
| `tomcat_threads_busy_threads`, `tomcat_threads_config_max_threads` | занятость потоков Tomcat | busy = max → очередь запросов |
| `hikaricp_connections_*` | пул соединений к БД | [[05 PostgreSQL]], раздел 20.3 |
| `http_server_requests_seconds` | RED по endpoint-ам | перцентили — из гистограмм (`percentiles-histogram`) |
| `http_client_requests_seconds` | исходящие вызовы | latency зависимостей |

```promql
# Доля времени в GC-паузах за минуту
rate(jvm_gc_pause_seconds_sum[1m])
# Скорость аллокаций, МБ/с
rate(jvm_gc_memory_allocated_bytes_total[1m]) / 1e6
# Занятость Tomcat
tomcat_threads_busy_threads / tomcat_threads_config_max_threads
# p99 endpoint-а
histogram_quantile(0.99, sum by (le, uri) (rate(http_server_requests_seconds_bucket[1m])))
```

***

## 16. Виртуальные потоки (Java 21+)

**Виртуальный поток** — лёгкий поток, управляемый JVM, а не ОС (аналог горутин — [[11 Go]], раздел 2). Монтируется на небольшой пул **потоков-носителей** (carrier threads, ForkJoinPool, по умолчанию = число ядер). При блокирующем вызове (сокет, `Thread.sleep`, `BlockingQueue`) виртуальный поток **отмонтируется**, носитель берёт другой.

```java
try (var executor = Executors.newVirtualThreadPerTaskExecutor()) { ... }
// Spring Boot 3.2+: spring.threads.virtual.enabled=true — Tomcat обрабатывает запросы на виртуальных потоках
```

**Что меняется для НТ:**

- предел «200 потоков Tomcat» исчезает — тысячи одновременных запросов без огромного числа потоков ОС;
- **узкое место переезжает** в пулы ресурсов: раньше 200 потоков естественно ограничивали нагрузку на БД, теперь 10 000 виртуальных потоков одновременно идут за соединениями HikariCP (пул 20) → огромное ожидание пула. Ограничивать семафорами/размером пула осознанно;
- **pinning:** в JDK 21–23 виртуальный поток, блокирующийся **внутри `synchronized`**, не отмонтируется и держит носитель. Если все носители «прибиты» — приложение стоит при свободном CPU. Диагностика: `-Djdk.tracePinnedThreads=full`, JFR-событие `jdk.VirtualThreadPinned`. В JDK 24 (JEP 491) `synchronized` больше не прибивает поток; нативные вызовы — по-прежнему прибивают;
- thread dump обычным `jstack` не показывает виртуальные потоки → `jcmd <pid> Thread.dump_to_file -format=json`;
- ThreadLocal в миллионе виртуальных потоков = миллион копий → память.

***

## 17. JVM в контейнере

- С JDK 10 (и 8u191) JVM **видит лимиты cgroup** (`-XX:+UseContainerSupport` по умолчанию): `Runtime.availableProcessors()` = CPU-лимит (округлённый вверх), размер кучи по умолчанию — от лимита памяти.
- По умолчанию `MaxRAMPercentage = 25 %` — в контейнере с 4 ГБ куча будет всего 1 ГБ. Явно: `-XX:MaxRAMPercentage=70` или `-Xmx`.
- **Память контейнера ≠ куча:** RSS = heap + Metaspace + Code Cache + стеки потоков + direct buffers + память GC (структуры G1 ~5–10 % кучи) + malloc нативных библиотек. Оставлять 25–35 % лимита на не-heap. Проверять NMT (раздел 14.4).
- **CPU-лимит и GC:** число потоков GC (`ParallelGCThreads`, `ConcGCThreads`) считается от видимых процессоров. Если лимит 2, а JVM почему-то видит 64 (старая JVM, `-XX:ActiveProcessorCount` не задан, CPU указан только в requests без limits) → 64 GC-потока сжигают квоту за миллисекунды → **throttling во время каждого GC** ([[13 Kubernetes, OpenShift, Docker]], раздел про CPU limits).
- Если в поде задан только `requests.cpu` без лимита — JVM видит все ядра ноды. Это нормально для использования свободного CPU, но размеры пулов (`ForkJoinPool.commonPool`, GC-потоки, Netty event loops) будут рассчитаны на 64 ядра.
- `-XX:ActiveProcessorCount=N` — явно задать, сколько процессоров «видит» JVM.

***

## 18. Сеть и JVM

### 18.1 Модели обработки соединений

| Сервер | Модель | Ключевые параметры |
|--------|--------|--------------------|
| Tomcat (Spring MVC) | NIO: акцепторы + поллер + пул рабочих потоков, **поток на запрос** | `server.tomcat.threads.max` (200), `max-connections` (8192), `accept-count` (100 — backlog), `connection-timeout`, `keep-alive-timeout`, `max-keep-alive-requests` (100) |
| Netty (WebFlux, gRPC, Kafka-клиенты частично) | event loop: N потоков (обычно 2 × ядра) обслуживают все соединения неблокирующе | **нельзя блокировать event loop** — как в asyncio ([[12 Python]], раздел 3.1) |
| Jetty / Undertow | похожие на Tomcat пулы | |

**Tomcat под нагрузкой:**

```
1. Соединений больше max-connections (8192) → новые ждут в очереди ОС (accept-count = 100)
2. Очередь ОС полна → ListenOverflows, клиенты получают таймаут подключения
3. Запросов одновременно больше threads.max (200) → они ждут поток
   Закон Литтла: 200 потоков / 0,1 с на запрос = максимум 2000 RPS;
   зависимость замедлилась до 1 с → 200 RPS, остальное — очередь
4. max-keep-alive-requests = 100 → после 100 запросов соединение закрывается,
   клиент переподключается (TLS заново!) — при высоком RPS это заметная доля рукопожатий
```

### 18.2 HTTP-клиенты и пулы

Дефолтные ограничения пулов Apache HttpClient (2 на маршрут в 4.x), OkHttp, RestTemplate без пула — [[03 Протоколы взаимодействия]], раздел 2.2. В Spring Boot 3: `RestClient`/`WebClient` с явно настроенным пулом и таймаутами. Reactor Netty `ConnectionProvider` — `maxConnections` (по умолчанию 2 × ядра, минимум 16), `pendingAcquireMaxCount`, `maxIdleTime` (важно — меньше idle timeout балансировщиков/фаерволов).

### 18.3 DNS-кэш JVM

`networkaddress.cache.ttl` (в `java.security` или `Security.setProperty`): по умолчанию 30 с, если нет SecurityManager. Отрицательные ответы — 10 с. Слишком большой TTL → после переключения IP (failover БД через DNS) JVM ходит на старый адрес.

### 18.4 TLS в JVM

- Рукопожатие — CPU (в профиле `sun.security.*`, `BigInteger`, `ECDSA`); дорогие RSA-ключи.
- Нативные провайдеры (Netty `netty-tcnative`/BoringSSL, Conscrypt) быстрее встроенного JSSE.
- `-Djavax.net.debug=ssl:handshake` — отладка (очень многословно).
- Ошибки `PKIX path building failed` — нет CA в truststore.

### 18.5 Direct memory и сеть

NIO и Netty используют **direct buffers** вне кучи для сетевого I/O. Лимит `-XX:MaxDirectMemorySize` (по умолчанию ≈ `-Xmx`). Утечка буферов Netty (не вызван `release()`) → рост `jvm_buffer_memory_used_bytes{id="direct"}` и RSS, затем `OutOfMemoryError: Direct buffer memory` или OOMKill контейнера. Netty-детектор утечек: `-Dio.netty.leakDetection.level=advanced`.

### 18.6 Сетевые ожидания в thread dump

```
"http-nio-8080-exec-17" RUNNABLE
   at sun.nio.ch.SocketDispatcher.read0(Native Method)
   ...
   at org.postgresql.core.PGStream.receiveChar(...)        ← ждёт ответ PostgreSQL
"http-nio-8080-exec-18" TIMED_WAITING (parking)
   at com.zaxxer.hikari.pool.HikariPool.getConnection(...)  ← ждёт соединение из пула
"http-nio-8080-exec-19" RUNNABLE
   at java.net.SocketInputStream.socketRead0(Native Method)
   at org.apache.http.impl.io.SessionInputBufferImpl...       ← ждёт ответ HTTP-зависимости (без read timeout — вечно)
```

Подсчёт стеков по «на чём стоят» сразу показывает узкое место: 150 из 200 потоков в `HikariPool.getConnection` → пул БД; 150 в `socketRead0` к одному хосту → медленная зависимость.

***

## 19. Поиск узких мест в JVM-сервисе — методика

```
1. RED сервиса: RPS, ошибки, p99 по endpoint-ам (http_server_requests_seconds)
2. CPU процесса vs лимит пода; throttling (container_cpu_cfs_throttled_*)
   CPU ≈ лимиту → async-profiler cpu / JFR → горячие методы
3. GC: доля пауз, max пауза, скорость аллокаций, Old после GC (раздел 15)
   паузы большие → разделы 3–4, 11; аллокации высокие → async-profiler alloc
4. CPU низкий, latency высокая → ожидание:
   thread dump × 3 (раздел 7) или async-profiler wall:
     HikariPool.getConnection → пул БД / медленные запросы
     socketRead0 → зависимость (+ таймауты!)
     BLOCKED на мониторе → contention
     Tomcat busy = max → потоки кончились (часто следствие ожиданий выше)
5. Память: RSS vs heap → NMT; direct buffers; Metaspace
6. Сеть: соединения, TIME_WAIT/CLOSE_WAIT, пулы HTTP-клиентов, DNS — [[02 Сеть — пропускная способность, фаерволы, узкие места]]
```

***

## 20. Вопросы на собеседовании (дополнительно к разделу 13)

1. Чем JFR отличается от async-profiler? Что такое safepoint bias?
2. Как найти, куда ушла память, если RSS процесса больше heap на гигабайты?
3. Какие метрики JVM ты выводишь на дашборд НТ и какие пороги считаешь тревожными?
4. Что такое виртуальные потоки и что они меняют для узких мест? Что такое pinning?
5. Как JVM определяет ресурсы в контейнере? Почему по умолчанию куча 25 % лимита?
6. Как работает Tomcat с потоками и соединениями? Что такое accept-count?
7. Как по thread dump понять, что узкое место — пул соединений к БД или внешняя зависимость?
8. Что такое direct memory и как она утекает?
