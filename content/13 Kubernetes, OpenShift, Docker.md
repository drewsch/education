---
domain: подготовка к собеседованиям
tags:
  - собеседование/k8s
status: to-review
order: 13
---
# K8s / OpenShift / Docker — Шпаргалка для собеседования

> Связанные темы: [[01 Linux — ядро, ресурсы, метрики]] (cgroups, PSI) · [[02 Сеть — пропускная способность, фаерволы, узкие места]] · [[10 JVM]] · [[11 Go]] · [[12 Python]] · [[15 Микросервисы и системный дизайн]]

***

## 1. Основные понятия

**Контейнер** — изолированный процесс в Linux. Использует namespaces (изоляция) и cgroups (ограничение ресурсов). Не виртуальная машина — ядро OS общее, только процесс изолирован.

**Docker Image** — слоистый снимок файловой системы. Каждая команда в Dockerfile = слой. Слои переиспользуются между контейнерами.

**Pod** — минимальная единица K8s. Один или несколько контейнеров на одной ноде с общим IP и volumes. Не самовосстанавливается — упал, нет пода. Поэтому создают Deployment.

**Node (Нода)** — физическая или виртуальная машина где работают поды. На ней kubelet (агент K8s) и container runtime (Docker/containerd).

**Кластер** — набор нод управляемых Control Plane (API Server, Scheduler, etcd).

**Namespace** — логическое разделение кластера. Разные окружения в одном кластере (прод, НТ, стейджинг). Поды из разных namespace изолированы.

**Label** — key-value метка на объекте. Service находит поды по label. HPA масштабирует deployment по label.

**Requests** — гарантия от scheduler. Scheduler ищет ноду где столько ресурсов свободно. Реальная память не выделяется при старте — только по мере использования (demand paging).

**Limits** — жёсткий потолок через cgroups. CPU: throttling (замедление). Memory: OOMKill (немедленное убийство пода).

**Throttling** — когда **под упёрся в свой cpu limit**. НЕ про ноду. Нода может быть свободна на 80% а под при этом throttled. Симптом: latency скачет, kubectl top показывает CPU = limit.

**OOMKill** — под превысил memory limit → kernel убивает процесс → K8s рестартует под. Exit Code: 137. Видно в kubectl describe pod → Reason: OOMKilled.

**Rolling Update** — плавная замена старых подов новыми без downtime. Один за одним.

**QoS классы подов:**

```
Guaranteed  → requests = limits → K8s убивает последним при нехватке ресурсов
Burstable   → requests < limits → убивает вторым
BestEffort  → нет requests/limits → убивает первым

```

***

## 2. Основные объекты K8s

### Pod

Запускает контейнеры. Не создаётся напрямую — только через Deployment.

```
spec:
  containers:
  - name: order-service
    image: order-service:1.2.3
    resources:
      requests:
        cpu: "500m"      # 0.5 ядра — гарантия scheduler
        memory: "512Mi"
      limits:
        cpu: "2000m"     # 2 ядра — потолок (throttling при превышении)
        memory: "2Gi"    # 2 ГБ — потолок (OOMKill при превышении)
    readinessProbe:      # K8s не шлёт трафик пока не готов
      httpGet:
        path: /actuator/health
        port: 8080
    livenessProbe:       # K8s рестартует если не отвечает
      httpGet:
        path: /actuator/health
        port: 8080

```

### Deployment

Управляет подами. Следит чтобы всегда было N реплик. Rolling Update при деплое.

```
kind: Deployment
spec:
  replicas: 4
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1         # можно временно иметь +1 под
      maxUnavailable: 0   # нельзя уменьшить количество подов
  selector:
    matchLabels:
      app: order-service
  template:               # шаблон пода
    metadata:
      labels:
        app: order-service
    spec:
      containers: [...]

```

### ConfigMap

Несекретная конфигурация отдельно от кода. Меняешь параметр — не нужно пересобирать image.

```
kind: ConfigMap
metadata:
  name: app-config
data:
  db.url: "jdbc:postgresql://postgres:5432/orders"
  kafka.bootstrap: "kafka:9092"
  thread.pool.size: "50"

```

Подключение к поду:

```
envFrom:
- configMapRef:
    name: app-config
# или отдельная переменная:
env:
- name: DB_URL
  valueFrom:
    configMapKeyRef:
      name: app-config
      key: db.url

```

Для НТ: меняешь thread pool, timeout, batch size прямо в configmap без пересборки.

