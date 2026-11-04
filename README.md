# NSU LLM Cluster

> **English summary.** A self-hosted LLM inference cluster for a computer class or lab. One Linux "main" server
> (web panel, OpenAI-compatible API at `/v1`, API keys, OIDC login) plus any number of Windows or Linux PCs that run
> llama.cpp (CPU or NVIDIA GPU). A model either runs as full copies on several PCs (`replicas`) or is cut into layers
> across PCs over the LAN (`split`, for models that fit on no single machine). Images are published on GHCR
> (`ghcr.io/denmurzik/nsu-llm-main`, `ghcr.io/denmurzik/nsu-llm-node`), or you can build them yourself. Login works with
> any OIDC provider (Keycloak example below). The documentation is in Russian. MIT license.

Кластер LLM-инференса для компьютерного класса или лаборатории. Один главный сервер на Linux даёт веб-панель и
OpenAI-совместимый API, а ПК класса (Windows или Linux, с видеокартой NVIDIA или без) запускают на себе модели
llama.cpp. Пользователь входит через ваш OIDC-провайдер (корпоративный SSO, Keycloak), выпускает ключ `sk-…` и работает
с моделью в браузере, через `curl`, любой OpenAI SDK или редактор кода. Администратор разворачивает модели с Hugging Face
в панели: ни конфиги править, ни главный сервер перезапускать не нужно.

Проект сделан в НГУ; вход через NSU ID остаётся примером по умолчанию, но портал принимает любого OIDC-провайдера.

## Что вы получаете

- **OpenAI-совместимый API** (`https://<ваш-сервер>/v1`): чат, потоковые ответы, список моделей, лимиты запросов и токенов на ключ.
- **Веб-панель:** каталог моделей, ключи, Playground (чат в браузере), быстрый старт с готовыми примерами, статистика
  использования. Для администратора: развёртывания, ноды, свои файлы моделей, состояние кластера, пользователи.
- **Модели от малых до сотен миллиардов параметров.** Малая модель целиком помещается на один ПК и раскладывается копиями
  (`replicas`: больше ПК, больше запросов в секунду). Большая модель режется по слоям между ПК класса через llama.cpp RPC
  (`split`). Для `split` скорость определяет сеть (на гигабитном Ethernet единицы токенов в секунду), зато модель, которой
  нет места ни на одной машине, всё равно работает.
- **Эксплуатация без ручной работы:** автоскачивание весов с Hugging Face через зеркало на главном (каждый файл
  скачивается из интернета один раз), расписание работы моделей, запасные ПК, Wake-on-LAN для выключенных машин, ежедневные
  резервные копии базы.

```
 пользователи ── HTTPS 443 ──▶ ┌──────────────── главный сервер (Linux, Docker) ───────────────┐
 (браузер, curl, SDK)          │ Caddy ─▶ портал (панель, ключи, OIDC) ─ LiteLLM (/v1) ─ Postgres│
                               │ k3s (управляет нодами), зеркало Hugging Face, реестр образов   │
                               └──────┬───────────────────────────────────────────▲────────────┘
                       запросы к модели│ TCP 18080                присоединение, │ TCP 6443, 443
                                      ▼                                          │ (ноды ходят сами)
        ┌─────────────────────────────────────────────────────────────────────────┴───────┐
        │ ПК класса: pc01   pc02   pc03   …   (Windows+WSL2 или Linux; GPU NVIDIA или CPU)   │
        │ каждый запускает контейнер ноды с llama.cpp; в режиме split ПК обмениваются слоями  │
        │ друг с другом по TCP 18050 (только внутри сети класса)                             │
        └─────────────────────────────────────────────────────────────────────────────────┘
```

Внутри: k3s `v1.36.5+k3s1`, llama.cpp `b12033`, LiteLLM `v1.106.6`, Postgres 17, портал на Python 3.12 (FastAPI),
интерфейс на React.

### Что проверено на реальном железе

- Главный сервер с настоящим OIDC-провайдером (NSU ID): вход, ключи, шлюз, развёртывания, нагрузочный тест на 1000
  одновременных потоков (`docs/verification.md`).
- CPU-ноды на Linux и Windows; Windows 11 с WSL2 и видеокартой RTX 4070 Laptop (8 ГБ): установка `install-node.ps1` по ssh,
  автозапуск после перезагрузки без входа пользователя, Gemma 4 E4B: около 62 токенов/с на запрос, до 473 токенов/с суммарно на 16
  параллельных запросах (для сравнения: старый 4-ядерный CPU даёт около 6 токенов/с).
- `split` проверен на двух машинах с маленькой тестовой моделью; на гигабитной сети с большой моделью на 15 ПК, то есть
  главный сценарий проекта, он **не проверялся**. Главный запускался на Windows под Docker Desktop; `install-main.sh` проверен
  автотестами с подменой команд, но не в чистой установке на новом Linux-хосте: если что-то пойдёт не так, откройте issue.

## Требования

### Главный сервер

