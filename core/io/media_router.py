import subprocess
import tempfile
import imagehash
from PIL import Image, ImageSequence
from pdf2image import convert_from_path
from pathlib import Path

class UniversalMediaLoader:
    """Извлекает PIL Images из любого формата. Оптимизировано для M4 (Fast-3 Extract)."""
    
    @staticmethod
    def extract_images(file_path: Path):
        ext = file_path.suffix.lower()
        images, hashes = [], []
        
        try:
            # 1. СТАТИКА
            if ext in {'.jpg', '.jpeg', '.png', '.webp', '.heic'}:
                with Image.open(file_path).convert("RGB") as img:
                    hashes.append(str(imagehash.phash(img)))
                    images.append(img.copy())
            
            # 2. ДИНАМИКА (GIF) - Строго 3 кадра
            elif ext == '.gif':
                with Image.open(file_path) as img:
                    frames = [frame.copy().convert("RGB") for frame in ImageSequence.Iterator(img)]
                    step = max(1, len(frames) // 3)
                    for i in range(0, len(frames), step)[:3]:
                        hashes.append(str(imagehash.phash(frames[i])))
                        images.append(frames[i])
                        
            # 3. ДОКУМЕНТЫ (PDF) - Строго 3 страницы
            elif ext == '.pdf':
                pages = convert_from_path(file_path, first_page=1, last_page=3, fmt='jpeg')
                for page in pages:
                    hashes.append(str(imagehash.phash(page)))
                    images.append(page)
                    
            # 4. ВИДЕО - 3 кадра (25%, 50%, 75%)
            elif ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
                dur_cmd = ['ffprobe', '-v', '0', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)]
                dur = float(subprocess.check_output(dur_cmd, timeout=5))
                if dur > 0:
                    with tempfile.TemporaryDirectory() as tmp:
                        timestamps = [dur * i for i in [0.25, 0.5, 0.75]]
                        for i, ts in enumerate(timestamps):
                            out = f"{tmp}/{i}.jpg"
                            cmd = ['ffmpeg', '-y', '-hwaccel', 'videotoolbox', '-ss', str(ts), '-i', str(file_path), '-vframes', '1', '-vf', 'scale=224:224', '-q:v', '2', out]
                            
                            # Вектор изменения: Жесткий таймаут (10 сек) предотвращает зависание на битых секторах видео
                            try:
                                result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                                if result.returncode == 0:
                                    with Image.open(out).convert("RGB") as img:
                                        hashes.append(str(imagehash.phash(img)))
                                        images.append(img.copy())
                            except subprocess.TimeoutExpired:
                                continue
        except Exception:
            return None, None
            
        return images, hashes