### Secret

То же что ConfigMap но для паролей и ключей. Хранится в base64 (не шифрование).

```
kind: Secret
data:
  db.password: cGFzc3dvcmQxMjM=   # base64("password123")

```

### Service

Стабильный адрес для группы подов + балансировка нагрузки. Поды перезапускаются, меняют IP — Service всегда один адрес.

```
kind: Service
spec:
  selector:
    app: order-service     # находит поды по label
  ports:
  - port: 8080
    targetPort: 8080
  type: ClusterIP          # только внутри кластера

```

Типы Service:

```
ClusterIP    → только внутри кластера (дефолт)
               DNS: order-service.nt.svc.cluster.local
NodePort     → доступен снаружи через порт ноды (node-ip:30080)
LoadBalancer → внешний IP от cloud провайдера

```

### Ingress / Route

HTTP роутинг снаружи кластера внутрь.

```
https://order-service.apps.cluster.com
  → Ingress/Route (HTTP роутинг)
  → Service (балансировка)
  → Поды (обработка)

```

Route — OpenShift аналог Ingress, проще настраивается, встроен TLS.

### HPA (Horizontal Pod Autoscaler)

Автомасштабирование по метрикам.

```
kind: HorizontalPodAutoscaler
spec:
  scaleTargetRef:
    name: order-service
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        averageUtilization: 70   # CPU > 70% → добавить поды

```

Для НТ: отключай или фиксируй replicas — иначе результаты нестабильны.

### Namespace

Логическое разделение кластера.

```
namespace: production   ← прод
namespace: nt           ← нагрузочное тестирование
namespace: staging      ← стейджинг
namespace: dev          ← разработка

```

### ResourceQuota

Лимиты суммарных ресурсов на namespace.

```
kind: ResourceQuota
spec:
  hard:
    requests.cpu: "20"
    requests.memory: "40Gi"
    pods: "50"

```

### PersistentVolumeClaim (PVC)

Постоянное хранилище. Переживает рестарт пода.

```
kind: PersistentVolumeClaim
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 50Gi

```

***

## 3. Requests vs Limits — детально

```
requests: 512Mi → scheduler ищет ноду с 512 МБ свободной памяти
                  реальная память не выделяется сразу (demand paging)
                  по мере использования JVM → физическая память растёт

limits: 2Gi    → cgroup memory.limit_in_bytes = 2 ГБ
                  превысил → OOMKill, exit code 137

limits: 2000m CPU → cgroup cpu quota
                    throttled если хочет больше 2 ядер
                    НЕ убивает — замедляет

requests = limits → Guaranteed QoS → K8s убивает последним
requests < limits → Burstable QoS  → нода может быть overcommitted

```

**Почему requests = limits — лучшая практика для продакшена:**

```
requests < limits → нода overcommitted
  Scheduler думает что 8 ГБ свободно
  Реально поды используют больше своих requests
  При пиковой нагрузке → нода не справляется
  → K8s eviction → случайные убийства подов

```

***

## 4. Throttling vs Нода перегружена

```
Throttling:
  Под упёрся в свой cpu LIMIT
  Нода может быть свободна на 80%!
  kubectl top pod: CPU = limit (например 2000m из 2000m)
  Grafana: container_cpu_cfs_throttled_seconds_total > 0
  Решение: увеличить cpu limit

Нода перегружена:
  Суммарные запросы всех подов > физических ядер ноды
  kubectl top nodes: CPU 95%+
  Все поды на ноде страдают
  Решение: добавить ноды или перераспределить поды

```

***

## 5. Noisy Neighbor

Один под на ноде мешает другим:

```
Нода: 16 ядер
Поды: limits суммарно 20 CPU → overcommitted
report-service запускает тяжёлый отчёт → хочет 8 CPU
order-service throttled → latency растёт непонятно почему

Диагностика:
  kubectl get pods -o wide | grep node-name  → кто на той же ноде
  Grafana: throttled_seconds по подам

Решение:
  Pod Anti-Affinity → разнести поды по разным нодам
  Dedicated node → выделить ноду только для НТ
  requests = limits → честное планирование

```

***

## 6. Полезные команды

