# TensorMedia

**Version 1.2.3** · 🌍 Language: [English](#english) | [Русский](#русский)

---

<a id="english"></a>
## 🇬🇧 English

TensorMedia is a cross-platform desktop app that finds and clears visual duplicates and near-duplicates in large photo/video libraries using an on-device ML engine. The deployment architecture guarantees zero-dependency execution across Windows, macOS, and Linux.

### ✨ Features
- **Visual near-duplicate detection** — SigLIP image embeddings + FAISS clustering group visually similar/duplicate photos and video frames at an adjustable similarity threshold.
- **Face grouping (biometric)** — FaceNet (InceptionResnetV1) embeddings cluster images by the people in them.
- **Side-by-side compare** — a carousel/grid Compare view for images and video to decide which copies to keep.
- **Smart auto-selection** — one click marks duplicates for removal via a Pareto cascade (resolution → size → recency), always keeping the best copy.
- **Safe cleanup** — move to Trash or delete permanently; reactive filesystem watching keeps the index in sync.
- **Accelerated & offline** — Apple MPS / CPU inference; macOS & Windows builds bundle the model weights and run fully offline.

### 🚀 Installation & Deployment

The application is distributed as self-contained artifacts via GitHub Actions. No Python environment or external library installation is required.

#### Windows (x86-64)
1. Open the repository's **[Releases](../../releases/latest)** page.
2. Download `TensorMedia_Setup.exe`.
3. Run the installer and follow the wizard. A desktop shortcut is created automatically.
*Note: Microsoft Defender SmartScreen may warn about an unsigned binary. Click "More info" -> "Run anyway".*

#### macOS (Apple Silicon / Intel)
1. Download `TensorMedia-macOS.dmg` from the **[latest release](../../releases/latest)**.
2. Double-click it to mount, then drag `TensorMedia` into the `Applications` folder.
3. Launch from Launchpad.
*Note: the app is ad-hoc signed, so on first launch right-click it -> "Open" (or approve it in System Settings -> Privacy & Security) to clear Gatekeeper.*

#### Linux (Ubuntu / Debian Base)
*Linux builds are compiled exclusively with CPU-inference routing to ensure maximum hardware compatibility and reduce artifact bloat (No CUDA requirement).*

> **Multi-part archive.** The Linux bundle (~2.7 GiB of statically bundled ML frameworks, weights excluded) exceeds GitHub's 2 GiB per-asset limit, so it is published as split volumes: `TensorMedia-Linux-x86_64.tar.gz.part-00`, `.part-01`, … This is a known trade-off of the monolithic `onedir` build.

1. Download **all** `TensorMedia-Linux-x86_64.tar.gz.part-*` files into one directory.
2. Reassemble and extract:
   ```bash
   cat TensorMedia-Linux-x86_64.tar.gz.part-* > TensorMedia-Linux-x86_64.tar.gz
   tar -xzf TensorMedia-Linux-x86_64.tar.gz
   ```
3. Run the binary:
   ```bash
   cd TensorMedia
   chmod +x TensorMedia
   ./TensorMedia
   ```

### 🏗 System Architecture
The application relies on a `onedir` compilation strategy rather than monolithic execution (`onefile`). Heavy computational libraries (e.g., PyTorch) are isolated in the `_internal` directory. This architectural choice eliminates temporary extraction overhead, reducing cold-start latency from ~30 seconds to < 1 second.

*Note (Linux): the Linux-CPU build ships without bundled model weights — on first launch the application offers a one-time runtime download (~480 MB) into `~/.local/share/TensorMedia/models`. Even weight-free the framework bundle is ~2.7 GiB, which exceeds the GitHub Releases 2 GiB per-asset cap, so the Linux artifact is published as a multi-part archive (reassemble with `cat`, see above).*

### ⚡ Performance — Reference Benchmark

End-to-end stress test of the full pipeline (disk indexing → blake3 hashing → SigLIP vectorization → FAISS clustering):

| Parameter | Value |
|---|---|
| Hardware | Mac mini M4, 16 GB RAM / 256 GB |
| SSD sequential read | ~1000 MB/s |
| Dataset volume | 569.24 GB |
| Files processed | 4 609 |
| Total wall-clock time | 17 min 05 sec |
| Effective throughput | **~555.3 MB/s** |

Sustained throughput at ~55% of raw SSD bandwidth across the full ML pipeline confirms there are **no I/O bottlenecks**: the multiprocessing I/O pool keeps the NPU/GPU vectorization stage saturated instead of waiting on disk.

### 🛠 Build from source
Requires **Python 3.12**.
```bash
git clone https://github.com/KyLaEga/TensorMedia.git
cd TensorMedia
python3.12 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py     # run from source
python build.py    # produce a distributable bundle (PyInstaller -> dist/)
```

---

<a id="русский"></a>
## 🇷🇺 Русский

TensorMedia — кроссплатформенное десктоп-приложение, которое находит и убирает визуальные дубли и почти-дубли в больших фото/видео-библиотеках с помощью локального ML-движка. Архитектура развертывания гарантирует автономный запуск в Windows, macOS и Linux без установки зависимостей.

### ✨ Возможности
- **Поиск визуальных дублей** — эмбеддинги SigLIP + кластеризация FAISS группируют похожие/дублирующиеся фото и кадры видео по настраиваемому порогу схожести.
- **Группировка по лицам (биометрия)** — эмбеддинги FaceNet (InceptionResnetV1) кластеризуют снимки по людям на них.
- **Сравнение бок-о-бок** — карусель/сетка сравнения для фото и видео, чтобы решить, какие копии оставить.
- **Умная авторазметка** — один клик помечает дубли к удалению по Парето-каскаду (разрешение → размер → дата), всегда сохраняя лучшую копию.
- **Безопасная очистка** — в Корзину или удаление навсегда; реактивное слежение за ФС держит индекс в актуальном состоянии.
- **Ускорение и офлайн** — инференс на Apple MPS / CPU; сборки macOS и Windows содержат веса моделей и работают полностью офлайн.

### 🚀 Установка и запуск

Приложение распространяется в виде готовых автономных сборок через GitHub Actions. Установка Python или внешних библиотек не требуется.

#### Windows (x86-64)
1. Откройте страницу **[Releases](../../releases/latest)** репозитория.
2. Скачайте `TensorMedia_Setup.exe`.
3. Запустите установщик и пройдите мастер. Ярлык на рабочем столе создаётся автоматически.
*Примечание: Windows SmartScreen может предупредить о неподписанном файле. Нажмите «Подробнее» -> «Выполнить в любом случае».*

#### macOS (Apple Silicon / Intel)
1. Скачайте `TensorMedia-macOS.dmg` из **[последнего релиза](../../releases/latest)**.
2. Дважды кликните для монтирования и перетащите `TensorMedia` в папку `Applications`.
3. Запустите через Launchpad.
*Примечание: приложение подписано ad-hoc, поэтому при первом запуске кликните по нему правой кнопкой -> «Открыть» (или подтвердите в «Системные настройки» -> «Конфиденциальность и безопасность»), чтобы снять блокировку Gatekeeper.*

#### Linux (Ubuntu / Debian Base)
*Сборки для Linux скомпилированы исключительно с поддержкой CPU-инференса. Это гарантирует максимальную аппаратную совместимость и исключает необходимость в проприетарных драйверах NVIDIA (CUDA).*

> **Многотомный архив.** Linux-сборка (~2.7 ГиБ статически упакованных ML-фреймворков, без весов) превышает лимит GitHub в 2 ГиБ на ассет, поэтому публикуется томами: `TensorMedia-Linux-x86_64.tar.gz.part-00`, `.part-01`, … Это известный трейдофф монолитной `onedir`-сборки.

1. Скачайте **все** файлы `TensorMedia-Linux-x86_64.tar.gz.part-*` в одну директорию.
2. Склейте тома и распакуйте:
   ```bash
   cat TensorMedia-Linux-x86_64.tar.gz.part-* > TensorMedia-Linux-x86_64.tar.gz
   tar -xzf TensorMedia-Linux-x86_64.tar.gz
   ```
3. Запустите бинарный файл:
   ```bash
   cd TensorMedia
   chmod +x TensorMedia
   ./TensorMedia
   ```

### 🏗 Системная архитектура
Приложение использует стратегию компиляции `onedir` вместо монолитного файла (`onefile`). Тяжелые вычислительные библиотеки (PyTorch) изолированы в системной директории `_internal`. Данный архитектурный выбор исключает скрытую распаковку архивов во временные папки системы, снижая задержку холодного старта с ~30 секунд до менее чем 1 секунды.

*Примечание (Linux): Linux-CPU сборка поставляется без упакованных весов моделей — при первом запуске приложение предложит одноразовую загрузку (~480 МБ) в `~/.local/share/TensorMedia/models`. Даже без весов бандл фреймворков ~2.7 ГиБ, что превышает лимит GitHub Releases (2 ГиБ на ассет), поэтому Linux-артефакт публикуется многотомным архивом (склейка через `cat`, см. выше).*

### ⚡ Производительность — эталонный бенчмарк

Сквозной стресс-тест полного конвейера (индексация диска → blake3-хэширование → векторизация SigLIP → кластеризация FAISS):

| Параметр | Значение |
|---|---|
| Железо | Mac mini M4, 16 ГБ RAM / 256 ГБ |
| Последовательное чтение SSD | ~1000 MB/s |
| Объём датасета | 569.24 GB |
| Обработано файлов | 4 609 |
| Общее время | 17 мин 05 сек |
| Сквозная скорость | **~555.3 MB/s** |

Устойчивая скорость на уровне ~55% от сырой пропускной способности SSD на полном ML-конвейере подтверждает **отсутствие I/O bottlenecks**: пул I/O-процессов держит стадию векторизации NPU/GPU загруженной, не простаивая на диске.

### 🛠 Сборка из исходников
Требуется **Python 3.12**.
```bash
git clone https://github.com/KyLaEga/TensorMedia.git
cd TensorMedia
python3.12 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py     # запуск из исходников
python build.py    # сборка дистрибутива (PyInstaller -> dist/)
```