| Что | Требование |
|---|---|
| ОС | Linux x86_64. Установщик знает Ubuntu, Debian, RHEL, Rocky, Alma, Fedora; на другом дистрибутиве Docker ставят вручную |
| Docker | Docker Engine; cgroup v2 (на современных дистрибутивах так и есть). Если Docker нет, установщик предложит поставить его из `download.docker.com` |
| CPU и RAM | минимум 2 ядра и 4 ГиБ RAM, рекомендуется 4+ ядра. Для сотен активных пользователей: 8+ ядер, 16 ГБ, отдельная машина (не нода) |
| Диск | не менее 20 ГиБ свободно в каталоге данных Docker, рекомендуется от 50. Зеркало моделей хранит скачанные веса на этом же диске (лимит `HF_MIRROR_MAX_GIB`, по умолчанию 500 ГиБ); под большие модели нужны сотни ГБ |
| GPU | не нужен: главный в инференсе не участвует |
| Порты (входящие) | `443/tcp` (панель и `/v1`), `80/tcp` (отдаёт `ca.crt`), `6443/tcp` (присоединение нод, только из сети класса) |
| Адреса | постоянный IPv4 в сети класса (`MAIN_ADVERTISE_IP`) и имя (`PUBLIC_HOSTNAME`, например `llm.example.edu`), которое пользователи открывают в браузере. Имя нужно в DNS кампуса, а пока его нет, на устройствах пользователей добавляют строку в `hosts` |
| Выход в интернет | `ghcr.io` (образы), `huggingface.co:443` (модели; за прокси: `HTTPS_PROXY` в `.env`), Docker Hub (`postgres`, `redis`, `registry` при первом запуске). Без интернета есть режим airgap, см. `docs/release.md` и «Решение проблем» |
| Нужны утилиты | `curl`; `git` (чтобы склонировать репозиторий) |

Главный можно запустить и на Windows под Docker Desktop (`main/run-main.ps1`), но это путь для проверки и демонстрации, для
класса берите Linux (`docs/deploy-class.md`, «Главный на Docker Desktop»).

### ПК класса (ноды)

| Что | Требование |
|---|---|
| ОС | **Windows 11 22H2 и новее** (основной путь: WSL2 + Docker, автозапуск без входа пользователя). **Windows 10**: только запасной путь через Docker Desktop (нода поднимается после входа пользователя), массовая установка `class-deploy.ps1` такие ПК пропускает. **Linux**: любой с Docker |
| Права | администратор на Windows, root на Linux. В BIOS включена виртуализация (требование WSL2) |
| GPU | необязательно. NVIDIA: драйвер на Windows/Linux **не ниже 570** (образ собран на CUDA 12.8, архитектуры sm_86, sm_89, sm_120); на Linux ещё `nvidia-container-toolkit` 1.20.1 или новее. Драйвер ставится на хост, в WSL его не ставят |
| RAM и диск | чем больше, тем крупнее модель. Под WSL скрипт отдаёт ОЗУ минус 4 ГБ (`-MemoryGB`). Образ ноды и кэш моделей занимают десятки ГБ на диске C: |
| Сеть | до главного: TCP 6443 и 443; от главного: TCP 18080; между нодами: TCP 18050 (для `split`; без шифрования и аутентификации, не выводите за пределы сети класса). Для `split` на больших моделях нужен гигабитный Ethernet по кабелю, Wi-Fi работает, но очень медленно |
| Часы | на ПК и главном идут синхронно (расхождение не больше минуты) |

### OIDC-провайдер

Нужен любой провайдер OpenID Connect с кодовой авторизацией. Требования портала (по коду `portal/app/auth/oidc.py`):

- **confidential-клиент** с client secret; поток authorization code с **PKCE (S256)**;
- id_token подписан **RS256**, `aud` равен `client_id`, `iss` совпадает с `OIDC_ISSUER` (проверяется и сам discovery-документ);
- запрашиваются scope `openid profile email`; из токена берутся `sub`, `email`, `preferred_username`, `name` (или
  `given_name` и `family_name`). Без `email` пользователь войдёт, но администратором стать не сможет;
- провайдер доступен с главного по HTTPS, с сертификатом, которому доверяет контейнер главного (публичный CA).
  Поддержка провайдера со своим внутренним CA в проекте не предусмотрена.

Настройки портала в `.env` главного:

| Переменная | Что это |
|---|---|
| `OIDC_ISSUER` | адрес issuer, например `https://sso.example.edu/realms/campus`. По умолчанию (если не задать) стоит NSU ID: **замените** |
| `OIDC_CLIENT_ID` | идентификатор клиента. По умолчанию `double.fresh.nsuts.ru`: **замените** |
| `OIDC_CLIENT_SECRET` | секрет клиента, обязателен (без него главный не стартует); без пробелов и кавычек |
| `ADMIN_EMAILS` | адреса администраторов через запятую (регистр не важен); совпавший `email` даёт роль администратора |
| `ADMIN_REQUIRE_EMAIL_VERIFIED` | `true`: админом становится только тот, у кого `email_verified` не `false`; `false`: решает только `ADMIN_EMAILS`. В `.env.example` стоит `false`, потому что NSU ID не присылает `email_verified=true`. Если ваш провайдер подтверждает почту, поставьте `true`: иначе тот, кто сумеет зарегистрироваться с чужим адресом администратора, получит его права |
| `OIDC_REDIRECT_URI`, `OIDC_POST_LOGOUT_REDIRECT_URI` | нужны, только если домен главного не тот, что зарегистрирован у клиента (мост через чужой фронт, `docs/deploy-class.md`). Обычно пусто |

#### Пример: клиент в Keycloak

В консоли администратора Keycloak, нужный realm, **Clients → Create client**:

1. **Client type:** OpenID Connect. **Client ID:** например `nsu-llm-cluster` (это `OIDC_CLIENT_ID`).
2. **Client authentication:** On (confidential). **Authentication flow:** оставьте только **Standard flow**.
3. **Valid redirect URIs:** точно `https://<PUBLIC_HOSTNAME>/auth/callback`, например `https://llm.example.edu/auth/callback`.
4. **Valid post logout redirect URIs:** `https://<PUBLIC_HOSTNAME>/`.
5. На вкладке **Advanced** можно задать **Proof Key for Code Exchange Code Challenge Method = S256**: портал всегда его присылает.
6. На вкладке **Credentials** скопируйте **Client secret**: это `OIDC_CLIENT_SECRET`.
7. `OIDC_ISSUER`: `https://<keycloak>/realms/<realm>` (у старых Keycloak и в установках с префиксом `/auth`:
   `https://<keycloak>/auth/realms/<realm>`). Проверьте: `curl -s <OIDC_ISSUER>/.well-known/openid-configuration`, поле
   `issuer` в ответе должно совпасть с `OIDC_ISSUER` символ в символ (без завершающего `/`).