```
# Поды
kubectl get pods -n nt                          # список подов
kubectl get pods -n nt -w                       # наблюдение в реальном времени
kubectl describe pod pod-name -n nt             # детали + события + причина краша
kubectl logs -f pod-name -n nt                  # логи
kubectl logs pod-name -n nt --previous          # логи до краша
kubectl exec -it pod-name -n nt -- bash         # войти внутрь

# Ресурсы
kubectl top pods -n nt                          # CPU/RAM подов
kubectl top nodes                               # нагрузка на ноды

# Deployment
kubectl scale deployment order-service --replicas=8 -n nt   # масштабировать
kubectl rollout restart deployment order-service -n nt       # рестарт подов
kubectl rollout status deployment order-service -n nt        # статус деплоя

# ConfigMap
kubectl edit configmap app-config -n nt         # редактировать конфиг
kubectl get configmap app-config -n nt -o yaml  # посмотреть конфиг

# События
kubectl get events -n nt --sort-by='.lastTimestamp' | tail -20

# Сравнение ПРОД и НТ конфигов
kubectl describe deployment order-service -n production > prod.txt
kubectl describe deployment order-service -n nt > nt.txt
diff prod.txt nt.txt

# OpenShift (oc = расширенный kubectl)
oc get pods -n nt
oc rsh pod-name              # войти внутрь (аналог exec -it)
oc get routes -n nt          # внешние URL
oc rollout restart dc/order-service -n nt

```

***

## 7. Grafana метрики K8s при НТ

```
CPU throttling (красный флаг!):
  rate(container_cpu_cfs_throttled_seconds_total{container="order-service"}[1m]) > 0

Memory использование (> 0.9 → скоро OOMKill):
  container_memory_working_set_bytes / container_spec_memory_limit_bytes

Рестарты подов:
  kube_pod_container_status_restarts_total

Количество доступных подов:
  kube_deployment_status_replicas_available

Network:
  rate(container_network_transmit_bytes_total[1m])
  rate(container_network_receive_bytes_total[1m])

```

***

## 8. OpenShift vs K8s

```
Функция              K8s              OpenShift
──────────────────────────────────────────────────────
GUI                  Dashboard        OpenShift Console
CI/CD                нет              Jenkins, Tekton встроены
Image registry       нет              встроенный registry
HTTP роутинг         Ingress          Route (проще, TLS встроен)
Security             базовая          SCC (строже)
GitOps               нет              Argo CD часто преднастроен
CLI                  kubectl          oc (надстройка над kubectl)
Namespace            Namespace        Project (+ RBAC)
Deployment           Deployment       DeploymentConfig (больше триггеров)

```

> ⚠️ DeploymentConfig объявлен устаревшим с OpenShift 4.14 — в новых проектах используют обычный Deployment.

**Argo CD** — GitOps инструмент. Конфиг в Git → Argo автоматически деплоит в кластер. Меняешь YAML (replicas, limits, configmap) → делаешь commit → Argo применяет.

**Lens** — GUI клиент для K8s/OpenShift. Видишь все поды, метрики, логи в одном окне. Удобно для диагностики во время НТ.

***

## 9. Типичные проблемы при НТ в K8s

```
Симптом                     Причина                   Решение
──────────────────────────────────────────────────────────────────────
Latency высокая,            CPU throttling             Увеличить cpu limit
CPU в поде низкий           (limit слишком мал)

Поды перезапускаются        OOMKilled                  Увеличить memory limit
Exit Code: 137              (memory limit мал)         или найти утечку

НТ результаты хуже ПРОД     KTC разные (limits/        Выровнять конфиги
                            requests, replicas)         diff prod vs nt

Нестабильные результаты     Noisy neighbor             Pod Anti-Affinity
                            другой под на ноде          или dedicated node

HPA добавляет поды          Автомасштабирование        Отключить HPA или
во время НТ                 мешает тесту               зафиксировать replicas

Поды не стартуют            Нет ресурсов на ноде       kubectl describe pod
(Pending статус)            ResourceQuota исчерпана    смотри Events секцию

```

***

## 10. Docker основные команды

```
# Образы
docker build -t service:1.0 .             # собрать image
docker images                              # список images
docker pull openjdk:17                     # скачать image

# Контейнеры
docker run -d \
  -p 8080:8080 \                           # порт host:container
  -e JAVA_OPTS="-Xmx2g" \                 # переменная окружения
  --memory=2g \                            # лимит памяти
  --cpus=2 \                               # лимит CPU
  service:1.0

docker ps                                  # запущенные контейнеры
docker logs -f container_id                # логи
docker exec -it container_id bash          # войти внутрь
docker stats                               # CPU/Memory всех контейнеров
docker inspect container_id                # полная информация

```

