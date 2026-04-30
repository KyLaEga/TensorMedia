import os
from pathlib import Path

def generate_architecture():
    print("Инициализация развертывания тензорной архитектуры...")
    
    # Топология директорий
    dirs = [
        "core/ml",         # Тензорные модели и FAISS
        "core/io",         # Распаковка медиаформатов
        "ui/views",        # Окна PyQt6
        "ui/components",   # Виджеты
        "utils",           # Логгеры, конфиги
        "assets/icons"
    ]
    
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)
        (Path(d) / "__init__.py").touch()

    # 1. Спецификация зависимостей (Optimized for Apple M4)
    req_content = """# Ядро и ML
torch>=2.2.0
transformers>=4.38.0
faiss-cpu>=1.7.4
numpy>=1.26.0
imagehash>=4.3.1

# Медиа-процессинг
Pillow>=10.2.0
pdf2image>=1.17.0
# OpenCV исключен за избыточностью. Используется нативный ffmpeg (VideoToolbox)

# Графический интерфейс
PyQt6>=6.6.0
"""
    Path("requirements.txt").write_text(req_content, encoding='utf-8')

    # 2. Универсальный маршрутизатор медиа (Photo/Video/GIF/PDF)
    router_content = """import subprocess
import tempfile
import imagehash
from PIL import Image, ImageSequence
from pdf2image import convert_from_path
from pathlib import Path

class UniversalMediaLoader:
    \"\"\"Извлекает PIL Images из любого формата для передачи в SigLIP.\"\"\"
    
    @staticmethod
    def extract_images(file_path: Path):
        ext = file_path.suffix.lower()
        images, hashes = [], []
        
        try:
            # 1. СТАТИКА (JPG, PNG, WEBP)
            if ext in {'.jpg', '.jpeg', '.png', '.webp'}:
                with Image.open(file_path).convert("RGB") as img:
                    hashes.append(str(imagehash.phash(img)))
                    images.append(img.copy())
            
            # 2. ДИНАМИКА (GIF)
            elif ext == '.gif':
                with Image.open(file_path) as img:
                    frames = [frame.copy().convert("RGB") for frame in ImageSequence.Iterator(img)]
                    # Берем максимум 5 равномерных кадров из GIF
                    step = max(1, len(frames) // 5)
                    for i in range(0, len(frames), step)[:5]:
                        hashes.append(str(imagehash.phash(frames[i])))
                        images.append(frames[i])
                        
            # 3. ДОКУМЕНТЫ (PDF)
            elif ext == '.pdf':
                # Извлекаем первые 5 страниц PDF
                pages = convert_from_path(file_path, first_page=1, last_page=5, fmt='jpeg')
                for page in pages:
                    hashes.append(str(imagehash.phash(page)))
                    images.append(page)
                    
            # 4. ВИДЕО (MP4, MOV, MKV)
            elif ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi'}:
                dur_cmd = ['ffprobe', '-v', '0', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)]
                dur = float(subprocess.check_output(dur_cmd, timeout=5))
                if dur > 0:
                    with tempfile.TemporaryDirectory() as tmp:
                        timestamps = [dur * i for i in [0.2, 0.35, 0.5, 0.65, 0.8]]
                        for i, ts in enumerate(timestamps):
                            out = f"{tmp}/{i}.jpg"
                            cmd = ['ffmpeg', '-y', '-hwaccel', 'videotoolbox', '-ss', str(ts), '-i', str(file_path), '-vframes', '1', '-vf', 'scale=224:224', '-q:v', '2', out]
                            if subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                                with Image.open(out).convert("RGB") as img:
                                    hashes.append(str(imagehash.phash(img)))
                                    images.append(img.copy())
        except Exception as e:
            return None, None
            
        return images, hashes
"""
    Path("core/io/media_router.py").write_text(router_content, encoding='utf-8')

    # 3. Каркас моста для PyQt6
    thread_bridge_content = """from PyQt6.QtCore import QThread, pyqtSignal
from pathlib import Path
# from core.ml.tensor_engine import TensorEngine 

class ScannerThread(QThread):
    \"\"\"Изолированный рабочий поток для защиты Main Thread от зависаний.\"\"\"
    progress_signal = pyqtSignal(int, int) # текущий файл, всего файлов
    result_signal = pyqtSignal(dict)       # лог перемещений
    error_signal = pyqtSignal(str)

    def __init__(self, target_dir: str):
        super().__init__()
        self.target_dir = target_dir
        # self.engine = TensorEngine()

    def run(self):
        try:
            # Имитация интеграции: self.engine.process_directory(self.target_dir, callback=self.progress_signal.emit)
            pass 
        except Exception as e:
            self.error_signal.emit(str(e))
"""
    Path("ui/scanner_thread.py").write_text(thread_bridge_content, encoding='utf-8')

    print("Архитектура сгенерирована. Выполните: pip install -r requirements.txt")

if __name__ == "__main__":
    generate_architecture()