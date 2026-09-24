<p align="center">
  <a href="README.md">English</a> · <a href="README.ru_RU.md">Русский</a>
</p>

<p align="center">
  <img src="media/hero.svg" alt="3xui-tester — проверка конфигураций inbound" width="100%">
</p>

<p align="center">
  <a href="https://github.com/OctoberIsTired/3xui-tester/actions/workflows/tests.yml"><img src="https://github.com/OctoberIsTired/3xui-tester/actions/workflows/tests.yml/badge.svg" alt="Тесты"></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+">
</p>

# 3xui-tester

`3xui-tester` проверяет варианты конфигурации inbound в 3x-ui/Xray. Он читает
OpenAPI работающей панели, получает исходный inbound, создаёт временный клон
и запускает локальный Xray-клиент для измерений. Результат каждого повтора
записывается в журнал, чтобы эксперимент можно было продолжить.

Проект проверен с 3x-ui v3.8.0 и Xray v26.9.9. Совместимость конкретной панели
проверяется перед изменением inbound: если её OpenAPI не содержит нужных
операций, запуск завершается до каких-либо изменений.

## Навигация

- [Подробная инструкция по запуску](docs/START.ru_RU.md) — установка,
  настройка, первый тест и работа с результатами по шагам.
- [Быстрый старт](#быстрый-старт) — подготовка конфигурации и первый запуск.
- [SSH-туннель к API 3x-ui](#ssh-туннель-к-api-3x-ui) — доступ к панели на сервере.
- [Команды CLI](#команды-cli) и [Web UI](#web-ui) — способы работы.
- [Конфигурация](#конфигурация) — ключевые поля и параметры.
- [Результаты и восстановление](#результаты-и-восстановление) — файлы, остановка и `--resume`.
- [ARCHITECTURE.md](ARCHITECTURE.md) — компоненты и жизненный цикл.

## Возможности

- Стратегии подбора: `mutation`, `pairwise` и `exhaustive`, с лимитом
  `testing.max_combinations`.
- Параметры inbound и локального Xray-клиента, условия и нормализация
  неактивных параметров.
- Измерения TCP, ICMP, HTTP latency/p95, стабильности и, по желанию,
  ограниченной загрузки.
- Режим `clone` по умолчанию; режим `existing` требует `allow_existing: true`
  и сохраняет снимок исходного inbound для восстановления.
- Durable-журнал, CSV/JSON/XLSX-экспорт и `--resume`.
- Локальный Web UI, привязанный только к `127.0.0.1`.

## Требования

- Python 3.12 или новее.
- [uv](https://docs.astral.sh/uv/).
- Бинарный файл Xray, доступный машине, на которой выполняется тест.
- Сетевой доступ к 3x-ui API и к публичному адресу тестового inbound.
- Учётные данные панели: предпочтительно API token.

Для настоящего теста машина запуска должна иметь доступ к clone inbound. На
практике это обычно Linux-сервер с 3x-ui. Windows подходит для локальной
разработки, preview и управления удалённой панелью, если на нём есть Xray и
маршрут до тестового inbound.

## Быстрый старт

1. Скопируйте `configs/example.yaml` в отдельный локальный файл, например
   `configs/local.yaml`.
2. Укажите `panel.url`, `inbound.source_id`, публичный
   `testing.server_address` и путь `testing.xray_binary`.
   Для REALITY при исходном TLS задайте также `testing.reality.target` и
   `testing.reality.server_names`.
3. Передайте секрет через переменную окружения, а не в YAML.
4. Выполните preview и `--dry-run`, затем проверьте достижимость тестового
   адреса и порта с машины запуска перед реальным тестом.

Конфигурация подставляет переменные вида `${PANEL_API_TOKEN}` из окружения.
Не добавляйте токены и пароли в Git.

## Сценарий запуска на Linux

Подходит для Ubuntu-сервера с 3x-ui, где уже установлен Xray.

```bash
git clone https://github.com/OctoberIsTired/3xui-tester.git
cd 3xui-tester
uv sync --extra dev
cp configs/example.yaml configs/local.yaml
export PANEL_API_TOKEN='replace-with-token'
```

Откройте `configs/local.yaml` и задайте как минимум:

```yaml
panel:
  url: "https://panel.example.com"
  api_token: "${PANEL_API_TOKEN}"

inbound:
  mode: clone
  source_id: 4

testing:
  server_address: "vpn.example.com"
  xray_binary: "/usr/local/x-ui/bin/xray-linux-amd64"
```

Проверьте план без соединения с панелью, затем выполните preflight без
изменения inbound:

```bash
uv run 3xui-tester combinations preview -c configs/local.yaml
uv run 3xui-tester test -c configs/local.yaml --dry-run
```

`--dry-run` обращается к панели, проверяет OpenAPI, исходный inbound и статус
Xray. Даже после успешного `--dry-run` измерения могут не пройти: порт клона
должен быть доступен с машины, запускающей тестер. Автоматический выбор порта
проверяет его доступность только локально, а не на сервере. Образец конфигурации
перебирает TLS, REALITY и разные транспорты без привязки к конкретному исходному
inbound, поэтому Xray может отклонить часть сочетаний. Для KCP нужен доступ по
UDP, а для TLS важны сертификат и имя сервера. `--dry-run` не проверяет
достижимость тестового порта и URL измерений.

Offline preview предупреждает о возможных проблемах без подключения к панели.
После чтения исходного inbound `--dry-run` показывает точное число
`skipped_combinations` для pairwise и exhaustive или
`skipped_initial_candidates` для mutation. Если исходный inbound использует TLS,
а оба поля REALITY не заданы, кандидаты REALITY будут пропущены. Тестер
проверяет формат target и согласованность SNI, но не проверяет доступность
target по сети: её нужно проверить с самого сервера Xray.

Если проверки прошли успешно, запустите ограниченный эксперимент:

```bash
uv run 3xui-tester test -c configs/local.yaml --max-tests 50
```

Для остановленного запуска используйте ту же конфигурацию и каталог результатов:

```bash
uv run 3xui-tester test -c configs/local.yaml --resume
```

## Сценарий запуска на Windows

Подходит для PowerShell на рабочей станции или Windows Server. Установите
Python 3.12+, uv и распакуйте Windows-сборку Xray, например в
`.tools/xray/xray.exe`.

```powershell
git clone https://github.com/OctoberIsTired/3xui-tester.git
Set-Location 3xui-tester
uv sync --extra dev
Copy-Item configs/example.yaml configs/local.yaml
$env:PANEL_API_TOKEN = 'replace-with-token'
```

В `configs/local.yaml` используйте прямые слеши в пути Xray — YAML и Python
корректно обработают такой путь:

```yaml
testing:
  xray_binary: ".tools/xray/xray.exe"
  server_address: "vpn.example.com"
```

Сначала выполните preview и dry-run:

```powershell
uv run 3xui-tester combinations preview -c configs/local.yaml
uv run 3xui-tester test -c configs/local.yaml --dry-run
```

Для полного запуска команда та же:

```powershell
uv run 3xui-tester test -c configs/local.yaml --max-tests 50
```

Windows не устанавливает обработчик `SIGTERM`; нажмите `Ctrl+C` для штатной
остановки. Runner завершит локальный Xray и выполнит cleanup clone inbound.

## SSH-туннель к API 3x-ui

Если тестер запущен на рабочей станции, а API 3x-ui доступен только на сервере,
пробросьте порт панели через SSH. Так тестер сможет обращаться к API управления
inbound без публикации порта панели в интернете:

```bash
ssh -N -L 2054:127.0.0.1:2054 user@server
```

Оставьте SSH-команду работающей в отдельном терминале на время работы CLI или
Web UI. Первый `2054` — локальный порт рабочей станции, а
`127.0.0.1:2054` — адрес API панели с точки зрения сервера. Если панель
слушает другой порт, замените числа. В конфигурации укажите локальный конец
туннеля с фактической схемой HTTP или HTTPS панели:

```yaml
panel:
  url: "https://127.0.0.1:2054"
testing:
  server_address: "vpn.example.com"
```

`panel.url` нужен для запросов к API 3x-ui. `testing.server_address` — адрес
Xray inbound на сервере, доступный тестовому клиенту. Порт тестового inbound
(`inbound.test_port`, если задан) должен быть доступен с машины запуска
отдельно: SSH-туннель к API не передаёт тестовый трафик. Если тестер запущен
на том же сервере, что и 3x-ui, можно указать локальный URL панели без SSH.

## Web UI

Локальная веб панель состоит из четырёх разделов:

| Раздел | Возможности |
| --- | --- |
| Подключение | Создание YAML с нуля: адрес панели, ID исходного inbound, порт копии, адрес сервера, путь к Xray, TLS и URL измерений. |
| Параметры | Каталог параметров из API панели, фиксация исходной конфигурации, собственные пути Xray. |
| План | Стратегия поиска, лимиты, пороги качества и предварительный просмотр комбинаций. |
| Запуск и результаты | Запуск и остановка теста, текущие метрики, история запусков и скачивание отчётов JSON, CSV, XLSX. |

Задайте переменную `PANEL_API_TOKEN` и запустите панель из корня репозитория.
Файл `configs/local.yaml` заранее создавать не нужно:

```bash
uv run python -m app.web --port 8765
```

Откройте `http://127.0.0.1:8765`, укажите адрес панели и загрузите список
inbound. Выберите исходный ID, заполните остальные поля подключения и нажмите
«Сохранить конфигурацию YAML». Затем выберите параметры эксперимента. По
умолчанию UI создаёт `configs/local.yaml`;
другой путь можно указать через `--config`. UI читает и сохраняет этот файл,
показывает inbound и запускает тест в фоне. Каждый запуск UI записывает в
отдельный каталог `output.directory/runs/<время>-<id>`; история и ссылки на
готовые отчёты доступны после обновления страницы. Учётные данные не
передаются браузеру; UI не требует авторизации, поэтому оставляйте его
доступным только локально. Если Web UI запущен на удалённом сервере,
пробросьте его порт отдельно для доступа из локального браузера:

```bash
ssh -L 8765:127.0.0.1:8765 user@server
```

После этого откройте локальный адрес в браузере рабочей станции.

Запросы к API панели по умолчанию идут напрямую, минуя системный HTTP proxy.
Если для доступа к панели нужен именно proxy, включите «Использовать системный
proxy» в разделе «Подключение».

## Команды CLI

```bash
uv run 3xui-tester panel test -c configs/local.yaml
uv run 3xui-tester inbound list -c configs/local.yaml
uv run 3xui-tester inbound show 4 -c configs/local.yaml
uv run 3xui-tester parameters list -c configs/local.yaml
uv run 3xui-tester combinations preview -c configs/local.yaml
uv run 3xui-tester test -c configs/local.yaml --dry-run
uv run 3xui-tester test -c configs/local.yaml --max-tests 50
uv run 3xui-tester test -c configs/local.yaml --resume
```

`--dry-run` проходит аутентификацию, discovery и preflight, но не создаёт,
не обновляет и не удаляет inbound. В режиме `existing` preflight записывает
`source-inbound-backup.json` даже при `--dry-run`. `--max-tests` ограничивает
число конфигураций дополнительно к `testing.max_combinations`.
После настоящего запуска проверьте `failed` в сводке и `result.status` в
`results.jsonl`: успешно завершившийся процесс может содержать неудачные
конфигурации. Например, Xray отклоняет VLESS без шифрования при подключении
к публичному адресу сервера.
Невыполнимые сочетания записываются один раз со статусом `SKIPPED` и кодом
причины до изменения inbound; в сводке пропуски отделены от сетевых ошибок.
Предупреждения укажут на короткий timeout screening и на то, что
`testing.max_failed_runs` может сократить фактическое число повторов.
В `results.xlsx` номер `#N` на диаграммах и в рейтинге листа `Dashboard`
соответствует строке `#N` на листе `Candidates`. Рядом указаны все параметры,
ID теста и хеш конфигурации. Оси диаграмм подписаны названиями измерений и
единицами: задержка — мс, скорость — Мбит/с, балл — от 0 до 1.
Чтобы проверить весь построенный план в пределах `testing.max_combinations`,
запустите `test` без `--max-tests`.

## Конфигурация

Полный пример находится в `configs/example.yaml`. Важные поля:

| Поле | Назначение |
| --- | --- |
| `panel.url` | Базовый URL 3x-ui без пути конкретной страницы. |
| `panel.api_token` | API token, обычно `${PANEL_API_TOKEN}`. |
| `inbound.source_id` | ID inbound, от которого создаётся clone. |
| `inbound.mode` | `clone` по умолчанию; `existing` требует `allow_existing: true`. |
| `inbound.test_port` | Необязательный фиксированный порт клона; если он не задан, выбирается первый локально свободный порт из `testing.port_range`. |
| `testing.server_address` | Публичный адрес clone inbound, а не URL панели. |
| `testing.xray_binary` | Путь к локальному бинарнику Xray. |
| `testing.reality.target` | TLS-цель на стороне сервера в формате `hostname:port`; нужна для REALITY при исходном TLS. |
| `testing.reality.server_names` | Список имён SNI, согласованных с сертификатом цели REALITY. |
| `testing.urls` | URL для HTTP-измерений через локальный SOCKS-прокси. |
| `testing.combination_strategy` | `mutation`, `pairwise` или `exhaustive`; по умолчанию `pairwise`. |
| `testing.max_combinations` | Жёсткий лимит проверяемых конфигураций. |
| `testing.runs_per_combination` | Число повторов каждой конфигурации; 5 позволяет сравнивать средние и стандартное отклонение. |
| `testing.max_failed_runs` | Сколько неудачных повторов допускается до прекращения проверки конфигурации. |
| `output.directory` | Каталог с результатами и checkpoint. |

Для автоматического построения клиентской конфигурации исходный inbound должен
содержать хотя бы одного клиента и использовать `vless`, `vmess`, `trojan` или
`shadowsocks`. Для теста берётся первый клиент из исходного inbound.

Параметр задаёт тип, набор значений, JSON path, target и необязательное условие:

```yaml
parameters:
  network:
    type: enum
    values: [tcp, kcp, ws, grpc, httpupgrade, xhttp]
    mutate: true
    target: inbound
    path: [streamSettings, network]

  mux:
    type: boolean
    values: [true, false]
    target: client
    path: [outbounds, 0, mux, enabled]
```

`target: inbound` изменяет тестовый inbound, а `target: client` — только
локальную конфигурацию Xray. В режиме `existing` тестовый inbound совпадает
с исходным. Условия поддерживают `equals`, `not_equals`, `in`, `not_in`,
`exists`, `all` и `any`.

KCP/mKCP использует UDP, поэтому UDP-трафик на тестовом порту должен быть
разрешён firewall и проброшен до Xray. Для TLS gRPC нужен ALPN `h2`. VLESS
`xtls-rprx-vision` применяется только к TCP с TLS или REALITY; runner удаляет
унаследованный flow при переключении на другой транспорт или режим защиты.
Для REALITY ключи X25519 и `shortId` создаются автоматически один раз на
эксперимент. В `configs/example.yaml` target и SNI оставлены закомментированными:
выберите подходящую для своего сервера цель и проверьте её доступность отдельно.

## Результаты и восстановление

После первого завершённого повтора в `output.directory` появляется
`results.jsonl` (журнал повторов), а `state.json` хранит checkpoint.
При неудачных повторах появляется `errors.jsonl`. По окончании запуска
экспорт создаёт `results.json` при
`formats: [json]`, `results.csv` и `summary.csv` при `formats: [csv]`,
`results.xlsx` при `formats: [xlsx]`. Экспортные файлы пересобираются из
`results.jsonl`.

Перед каждым обычным запуском, включая `--resume`, тестер выполняет короткую
screening-проверку трёх профилей на временном inbound: TCP+TLS, TCP+REALITY и
XHTTP+REALITY (path `/`, mode `auto`). Недоступные по исходным настройкам
профили помечаются как пропущенные. Очищенные результаты записываются в
`diagnostics.jsonl`; неудача контрольной проверки не останавливает основной
план. Ping и измерение скорости в контроль не входят.

`--resume` требует существующий `state.json` в том же `output.directory`.
Используйте прежнюю конфигурацию: runner проверяет подпись параметров и плана,
восстанавливает список завершённых повторов из `results.jsonl` и пропускает
ключи `<configuration-hash>:<run-number>`. При обычном новом запуске выберите
новый каталог результатов, чтобы не смешать журналы экспериментов.
Ключи REALITY и `shortId` хранятся в обычном JSON-файле
`output.directory/.reality-credentials.json` и повторно используются при
`--resume`. Если файл утрачен, возобновление REALITY-эксперимента завершается
ошибкой вместо создания других ключей. Не публикуйте этот файл: ключи не
выводятся в CLI, Web UI, журналы результатов и экспорт.

При штатной остановке и ошибке runner пытается удалить клон или восстановить
исходный inbound в режиме `existing`, затем сохраняет checkpoint и экспорт.
На Windows останавливайте CLI через `Ctrl+C`; на Linux также обрабатывается
`SIGTERM`. После аварийного завершения процесса проверьте панель вручную:
временный клон помечается префиксом `[3xui-tester:` в `remark`, а при
`existing` снимок находится в `source-inbound-backup.json`. Снимок содержит
полный payload, включая потенциально чувствительные данные; на Ubuntu файл
получает права `0600`.

## Безопасность

Тестируйте только панели, inbound и измерительные URL, на которые у вас есть
разрешение. Для throughput укажите собственный разрешённый URL и лимитируйте
`speed_test.max_bytes`.

## Разработка

```bash
uv sync --extra dev
uv run pytest
```

Правила для агентов и карта исходников находятся в [AGENTS.md](AGENTS.md).