***

## 11. CPU limits в Kubernetes: почему под «простаивает», а его всё равно троттлят — глубокое погружение

Это одна из самых частых и самых неочевидных причин плохих хвостов latency на НТ в Kubernetes. Базовая механика cgroups — в [[01 Linux — ядро, ресурсы, метрики]], раздел 8.

### 11.1 Как requests и limits превращаются в настройки ядра

| В манифесте | Что настраивается в cgroup v2 | Как работает |
|-------------|-------------------------------|-------------|
| `requests.cpu: 500m` | `cpu.weight` (вес в CFS/EEVDF) | **Пропорциональная доля** CPU при конкуренции. Если ядра свободны — контейнер может брать больше. Это не ограничение, а гарантия при борьбе за CPU |
| `limits.cpu: 2` | `cpu.max = "200000 100000"` | **Жёсткая квота**: не больше 200 мс процессорного времени за каждый период 100 мс, суммарно по всем потокам контейнера |

Ключевое: **лимит — это не «2 ядра», а «200 мс CPU-времени в каждом окне 100 мс»**. Эти 200 мс можно потратить как угодно: 2 потока по 100 мс, или 20 потоков по 10 мс.

### 11.2 Почему средняя загрузка врёт

Метрики CPU в Prometheus (`rate(container_cpu_usage_seconds_total[1m])`) усредняют потребление **за минуту**. Квота же проверяется **каждые 100 мс**.

```
Под с limits.cpu = 1 (100 мс CPU на период 100 мс). Сервис на 8 потоках.
Запрос приходит и обрабатывается параллельно 8 потоками по 25 мс CPU каждый = 200 мс CPU.

Время 0 мс:    8 потоков начинают работу
Время 12,5 мс: потрачено 8 × 12,5 = 100 мс CPU → квота исчерпана → ВСЕ потоки остановлены
Время 100 мс:  новый период, квота обновилась → потоки продолжают
Время 112,5 мс: доработали оставшиеся 100 мс CPU
Запрос, который мог выполниться за 25 мс (8 потоков параллельно), выполнился за 112,5 мс.

Если такие запросы приходят 2 раза в секунду, средняя загрузка = 2 × 200 мс / 1000 мс = 0,4 ядра = 40 % лимита.
Графики: «CPU 40 %, всё хорошо». Реальность: каждый запрос ждёт по ~90 мс из-за троттлинга.
```

Это и есть ответ на вопрос «почему под idle, а его троттлят»: **троттлинг определяется всплесками внутри 100-миллисекундных окон, а не средней загрузкой**. Многопоточное приложение способно сжечь квоту за малую долю периода.

### 11.3 Типичные источники всплесков

- **GC JVM**: параллельные фазы (G1 Young, Mixed) запускают `ParallelGCThreads` потоков. JVM, видящая 16 процессоров при лимите 2, за одну паузу GC сжигает квоту → пауза GC растягивается на десятки миллисекунд троттлинга ([[10 JVM]], раздел 17).
- **Go-рантайм** с GOMAXPROCS = числу ядер ноды (Go < 1.25 без automaxprocs) ([[11 Go]], раздел 11).
- **Пулы потоков**, рассчитанные по числу ядер ноды: `ForkJoinPool.commonPool`, Netty event loops, `os.cpu_count()` в Python ([[12 Python]], раздел 10).
- **JIT-компиляция** при старте/прогреве (C1/C2 потоки).
- **Пиковые запросы**: параллельная обработка (`parallelStream`, fan-out с CompletableFuture), сжатие, TLS-рукопожатия при волне новых соединений.
- **Старт приложения**: Spring Boot при старте с жёстким лимитом стартует в разы дольше → readiness/liveness пробы проваливаются → рестарты.

### 11.4 Как увидеть троттлинг

```promql
# Доля периодов, в которых контейнер был троттлен (0..1)
sum by (namespace, pod, container) (rate(container_cpu_cfs_throttled_periods_total[5m]))
/
sum by (namespace, pod, container) (rate(container_cpu_cfs_periods_total[5m]))

# Сколько секунд в секунду контейнер простоял из-за троттлинга
rate(container_cpu_cfs_throttled_seconds_total[5m])

# Использование относительно лимита (среднее — само по себе обманчиво!)
rate(container_cpu_usage_seconds_total[5m]) / on(namespace,pod,container)
  kube_pod_container_resource_limits{resource="cpu"}
```