Тот же набор (confidential-клиент, redirect URI `/auth/callback`, post-logout `/`) переносится на любой другой провайдер.

## Образы

Образы собираются из этого репозитория и публикуются в GitHub Container Registry (публичные пакеты, токен для скачивания не нужен):

| Образ | Размер (сжатый) | Кто его берёт |
|---|---|---|
| `ghcr.io/denmurzik/nsu-llm-main:0.1.0` | ~190 МБ | `install-main.sh` на главном |
| `ghcr.io/denmurzik/nsu-llm-node:0.1.0-cpu` | ~120 МБ | главный скачивает его один раз и отдаёт ПК класса по локальной сети |
| `ghcr.io/denmurzik/nsu-llm-node:0.1.0-gpu` | ~2,5 ГБ | то же (CUDA 12.8) |

Как это работает: `install-main.sh` с `IMAGE_SOURCE=pull` скачивает образ главного, запускает его, затем кладёт образы
нод в собственный реестр главного (`https://<главный>/v2/`, только чтение, только из частных подсетей), и ПК класса берут
образ оттуда, а не из интернета. Версия образов равна версии скриптов репозитория (`0.1.0`), поэтому выполняйте
`git checkout v0.1.0` (или тег нужной версии). Подробности: `docs/release.md`.

> **Первые публичные образы появятся вместе с первым релизом `v0.1.0`.** Пока тег и пакеты не опубликованы, используйте
> сборку из исходников (ниже): установщик по умолчанию делает именно её.

### Сборка из исходников

Образ главного установщик на Linux соберёт сам (`IMAGE_SOURCE=build`, значение по умолчанию, если рядом лежит
`main/Dockerfile`). Образы нод собираются вручную; сборка llama.cpp тяжёлая по CPU и памяти, снижайте `JOBS`, если машина
слабая, а образ `gpu` лучше собирать на сервере:

```bash
docker build --provenance=false -f main/Dockerfile -t nsu-llm-main:0.1.0 .
docker build --provenance=false -f node/Dockerfile --target cpu --build-arg JOBS=4  -t nsu-llm-node:0.1.0-cpu node
docker build --provenance=false -f node/Dockerfile --target gpu --build-arg JOBS=16 -t nsu-llm-node:0.1.0-gpu node
```

Образы нод в этом случае не попадают в реестр главного. Доставьте их на ПК архивом (`-ImageTar` в `install-node.ps1`, на
Linux `docker load`, шаг «Распространение образов» в `docs/deploy-class.md`):

```bash
docker save --platform linux/amd64 nsu-llm-node:0.1.0-gpu | gzip > nsu-llm-node-0.1.0-gpu.tar.gz
docker save --platform linux/amd64 nsu-llm-node:0.1.0-cpu | gzip > nsu-llm-node-0.1.0-cpu.tar.gz
```

**Свои образы в GHCR.** Если вы хотите, как мы, раздавать образы с главного, а не архивами, соберите и выложите их под своим
аккаунтом скриптом `release/release.sh` (собирает три образа, выкладывает в `ghcr.io/<ваш-аккаунт>/…`, пишет
`release/<версия>.json`, ставит локальный git-тег `v<версия>`):

```bash
mkdir -p ~/.config/nsu-llm && chmod 700 ~/.config/nsu-llm
umask 077 && cat > ~/.config/nsu-llm/ghcr-token      # токен GitHub (classic) с правом write:packages, Ctrl-D
GHCR_OWNER=<ваш-аккаунт> release/release.sh --dry-run 0.1.0     # посмотреть шаги, ничего не меняя
GHCR_OWNER=<ваш-аккаунт> release/release.sh 0.1.0
```

Скрипту нужно чистое дерево git и версия, совпадающая с версией скриптов (`0.1.0`). Токен читается из файла (права строго
0600) и не попадает в командную строку. После первой выкладки пакеты на GitHub приватные: сделайте их Public в настройках
пакета (или передайте установщику `GHCR_TOKEN_FILE`). Установка своих образов: `IMAGE_SOURCE=pull` и `GHCR_OWNER=<ваш-аккаунт>`.

## Быстрый старт

Команды выполняются из корня клонированного репозитория:

```bash
git clone https://github.com/Denmurzik/nsu-llm-cluster.git
cd nsu-llm-cluster
git checkout v0.1.0        # после выхода релиза; до него остаётесь на main и собираете образы сами
```

### 1. Главный сервер

Сначала зарегистрируйте OIDC-клиент (раздел выше) и узнайте IP сервера.

#### Без вопросов

Файл ответов (рекомендуется: все значения на виду, команду можно повторять):

```bash
install -m 600 /dev/null answers.env      # файл с правами 600: в нём секрет
nano answers.env
```

```ini
PUBLIC_HOSTNAME=llm.example.edu
MAIN_ADVERTISE_IP=10.0.0.5
OIDC_ISSUER=https://sso.example.edu/realms/campus
OIDC_CLIENT_ID=nsu-llm-cluster
OIDC_CLIENT_SECRET=<секрет клиента>
ADMIN_EMAILS=admin@example.edu
ADMIN_REQUIRE_EMAIL_VERIFIED=true
CERT_MODE=internal
INSTALL_DOCKER=yes
# образы: с GHCR (после выхода релиза) или собрать из исходников
IMAGE_SOURCE=pull
GHCR_OWNER=denmurzik
# IMAGE_SOURCE=build
```

```bash
sudo ./main/install-main.sh --dry-run --yes --answers answers.env    # проверить хост и план, ничего не меняя
sudo ./main/install-main.sh --yes --answers answers.env
```

