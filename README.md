# TensorMedia

**Version 1.2.3** · 🌍 Language: [English](#english) | [Русский](#русский)

---

<a id="english"></a>
## 🇬🇧 English

TensorMedia is a cross-platform application featuring an integrated machine learning engine for advanced media processing. The deployment architecture guarantees zero-dependency execution across Windows, macOS, and Linux environments.

### 🚀 Installation & Deployment

The application is distributed as self-contained artifacts via GitHub Actions. No Python environment or external library installation is required.

#### Windows (x86-64)
1. Navigate to the **Releases/Artifacts** tab in the repository.
2. Download the `TensorMedia-Windows-Installer.zip` archive and extract it.
3. Run `TensorMedia_Setup.exe`.
4. Follow the standard installation wizard. A desktop shortcut will be automatically generated.
*Note: Microsoft Defender SmartScreen may display a warning for unsigned binaries. Click "More info" -> "Run anyway".*

#### macOS (Apple Silicon / Intel)
1. Download `TensorMedia-macOS-DMG.zip` and extract the `.dmg` file.
2. Double-click the disk image to mount it.
3. Drag and drop the `TensorMedia` icon into the `Applications` folder.
4. Launch from Launchpad.
*Note: Due to Gatekeeper restrictions on unsigned applications, right-click the app -> "Open" on the first launch, or approve it in System Settings -> Privacy & Security.*

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

---

<a id="русский"></a>
## 🇷🇺 Русский

TensorMedia — кроссплатформенное приложение со встроенным ядром машинного обучения для обработки медиаданных. Архитектура развертывания гарантирует автономный запуск в средах Windows, macOS и Linux без установки дополнительных зависимостей.

### 🚀 Установка и запуск

Приложение распространяется в виде готовых автономных сборок через GitHub Actions. Установка Python или внешних библиотек не требуется.

#### Windows (x86-64)
1. Перейдите на вкладку **Releases/Artifacts** в репозитории.
2. Скачайте архив `TensorMedia-Windows-Installer.zip` и распакуйте его.
3. Запустите файл `TensorMedia_Setup.exe`.
4. Пройдите шаги стандартного установщика. Ярлык на рабочем столе будет создан автоматически.
*Примечание: Фильтр Windows SmartScreen может заблокировать запуск неподписанного установщика. Нажмите «Подробнее» -> «Выполнить в любом случае».*

#### macOS (Apple Silicon / Intel)
1. Скачайте `TensorMedia-macOS-DMG.zip` и извлеките файл `.dmg`.
2. Дважды кликните по образу диска для его монтирования.
3. Перетащите иконку `TensorMedia` в системную папку `Applications` (Программы).
4. Запустите приложение через Launchpad.
*Примечание: Gatekeeper блокирует неподписанные приложения. При первом запуске нажмите правой кнопкой мыши -> «Открыть» или подтвердите запуск в меню «Системные настройки» -> «Конфиденциальность и безопасность».*

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