```bash
# Прямо в cgroup контейнера (cgroup v2)
cat /sys/fs/cgroup/cpu.max        # квота и период: "200000 100000"
cat /sys/fs/cgroup/cpu.stat       # usage_usec, nr_periods, nr_throttled, throttled_usec
# nr_throttled / nr_periods — доля троттлинговых периодов с момента старта
```

**Интерпретация:**

| Доля троттлинговых периодов | Значение |
|-----------------------------|----------|
| 0–1 % | норма |
| 1–5 % | возможно влияние на p99 — смотреть корреляцию с хвостами latency |
| > 10 % | почти наверняка видно в latency; на НТ результаты некорректны |
| троттлинг при среднем CPU < 50 % лимита | классика «всплесков»: многопоточность, GC, неправильно рассчитанные пулы |

**Корреляция с latency:** наложить на один график p99 сервиса и `throttled_seconds` — синхронные всплески подтверждают диагноз. JFR/async-profiler троттлинг не видят (с точки зрения процесса время «просто прошло»), поэтому смотреть метрики cgroup обязательно.

### 11.5 Что делать

| Решение | Плюсы | Минусы и риски |
|---------|-------|----------------|
| **Убрать CPU limits, оставить requests** | нет троттлинга; под использует свободный CPU ноды; `cpu.weight` всё равно делит CPU пропорционально requests при конкуренции | «шумный сосед» может съесть свободный CPU ноды (но не гарантированную долю других); QoS — Burstable; в некоторых компаниях политика требует лимиты |
| **Увеличить лимит** (например, limit = 2–4 × request) | меньше вероятность исчерпать квоту во всплеске | под может получать CPU сверх запроса — на перегруженной ноде результаты нестабильны |
| **Согласовать рантайм с лимитом** | меньше потоков → меньше всплеск | JVM: `-XX:ActiveProcessorCount`, `ParallelGCThreads`, `ConcGCThreads`; Go: GOMAXPROCS (1.25+ сам); Python: явное число воркеров; размеры пулов |
| **Целые ядра + static CPU manager** (`requests = limits` = целое число, QoS Guaranteed, kubelet `--cpu-manager-policy=static`) | контейнер получает **эксклюзивные** ядра (cpuset), без квоты CFS и без соседей | ядра недоступны другим подам; только целые числа; нужна настройка kubelet |
| **CFS burst** (`cpu.max.burst` в cgroup v2, ядро 5.14+) | разрешить кратковременно превышать квоту за счёт неиспользованного «накопленного» времени | в ванильном Kubernetes нет полей манифеста для burst — только через настройку runtime/ноды |
| **Меньший период квоты** (`cpuCFSQuotaPeriod` kubelet, feature gate) | всплески распределяются ровнее | больше накладных расходов планировщика; редко используют |

**Исторический баг ядра:** до ядер 5.4 (и бэкпортов в некоторых дистрибутивах) в механизме CFS bandwidth был дефект с «истечением» неиспользованных кусков квоты на отдельных CPU, из-за которого троттлинг возникал даже у подов, **не доходивших** до лимита. На старых нодах (CentOS 7 и подобных) это добавляло троттлинга сверх объяснимого. Проверять версию ядра нод.

### 11.6 Что это значит для НТ

1. **Добавить троттлинг на дашборд НТ** рядом с CPU — без него «CPU 40 %» вводит в заблуждение.
2. Конфигурация requests/limits на стенде НТ **должна совпадать с продом** — иначе троттлинг на одном и отсутствие на другом сделают результаты несравнимыми.
3. При поиске максимальной производительности сначала посмотреть, не упирается ли сервис в квоту, а не в реальный CPU ноды.
4. Проверить, как рантайм видит процессоры: `Runtime.getRuntime().availableProcessors()` в JVM, `runtime.GOMAXPROCS(0)` в Go, `os.cpu_count()` в Python — и сравнить с лимитом.
5. Отдельный тест: тот же сервис с лимитом и без лимита (только requests) — разница в p99 покажет цену троттлинга.

***

## 12. Сеть в Kubernetes

### 12.1 Путь запроса

```
Клиент → внешний LB → Ingress-контроллер (nginx/Envoy) → Service (ClusterIP)
       → kube-proxy правила (iptables / IPVS / nftables / eBPF) — DNAT на IP пода
       → CNI-сеть (overlay VXLAN/Geneve или маршрутизация BGP) → под (возможно — sidecar Envoy) → приложение
```