**Осторожно с умолчаниями.** Если не указать `PUBLIC_HOSTNAME`, `OIDC_ISSUER` или `OIDC_CLIENT_ID`, установщик молча возьмёт
значения НГУ (`double.fresh.nsuts.ru`, `sso-ng.nsu.ru`). Интерактивный мастер (`sudo ./main/install-main.sh` без параметров)
спрашивает имя хоста, LAN-адрес, секрет, администраторов, сертификат, airgap, брандмауэр и источник образа, но **не спрашивает
`OIDC_ISSUER` и `OIDC_CLIENT_ID`**: для него сначала `cp .env.example .env` и поправьте эти две строки (и
`ADMIN_REQUIRE_EMAIL_VERIFIED`) вручную; значения, которые уже есть в `.env`, мастер не переспрашивает.

Что делает установщик: проверяет ОС, Docker, cgroup v2, свободные порты 80/443/6443, диск, RAM, CPU, LAN-адрес; пишет `.env`
(права 600) и свои ответы в `install-main.env`; при активных `ufw` или `firewalld` открывает 80, 443, 6443; готовит образ;
запускает `main/run-main.sh`; ждёт `/readyz` (по умолчанию до 900 с, `--timeout SEC`); в конце печатает адрес панели, ссылку на
`ca.crt`, токен подключения нод и готовые команды для нод. Первый запуск занимает 2–3 минуты.

Повторный запуск той же команды обновляет образ и пересоздаёт контейнер на тех же томах: база, ключи и секреты сохраняются.
Секрет не передавайте в командной строке (он остаётся в истории shell): кладите в файл ответов, вводите по запросу или
передавайте окружением `sudo --preserve-env=OIDC_CLIENT_SECRET`.

Проверка:

```bash
curl -sk https://<MAIN_IP>/readyz        # {"db":true,"litellm":true}
```

Другие режимы установщика: `--dry-run` (только план), `--image-tar FILE` (образ главного из архива `docker save`),
`--env-file FILE` и `--config FILE` (другие пути к `.env` и сохранённым ответам), `--uninstall`, `--update VERSION`.

**Главный на Windows (Docker Desktop)** для проверки и демонстрации: `install-main.sh` на Windows не работает, контейнер
запускает низкоуровневый скрипт, образ предварительно собирают или загружают:

```powershell
Copy-Item .env.example .env      # заполните PUBLIC_HOSTNAME, OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, ADMIN_EMAILS
powershell -ExecutionPolicy Bypass -File .\main\run-main.ps1 -EnvFile .env
```

Параметры: `-AdvertiseIp`, `-Airgap`, `-Certs`, `-Image`, `-PrintToken`, `-Uninstall`, `-DryRun`. Docker Desktop должен иметь не менее
4 ГиБ памяти (`memory=8GB` в `%UserProfile%\.wslconfig` и `wsl --shutdown`). Особенности (hairpin, Hyper-V firewall): `docs/deploy-class.md`.

**Сертификат.** По умолчанию (`CERT_MODE=internal`) главный сам выпускает сертификат на имя и IP из собственного CA. Чтобы
браузеры и клиенты ему верили, установите корень на устройствах пользователей (ПК класса и ноды делают это сами, см. ниже):

```bash
curl -o ca.crt http://<MAIN_IP>/ca.crt
sudo cp ca.crt /usr/local/share/ca-certificates/nsu-llm.crt && sudo update-ca-certificates     # Linux
```

```powershell
certutil -addstore -f Root ca.crt        # Windows, PowerShell от администратора
```

Если у вас есть настоящий сертификат на `PUBLIC_HOSTNAME` (например, от центра сертификации вуза), используйте
`CERT_MODE=own` и `CERTS_DIR=/путь` с файлами `tls.crt` (полная цепочка) и `tls.key`: тогда ничего раздавать не нужно, а
ПК класса обращаются к главному по имени (`-Main llm.example.edu`).

### 2. Первый вход администратором

1. Убедитесь, что `PUBLIC_HOSTNAME` ведёт на IP главного (запись в DNS или строка в `hosts`) и корень CA установлен.
2. Откройте `https://<PUBLIC_HOSTNAME>/` и войдите через ваш OIDC-провайдер. Адрес из `ADMIN_EMAILS` получает разделы
   администратора: «Развёртывания», «Ноды», «Мои модели», «Состояние», «Пользователи».
3. Если вы вошли обычным пользователем: проверьте `ADMIN_EMAILS` (точный адрес, как в токене), `ADMIN_REQUIRE_EMAIL_VERIFIED`
   и что провайдер присылает claim `email`. После правки `.env` повторите `sudo ./main/install-main.sh --yes ...`.

### 3. Первая нода

Токен подключения нод показан в конце установки, на странице «Ноды» в панели и командой `./main/run-main.sh --print-token`.
Храните его как пароль.

**Windows 11** (PowerShell **от администратора**; скопируйте на ПК каталог `node` репозитория целиком, скрипты зависят друг от друга):

```powershell
powershell -ExecutionPolicy Bypass -File .\node\install-node.ps1 -DryRun -Main <MAIN_IP> -Token <ТОКЕН>   # посмотреть план
powershell -ExecutionPolicy Bypass -File .\node\install-node.ps1 -Main <MAIN_IP> -Token <ТОКЕН>
```

