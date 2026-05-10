# TensorMedia

🌍 Language: [English](#english) | [Русский](#русский)

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
1. Download `TensorMedia-Linux-CPU.zip`.
2. Extract the archive to your preferred directory (e.g., `~/Apps/TensorMedia`).
3. Execute the `TensorMedia` binary directly from the terminal or file manager:
   ```bash
   chmod +x TensorMedia
   ./TensorMedia
   ```

### 🏗 System Architecture
The application relies on a `onedir` compilation strategy rather than monolithic execution (`onefile`). Heavy computational libraries (e.g., PyTorch) are isolated in the `_internal` directory. This architectural choice eliminates temporary extraction overhead, reducing cold-start latency from ~30 seconds to < 1 second.

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
1. Скачайте `TensorMedia-Linux-CPU.zip`.
2. Распакуйте архив в удобную директорию (например, `~/Apps/TensorMedia`).
3. Запустите бинарный файл `TensorMedia` через терминал или файловый менеджер:
   ```bash
   chmod +x TensorMedia
   ./TensorMedia
   ```

### 🏗 Системная архитектура
Приложение использует стратегию компиляции `onedir` вместо монолитного файла (`onefile`). Тяжелые вычислительные библиотеки (PyTorch) изолированы в системной директории `_internal`. Данный архитектурный выбор исключает скрытую распаковку архивов во временные папки системы, снижая задержку холодного старта с ~30 секунд до менее чем 1 секунды.