Каждый элемент добавляет latency и имеет свои лимиты.

### 12.2 CNI и overlay

- **CNI-плагин** (Calico, Cilium, Flannel, OVN-Kubernetes в OpenShift) создаёт сеть подов.
- **Overlay** (VXLAN, Geneve) инкапсулирует пакеты пода в пакеты ноды: +50 байт заголовков → MTU пода 1450 вместо 1500; дополнительный CPU на инкапсуляцию (softirq). Неправильный MTU → PMTU black hole ([[02 Сеть — пропускная способность, фаерволы, узкие места]], раздел 9).
- **Без overlay** (BGP, нативная маршрутизация облака) — меньше накладных расходов.
- **eBPF** (Cilium) — заменяет iptables и kube-proxy, быстрее при большом числе сервисов.

### 12.3 kube-proxy и Service

| Режим | Как | Под нагрузкой |
|-------|-----|--------------|
| iptables (по умолчанию долгое время) | цепочки правил DNAT, выбор пода — случайный (`statistic --probability`) | правила обрабатываются линейно → при тысячах сервисов растёт CPU и время обновления; балансировка **соединений**, а не запросов |
| IPVS | хэш-таблицы ядра, алгоритмы rr/lc/sh | лучше масштабируется на много сервисов |
| nftables (новый) | наборы и словари nftables | быстрее iptables |
| eBPF (Cilium, kube-proxy replacement) | балансировка в eBPF на сокете/в драйвере | минимальные накладные расходы |

**Service = L4.** HTTP/2, gRPC, пулы соединений к сервису распределяются по подам **соединениями** → перекос нагрузки ([[03 Протоколы взаимодействия]], раздел 7.3). Признак на НТ: при 6 репликах CPU сильно различается между подами.

**Session affinity / externalTrafficPolicy:** `externalTrafficPolicy: Local` сохраняет IP клиента и не делает лишний хоп между нодами, но трафик идёт только на ноды с подами — возможен перекос.

### 12.4 conntrack на нодах

Весь трафик через Service проходит через conntrack ноды (DNAT). Высокий темп **новых** соединений (нет keep-alive, генератор нагрузки без пула) переполняет таблицу → `nf_conntrack: table full, dropping packet` в dmesg ноды, таймауты подключения ([[02 Сеть — пропускная способность, фаерволы, узкие места]], раздел 12). Смотреть `node_nf_conntrack_entries / node_nf_conntrack_entries_limit` по нодам.

### 12.5 DNS в кластере

- CoreDNS — Deployment в `kube-system`; каждый под разрешает имена через него.
- `ndots:5` → имя внешнего сервиса проверяется с 3–5 суффиксами → в 4–10 раз больше DNS-запросов ([[02 Сеть — пропускная способность, фаерволы, узкие места]], раздел 10).
- Под нагрузкой с короткими соединениями CoreDNS становится узким местом: метрики `coredns_dns_requests_total`, `coredns_dns_request_duration_seconds`, CPU подов CoreDNS.
- Известная проблема — гонки conntrack при параллельных UDP-запросах A и AAAA → задержки ровно 5 с. Решения: NodeLocal DNSCache, `options single-request-reopen` (glibc), FQDN с точкой, снизить `ndots` в `dnsConfig` пода.

### 12.6 NetworkPolicy

По умолчанию все поды могут общаться со всеми. NetworkPolicy ограничивает ingress/egress по меткам и namespace. Реализует CNI (iptables или eBPF на нодах). Типичная ошибка на НТ: политика блокирует трафик генератора нагрузки или метрик (Prometheus не может скрейпить) — симптом «connect timeout» (DROP), а не «refused».

### 12.7 Service mesh

Sidecar Envoy на каждом поде: mTLS, L7-балансировка, ретраи, метрики. Цена: +2 прокси на каждый вызов (~0,5–3 мс p99), CPU и память sidecar (они **тоже** имеют свои requests/limits и могут троттлиться!). Ретраи mesh + ретраи приложения = умножение нагрузки при сбое ([[15 Микросервисы и системный дизайн]], разделы 12–13).

### 12.8 Ingress / Route