Скрипт сам определяет Windows, GPU и драйвер, ставит WSL2 с отдельным дистрибутивом Ubuntu 24.04, Docker CE и (для GPU)
NVIDIA Container Toolkit, пишет `%UserProfile%\.wslconfig`, выключает сон и гибернацию, создаёт правила брандмауэра и задачу
автозапуска без входа пользователя, включает Wake-on-LAN, скачивает образ ноды с главного (если нужен архив: `-ImageTar`) и
запускает ноду. Он спрашивает только недостающее; пароль нужен для задачи автозапуска. Если WSL ставится впервые, скрипт просит
перезагрузить ПК (код 3010): после перезагрузки запустите ту же команду. Повторные запуски безопасны. Состояние: `.\install-node.ps1 -Status`
(после установки копия лежит в `C:\ProgramData\NSU-LLM\scripts\`).

Классу шире /24 или в другой подсети разрешите доступ явно: `-AllowFrom 10.1.0.0/22,10.0.0.5` (подсети класса и адрес главного).

**Windows 10** (запасной путь через Docker Desktop; он должен быть установлен и запущен):
`.\node\install-node.ps1 -Docker desktop -Main <MAIN_IP> -Token <ТОКЕН>`. Нода поднимается только после входа
пользователя в Windows. Образ из реестра главного Docker Desktop скачает, только если Windows доверяет сертификату главного
(установите `ca.crt` в доверенные корни), иначе используйте `-ImageTar`.

**Linux** (нужен Docker, root; образ с главного):

```bash
sudo ./node/run-node.sh --main <MAIN_IP> --token <ТОКЕН> --from-main            # GPU определяется по nvidia-smi
sudo ./node/run-node.sh --main <MAIN_IP> --token <ТОКЕН> --from-main --accel cpu
sudo ./node/run-node.sh --main <MAIN_IP> --token <ТОКЕН> --mode standalone      # без Kubernetes (только replicas)
```

Через несколько секунд нода появится в панели на странице «Ноды». Если нет, на ноде запустите `.\node\diag.ps1`
(Windows) и `bash main/diag.sh` на главном: они проверяют порты, DNS, сертификат и брандмауэр и печатают готовые команды
исправления.

### 4. Весь класс сразу: `class-deploy.ps1`

С ПК администратора (Windows PowerShell 5.1, репозиторий склонирован на нём) скрипт по WinRM ставит, обновляет и удаляет
ноды на всех ПК и показывает состояние таблицей. На ПК класса должны быть: Windows 11 22H2+, включённый WinRM
(`Enable-PSRemoting -Force -SkipNetworkProfileCheck`), учётная запись администратора с паролем.

```powershell
cd node
.\class-deploy.ps1 -Action check                                   # какие ПК достижимы и что включить на остальных
.\class-deploy.ps1 -Action install -Only pc01 -Main <MAIN_IP> -Token <ТОКЕН>   # сначала один ПК до конца
.\class-deploy.ps1 -Action install -Main <MAIN_IP> -Token <ТОКЕН>              # остальные
.\class-deploy.ps1 -Action status                                  # состояние всех нод
```

Список ПК лежит в `class-nodes.csv` рядом с запуском (если файла нет, мастер спросит адреса и создаст его):

```csv
name,address,mode,accel,docker
pc01,10.1.0.101,k3s,auto,wsl
pc02,10.1.0.102,k3s,auto,wsl
```

Скрипт без архивов раздаёт ПК команду скачать образ с главного, поэтому главный должен быть установлен с `IMAGE_SOURCE=pull`.
Если вы собирали образы сами, передайте `-ImageTarCpu` и `-ImageTarGpu` (и `-ImageSource tar`). Подробности про WinRM,
TrustedHosts, журналы и коды возврата: `docs/deploy-class.md`, «Массовая установка на 15 ПК».

### 5. Первая модель

1. Первое развёртывание `gemma-4-e4b` (около 5 ГБ) создаётся автоматически из значений `DEFAULT_*` в `.env` и само
   занимает свободные ноды. Смотрите «Администрирование → Развёртывания»: колонка готовности показывает «Скачивание X %»,
   «Загрузка весов», «Готов». Модель сначала скачивается на главный (один раз), затем на ноды.
2. Своя модель: «Развёртывания → создать». Укажите репозиторий Hugging Face и файл GGUF (например, `unsloth/gemma-4-E4B-it-GGUF`
   и `gemma-4-E4B-it-Q4_K_M.gguf`), режим, ускоритель, **контекст на запрос** и **параллельность**. Мастер показывает оценку памяти
   и предупреждает, если модель не поместится. Нужен репозиторий в формате GGUF; для закрытых репозиториев задайте `HF_TOKEN` в `.env`.
3. **Какой режим выбрать.**
   - `replicas`: на каждой назначенной ноде работает полная копия модели, запросы балансируются между копиями. Берите, когда
     модель помещается на один ПК (в видеопамять GPU или в RAM). Это основной и самый быстрый режим.
   - `split`: модель режется по слоям между группой из `group_size` ПК. Берите, когда она не помещается ни на один ПК.
     Скорость ограничивают сеть и самая слабая нода; работает только на нодах k3s и движке llama.cpp. Запасные ноды,
     паузу пересборки и расписание описывает раздел «Split в эксплуатации» в `docs/deploy-class.md`.
4. Память: контекст на запрос × параллельность задаёт размер KV-кэша (серверу уходит их произведение). Для малой видеокарты
   начните с 8192 × 2.

### 6. Ключ и вызовы API

В панели: «Ключи → создать», значение показывается один раз. Ключ действует на все модели, лимиты задают `KEY_RPM_LIMIT`
(60 запросов/мин) и `KEY_TPM_LIMIT` (100 000 токенов/мин), число ключей на пользователя: `MAX_KEYS_PER_USER` (5). Удалённый ключ
перестаёт работать в течение ~15 секунд. Имя модели в запросе равно имени развёртывания.

```bash
export NSU_LLM_KEY=sk-...
curl https://<PUBLIC_HOSTNAME>/v1/chat/completions \
  -H "Authorization: Bearer $NSU_LLM_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gemma-4-e4b", "messages": [{"role": "user", "content": "Привет"}], "stream": true}'