- Ingress-контроллер (nginx, HAProxy, Envoy-based) — L7: завершает TLS, держит соединения к подам.
- Узкие места: CPU контроллера (TLS, логирование каждого запроса), число worker-процессов, keep-alive к апстримам (`upstream-keepalive-connections`), таймауты (`proxy-read-timeout` 60 с по умолчанию → 504 на долгих запросах).
- OpenShift Router (HAProxy): `maxconn`, число потоков, idle timeout маршрута (`haproxy.router.openshift.io/timeout`).

***

## 13. Пробы, graceful shutdown, деплой под нагрузкой

### 13.1 Пробы

| Проба | Провал приводит к | Ошибки настройки |
|-------|-------------------|------------------|
| `startupProbe` | рестарт, пока не прошла; остальные пробы ждут | без неё медленный старт (JVM) убивается liveness-пробой |
| `readinessProbe` | под убирается из Endpoints (не получает трафик) | проверять внешние зависимости (БД) → упала БД → **все** поды не ready → полный отказ; слишком частая проба под нагрузкой — сама нагрузка |
| `livenessProbe` | **рестарт контейнера** | тяжёлая проверка с коротким таймаутом: под нагрузкой (GC, троттлинг!) проба не успевает → рестарт → нагрузка на остальные поды → каскад рестартов |

Правило: liveness — максимально лёгкая («процесс жив и не завис»), с щедрым `timeoutSeconds` и `failureThreshold`; readiness — «готов обслуживать», без глубоких проверок внешних систем.

### 13.2 Graceful shutdown

```
1. Под получает команду на удаление
2. Одновременно: под удаляется из Endpoints  И  контейнер получает SIGTERM
   (удаление из Endpoints распространяется на все ноды/ingress за секунды — гонка!)
3. preStop hook выполняется ДО SIGTERM → sleep 5–10 с, чтобы трафик перестал приходить
4. Приложение по SIGTERM перестаёт принимать новые запросы, дорабатывает текущие
   (Spring Boot: server.shutdown=graceful, spring.lifecycle.timeout-per-shutdown-phase=30s)
5. terminationGracePeriodSeconds (30 с) истёк → SIGKILL
```

Без `preStop`-задержки и graceful shutdown каждый деплой или масштабирование вниз под нагрузкой даёт всплеск 502/503 и обрывов соединений. **Отдельный сценарий НТ: rolling update под целевой нагрузкой — считать ошибки.**

***

## 14. Поиск узких мест в Kubernetes — методика

```
1. Под: CPU usage vs limit, ТРОТТЛИНГ (раздел 11), память vs limit (working_set), рестарты, OOMKilled
2. Распределение нагрузки между подами: одинаковы ли CPU и RPS у реплик? (L4 + долгие соединения)
3. Нода: CPU/память/PSI ноды, соседи (kubectl get pods -o wide --field-selector spec.nodeName=...),
   steal, conntrack, softirq — [[01 Linux — ядро, ресурсы, метрики]]
4. Сеть: Ingress (CPU, 5xx, таймауты), CoreDNS (latency, CPU), sidecar-ы, MTU
5. Масштабирование: HPA во время теста (зафиксировать реплики), cluster autoscaler (новые ноды — минуты)
6. События: kubectl get events — Evicted, FailedScheduling, Unhealthy (пробы), OOMKilling
7. Приложение внутри пода — по стеку: [[10 JVM]], [[11 Go]], [[12 Python]]
```

***

## 15. Вопросы на собеседовании

1. Как requests и limits CPU превращаются в настройки ядра? Чем вес отличается от квоты?
2. Почему под с низкой средней загрузкой CPU может сильно троттлиться? Посчитай пример.
3. Какие метрики показывают троттлинг? Как связать его с latency?
4. Как бороться с троттлингом: убрать лимиты, static CPU manager, настроить рантайм? Плюсы и минусы.
5. Почему JVM/Go/Python в контейнере могут неправильно оценить число процессоров и чем это грозит?
6. Чем отличаются QoS-классы и кого убьют первым при нехватке памяти?
7. Что такое working_set и почему OOMKill происходит по нему?
8. Как работает Service и kube-proxy? Почему gRPC-нагрузка распределяется неравномерно?
9. Какие сетевые проблемы бывают в кластере под нагрузкой (conntrack, DNS ndots, MTU)?
10. Чем readiness отличается от liveness? Какие ошибки настройки проб опасны под нагрузкой?
11. Как сделать graceful shutdown и почему без preStop бывают ошибки при деплое?
12. Как обеспечить сравнимость результатов НТ между стендом и продом в Kubernetes?