```

```python
import os
from openai import OpenAI

client = OpenAI(base_url="https://<PUBLIC_HOSTNAME>/v1", api_key=os.environ["NSU_LLM_KEY"])
reply = client.chat.completions.create(model="gemma-4-e4b", messages=[{"role": "user", "content": "Привет"}])
print(reply.choices[0].message.content)
```

Если корень CA не установлен в систему: `curl --cacert ca.crt …`, для Python-клиента `SSL_CERT_FILE=ca.crt`.

**Редактор кода opencode** (файл `~/.config/opencode/opencode.json`; ключ берётся из переменной окружения, а не пишется в файл):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "nsu-llm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "NSU LLM",
      "options": {
        "baseURL": "https://<PUBLIC_HOSTNAME>/v1",
        "apiKey": "{env:NSU_LLM_KEY}"
      },
      "models": {
        "gemma-4-e4b": { "name": "Gemma 4 E4B" }
      }
    }
  }
}
```

Точный формат конфигурации смотрите в документации opencode. Готовые примеры для curl, OpenAI SDK и других клиентов с вашим адресом
показывает и страница «Быстрый старт» в панели.

## Эксплуатация

### Split в эксплуатации

Задаётся на странице развёртывания («Развёртывания» → изменить); запас, пауза и расписание подов не перезапускают.

- **Запасные ноды (`spare_nodes`, 0–100).** Столько назначенных нод не входят в группы и ждут замены. Когда нода группы выходит,
  её место сразу занимает запасная того же ускорителя. Групп собирается `(назначено − запас) / group_size`.
- **Пауза перед пересборкой (`regroup_grace_s`).** Пока запасной ноды нет, потерянная нода держит место в группе это время.
  По умолчанию `SPLIT_REGROUP_GRACE_S` = 600 с: перезагрузка одного ПК не должна заставлять собирать модель на 15 нодах заново, а
  загрузка идёт часами. Для `replicas` пауза `NODE_NOTREADY_GRACE_S` = 120 с.
- **Расписание (`schedule`).** Рабочее окно: дни недели и часы (часовой пояс по Новосибирску, UTC+7). Вне окна бэкенды
  уходят из шлюза, поды живут ещё `SCHEDULE_DRAIN_S` (60 с), ноды отдаются другому развёртыванию с auto-assign. Без расписания
  развёртывание работает круглосуточно.
- **Обслуживание ноды** («Ноды» → «На обслуживании»): нода ничего не запускает и не назначается автоматически, но сохраняет
  назначение; группа split пересобирается сразу.
- **Доля RAM ноды (`ram_budget_pct`, 10–90 %, по умолчанию 70) и `use_node_ram`.** Сколько памяти WSL нода отдаёт слоям в RAM и
  отдаёт ли вообще; слои в RAM считает процессор, это заметно медленнее видеокарты.
- **Предел устройств:** до 63 на группу (лидер собран на 64); нода с GPU и RAM даёт два устройства.
- Значения по умолчанию задают переменные `.env`; `SPLIT_REGROUP_GRACE_S`, `NODE_NOTREADY_GRACE_S`, `SCHEDULE_DRAIN_S` и
  `GPU_COMPUTE_RESERVE_MIB` портал читает при старте (для смены перезапустите главный).

Диск лидера, оценка памяти и сбои: `docs/deploy-class.md`, «Большая модель в split».

### Настройки под нагрузку

Значения по умолчанию подходят для класса; для порядка 1000 активных пользователей см. `docs/research/parallelism.md` и
`docs/verification.md` (там же результаты нагрузочного теста). Менять стоит по результатам своего теста.

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `LITELLM_WORKERS` | 4 | процессы шлюза LiteLLM (1–16) |
| `PORTAL_WORKERS` | 4 | процессы API портала (1–4) |
| `QUEUE_FACTOR` | 2 | очередь бэкенда `parallel × QUEUE_FACTOR`, сверх неё шлюз отвечает 429 с `Retry-After` |

### Журналы

Журналы supervisord, Caddy, портала, контроллеров и k3s идут в stdout контейнера: `docker logs --tail 200 llm-main` (Docker ограничивает
их 5 файлами по 50 МБ). Журналы подов (LiteLLM, зеркало, модели): `docker exec llm-main k3s kubectl -n llm logs deploy/<имя>`.
Страница «Состояние» в панели показывает ноды, развёртывания, ответы 429 и 5xx шлюза, GPU главного и возраст последней
резервной копии.

### Свои файлы моделей

Не только Hugging Face: GGUF-файл можно загрузить в панели («Мои модели → Загрузка файла»: частями, с паузой и докачкой) или
положить в каталог тома главного. Такая модель выбирается в мастере на вкладке «Свои файлы». Модель, которой нет в интернете
или у главного нет выхода туда, кладут и в каталог `offline/`. Подробнее: `docs/local-models.md`, `docs/hf-mirror.md`.

```bash
docker cp ./my-model-Q4_K_M.gguf llm-main:/var/lib/llm-hf/models/     # портал сам найдёт и проверит файл
```

### Wake-on-LAN

Выключенный ПК, который нужен развёртыванию, портал будит сам (magic packet, повторы с нарастающей паузой); кнопка «Разбудить»
на странице «Ноды» шлёт пакет сразу. `install-node.ps1` включает на ПК нужные настройки карты и отключает быстрый запуск Windows.
Нужно ещё включить Wake-on-LAN в BIOS, разрешить маршрутизаторам кампуса directed broadcast в подсеть класса и держать ПК на кабеле
(по Wi-Fi из выключенного состояния ПК обычно не просыпается). Настройки: `WOL_*` в `.env.example`, подробности в
`docs/deploy-class.md`, «Автопробуждение ПК класса».

### Обновление

```bash
# образы новой версии скачиваются заранее, главный продолжает работать (нужен IMAGE_SOURCE=pull)
sudo ./main/install-main.sh --update 0.2.0
# переход: короткий перерыв ~30 с, данные сохраняются
git fetch --tags && git checkout v0.2.0
sudo ./main/install-main.sh --yes
```

При `IMAGE_SOURCE=build` обновление это `git pull` и повторный запуск `sudo ./main/install-main.sh --yes` (образ пересоберётся).
Ноды после обновления главного: Windows: `.\node\class-deploy.ps1 -Action update` (или на отдельном ПК `install-node.ps1 -ImageSource main`);
Linux: повторите `run-node.sh ... --from-main`. Порядок и тонкости (смена `BACKEND_API_KEY`, токен агента): `docs/release.md` и
`docs/deploy-class.md`, «Обновление».

### Резервные копии

Задание `postgres-backup` ежедневно (по умолчанию 04:17 по Новосибирску: `BACKUP_SCHEDULE` в UTC, cron) снимает дампы баз портала и
LiteLLM в `/var/lib/llm/backups` тома `llm-main-data`; хранится `BACKUP_KEEP` наборов (14).

```bash
docker exec llm-main k3s kubectl -n llm create job --from=cronjob/postgres-backup backup-now    # копия прямо сейчас
docker exec llm-main /opt/llm/main/backup/pg-restore.sh --list                                  # какие копии есть
docker exec -it llm-main /opt/llm/main/backup/pg-restore.sh latest                              # восстановить
docker cp llm-main:/var/lib/llm/backups ./backups                                               # копия на другой диск
```

**Отдельно сохраните `secrets.env`** (`docker exec llm-main cat /var/lib/llm/secrets.env`) и храните не рядом с копиями: без него
LiteLLM не расшифрует сохранённые модели. Файлы лежат на том же диске, что и база: периодически копируйте их на другую машину.
Восстановление `pg-restore.sh` проверяет набор, загружает базы во временные, останавливает LiteLLM и портал, снимает страховочную
копию текущих баз, меняет базы местами и запускает всё обратно; при сбое возвращает прежние базы. Нужно место под вторую копию баз.
Установка на новом месте: положите `secrets.env` в том `llm-main-data` до первого запуска, запустите главный, скопируйте файлы
`*.dump` в `/var/lib/llm/backups` и выполните `pg-restore.sh latest`.

### Удаление

```bash
sudo ./main/install-main.sh --uninstall      # УДАЛЯЕТ контейнер и тома: базу, ключи, секреты, кэш моделей (с подтверждением)
```

```powershell
# Windows-нода (с самого ПК; удаляет ноду из кластера, WSL-дистрибутив NSU-LLM, задачи, правила брандмауэра)
powershell -ExecutionPolicy Bypass -File C:\ProgramData\NSU-LLM\scripts\install-node.ps1 -Uninstall
.\node\class-deploy.ps1 -Action uninstall -Only pc15      # или с ПК администратора
```

```bash
sudo ./node/run-node.sh --uninstall --main <MAIN_IP> --name "$(hostname)" --token <ТОКЕН>      # Linux-нода
```

## Решение проблем

Первым делом: `bash main/diag.sh` на главном, `.\node\diag.ps1` и `.\node\install-node.ps1 -Status` на ПК класса. Они ничего
не меняют и печатают команду исправления для каждого провала. Журнал главного: `docker logs --tail 200 llm-main`; поды:
`docker exec llm-main k3s kubectl -n llm get pods`.

- **Брандмауэр и `ufw` на главном.** Порты `80`, `443`, `6443`, опубликованные Docker, обходят правила `ufw` и `firewalld`:
  отсутствие правила не значит, что порт закрыт, и наоборот, `ufw allow` их не ограничивает. Ограничения по адресам ставят в цепочку
  `DOCKER-USER` (пример в `docs/deploy-class.md`, «Брандмауэр главного»). `install-main.sh` с `FIREWALL=yes` лишь открывает три порта.
- **«Нет hairpin» в логе главного** (`docker logs llm-main | grep -i hairpin`). При старте главный проверяет, достаёт ли
  контейнер до собственного LAN-адреса (`MAIN_ADVERTISE_IP:6443`). Если нет, ноды всё равно присоединяются, но `kubectl logs/exec` и
  логи подов на других машинах недоступны. Частая причина на Linux с `ufw`: трафик из контейнера (интерфейс `docker0`) к
  адресу самого хоста режется; разрешите его (например `sudo ufw allow in on docker0`) и перезапустите главный. Адрес
  выбирается один раз при старте, поэтому после любого изменения сети **перезапустите главный** (`sudo ./main/install-main.sh --yes`).
  Проверяйте также, что `MAIN_ADVERTISE_IP` это адрес физического интерфейса, а не VPN или виртуального адаптера.
- **Windows 10.** Основной путь (WSL2 в режиме mirrored, правило Hyper-V firewall) требует Windows 11 22H2 (сборка 22621). На
  Windows 10 используйте `-Docker desktop`; нода стартует только после входа пользователя, а `class-deploy.ps1` такой ПК
  пропускает («Windows сборки …: нужен Windows 11 22H2»). Образ из реестра главного Docker Desktop скачает, только если Windows
  доверяет сертификату главного.
- **GPU не видна.** Проверьте драйвер на хосте (`nvidia-smi`, версия не ниже 570). Windows: в WSL
  `wsl -d NSU-LLM -u root -e /usr/lib/wsl/lib/nvidia-smi -L`; `install-node.ps1 -Status`; при `-Accel auto` скрипт при проблеме с драйвером
  ставит ноду как cpu и пишет причину, `-Accel nvidia` в этом случае останавливает установку. Linux:
  `docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi` и `nvidia-container-toolkit` 1.20.1+. Если ускоритель
  ноды менялся (поставили драйвер или карту), нода откажется стартовать с сообщением «Метки ноды изменились»: выполните
  `--uninstall` (`-Uninstall`) и установите заново. Лестница проверки GPU: `docs/deploy-class.md`.
- **Порты.** Нужны: нода → главный `6443`, `443`; главный → нода `18080`; нода ↔ нода `18050` (только `split`). Правила на ПК
  Windows: `NSU-LLM-Node` (создаёт скрипт); при WSL2 mirrored ещё правило Hyper-V firewall (его команду печатает `node\diag.ps1`).
  Входящее правило Windows «Блокировать 443» сильнее разрешающих и ломает вход пользователей и standalone-ноды. Реестр образов
  `/v2/` отвечает 403, если подсеть класса не из частных диапазонов: задайте `REGISTRY_ALLOW_FROM` в `.env`
  (по умолчанию `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1`).
- **Клиенты не доверяют сертификату.** Установите `ca.crt` (раздел «Сертификат»), либо `curl --cacert ca.crt`, `SSL_CERT_FILE=ca.crt`.
  На многих ПК раздайте корень групповой политикой (`docs/deploy-class.md`, «Сертификат»); перед импортом сверьте отпечаток
  (`openssl x509 -in ca.crt -noout -fingerprint -sha256`). Ноды скачивают корень сами при первом запуске. Пересоздание тома
  `llm-main-data` выпускает новый CA, его придётся раздать заново.
- **Вход возвращает на чужой адрес или «redirect_uri mismatch».** `PUBLIC_HOSTNAME` должен совпадать с зарегистрированным у клиента
  адресом `https://<PUBLIC_HOSTNAME>/auth/callback`; открывать панель нужно по этому имени.
- **Вход проходит, но нет разделов администратора.** Проверьте `ADMIN_EMAILS` (адрес из токена), `ADMIN_REQUIRE_EMAIL_VERIFIED` и
  claim `email`. «Не удалось войти»: `docker logs llm-main` покажет причину (discovery, issuer, подпись).
- **Модель долго «не готова».** Смотрите стадию в колонке готовности. Лог зеркала:
  `docker exec llm-main k3s kubectl -n llm logs deploy/hf-mirror`. Главный должен достучаться до `huggingface.co:443` (прокси:
  `HTTPS_PROXY` в `.env`) или файлы должны лежать в томе вручную. Если ПК класса не резолвят `PUBLIC_HOSTNAME` (нет DNS), задайте
  `HF_MIRROR_URL=https://<MAIN_IP>/hf`.
- **Нода пересоздана и не присоединяется («Node password rejected»).** Выполните `--uninstall` (`-Uninstall`) с тем же именем и
  запустите заново.
- **Диск Windows-ноды.** Образ и модели лежат в виртуальном диске WSL на C:. Для большого `split` место на ноде-лидере
  `размер модели + RPC_CACHE_MAX_GIB`; `df` внутри WSL показывает виртуальный диск, а не свободное место C:, поэтому
  `RPC_CACHE_MAX_GIB` задавайте по реальному остатку (`Get-PSDrive C`).
- **Главный без интернета.** `sudo ./main/install-main.sh` с `AIRGAP=yes` кладёт заранее системные образы k3s, Postgres, Redis
  и LiteLLM; модели кладут в том зеркала руками (`docs/hf-mirror.md`, «Том и офлайн-режим»). `ghcr.io` недоступен (прокси, 403):
  `docker save` образа на другой машине и `IMAGE_SOURCE=load` с `--image-tar nsu-llm-main.tar` (`docs/release.md`, раздел 4).
- **Главный на Docker Desktop: «WSL не хватает памяти».** В `%UserProfile%\.wslconfig` задайте `[wsl2]` и `memory=8GB`, выполните `wsl --shutdown`.
- **Часы расходятся.** Включите службу времени Windows (`w32time`) и chrony или `systemd-timesyncd` на главном: иначе пересборка
  групп `split` после перезагрузки ПК может не сработать.

## Документация

| Файл | О чём |
|---|---|
| `docs/deploy-class.md` | сеть, DNS, сертификаты, Windows-ноды, WinRM, Wake-on-LAN, split на больших моделях, обновление |
| `docs/release.md` | образы в GHCR, реестр главного, обновление версий, работа без интернета |
| `docs/hf-mirror.md` | зеркало Hugging Face, кэши, офлайн-режим, долгий старт больших моделей |
| `docs/local-models.md` | свои файлы GGUF |
| `docs/verification.md` | журнал проверок и замеры на реальном железе |
| `docs/research/` | исследования: параллелизм, большие модели, k3s в Docker, LiteLLM |
| `node/README.md` | образ ноды: переменные окружения, режим standalone, параметры `run-node.*` |
| `e2e/README.md` | `e2e/smoke.py` и `e2e/admin.py`: проверка работающего стенда по HTTP |

## Структура репозитория

| Каталог | Содержимое |
|---|---|
| `main/` | образ главного сервера, entrypoint, манифесты k3s, `install-main.sh`, `run-main.sh`, `run-main.ps1`, `diag.sh` |
| `node/` | образ ноды (llama.cpp, k3s agent, standalone), `run-node.sh`, `run-node.ps1`, `install-node.ps1`, `class-deploy.ps1`, `diag.ps1` |
| `portal/` | FastAPI: OIDC-вход, ключи, каталог, админка, контроллеры развёртываний |
| `web/` | интерфейс на React |
| `release/` | `release.sh`: сборка и выкладка образов в GHCR |
| `e2e/`, `loadtest/` | проверка по HTTP и нагрузочные тесты |
| `deploy/` | вспомогательные файлы для фронт-прокси перед главным |

## Лицензия

MIT, см. `LICENSE`.
