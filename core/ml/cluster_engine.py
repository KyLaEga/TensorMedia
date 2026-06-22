# ============================================================
# MODULE: core/ml/cluster_engine.py
# ============================================================
import os
import gc
import time
import zipfile
import numpy as np
import torch
import cv2
import blake3
import psutil
from PIL import Image, UnidentifiedImageError
from pathlib import Path
from multiprocessing import Pool, Value, shared_memory

cv2.setNumThreads(0)

# HEIC/HEIF: регистрируем PIL-опенер pillow-heif на уровне МОДУЛЯ — его исполняют
# и главный процесс (warmup), и spawn-воркеры пула (process_single_file_io зовёт
# Image.open в дочернем процессе). Без него Image.open(".heic") бросает
# UnidentifiedImageError, и дефолтный iPhone-формат тихо выпадал из индекса.
# Логика вынесена в utils.image_io (общая с GUI-превью); её импорт Qt-чист, т.е.
# безопасен в spawn-воркере. Зеркало в requirements.txt и TensorMedia.spec.
from utils.image_io import register_heif as _register_heif
_register_heif()

# Глушим C++-логгер OpenCV. Декодер AVFoundation (macOS) не читает VP8/VP9 .webm
# и печатает "Couldn't read video stream" напрямую в stderr В ОБХОД Python — это
# не ловится try/except. Нечитаемые файлы и так пропускаются по isOpened()==False
# (см. ветку видео ниже), поэтому шумный C++-вывод нам не нужен.
try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
except Exception:
    pass

from utils.env_config import get_models_dir
from utils.logger import auditor
from core.profiler import HardwareProfiler
from utils.batch_operations import DBConnectionPool
from core.ml.faiss_manager import FaissManager

os.environ["LOKY_MAX_CPU_COUNT"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

_worker_cancel_flag = None

def _scan_pool_init(cancel_flag):
    global _worker_cancel_flag
    _worker_cancel_flag = cancel_flag

def _scan_io_cancelled() -> bool:
    if _worker_cancel_flag is None:
        return False
    try:
        return bool(_worker_cancel_flag.value)
    except Exception as e:
        auditor.debug(f"Failed to read scan cancel flag: {e}", exc_info=True)
        return False

def _cancelled_io_result(file_path: Path, size, mtime, file_hash, vector) -> dict:
    return {
        "path": str(file_path), "size": size, "mtime": mtime, "phash": file_hash,
        "vector": vector, "shm_blocks": [], "res": "",
        "dur": 0.0, "codec": "", "sharpness": 0.0, "fps": 0.0, "watermark": 0.0,
    }


def estimate_watermark_score(gray_frames) -> float:
    """Оценка «вотермарочности» по нескольким кадрам одного файла, 0..~0.05.

    Идея: вотермарка/burned-in лого/текст СТАТИЧНА (живёт в одних пикселях по
    кадрам), тогда как сам контент меняется. Берём пиксели, которые ОДНОВРЕМЕННО
    (а) почти не меняются между кадрами относительно общего движения и (б) несут
    сильные контуры (логотип/текст). Доля таких пикселей = score.

    Гейты против ложных срабатываний:
      • нужно ≥3 кадра одинакового размера (приводим к фикс-сетке 160×90 серого);
      • если ВЕСЬ клип почти статичен (motion < порога) — отличить вотермарку от
        статичной сцены нельзя → 0.0 (не штрафуем);
      • чёрные полосы (flat) контуров не дают → не считаются.
    Чистый Qt-free numpy/cv2, безопасно в spawn-воркере."""
    try:
        if gray_frames is None or len(gray_frames) < 3:
            return 0.0
        st = np.stack(gray_frames).astype(np.float32)        # (T,H,W)
        tstd = st.std(axis=0)
        motion = float(tstd.mean())
        if motion < 8.0:
            return 0.0
        mean = st.mean(axis=0)
        gx = np.abs(np.diff(mean, axis=1)); gy = np.abs(np.diff(mean, axis=0))
        edge = np.zeros_like(mean)
        edge[:, :-1] += gx; edge[:-1, :] += gy
        static = tstd < (0.35 * motion)                      # статичные ОТНОСИТЕЛЬНО движения
        overlay = static & (edge > 18.0)
        return float(overlay.mean())
    except Exception as e:
        auditor.debug(f"Watermark estimate failed: {e}", exc_info=True)
        return 0.0

def _unlink_shm_blocks(blocks) -> None:
    """Unlink every POSIX shared-memory segment referenced by `blocks`.

    Safe to call multiple times: a segment already freed raises
    FileNotFoundError on re-open, which we swallow. This is the single point
    of truth for releasing /dev/shm segments so no result path can leak them.
    """
    for b in blocks or []:
        if isinstance(b, dict) and b.get("is_shm"):
            try:
                shared_memory.SharedMemory(name=b["name"]).unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                auditor.debug(f"Failed to unlink SHM segment {b.get('name')}: {e}", exc_info=True)

def calculate_optical_sharpness(frame: np.ndarray) -> float:
    try:
        # ВАЛИДАЦИЯ РАЗМЕРНОСТИ: пустой/одномерно-вырожденный кадр (декодер вернул
        # 0xN или Nx0, либо None) не даёт корректной матрицы Лапласа. Дисперсия
        # такого среза роняет numpy в RuntimeWarning "Degrees of freedom <= 0 for
        # slice" и возвращает nan, отравляющий метрику резкости.
        if frame is None or frame.size == 0 or frame.ndim < 2:
            return 0.0
        if len(frame.shape) == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            gray = frame
        lap = cv2.Laplacian(gray, cv2.CV_64F)
        h, w = lap.shape
        grid_size = 4
        bh, bw = max(1, h // grid_size), max(1, w // grid_size)
        max_var = 0.0
        for i in range(grid_size):
            for j in range(grid_size):
                block = lap[i*bh:(i+1)*bh, j*bw:(j+1)*bw]
                # Срез может оказаться пустым (i*bh >= h на мелких кадрах) или из
                # одного элемента. var() на таком массиве → "Degrees of freedom
                # <= 0". Считаем дисперсию только для блоков из ≥2 значений.
                if block.size < 2:
                    continue
                var = float(block.var())
                if var > max_var: max_var = var
        return float(max_var)
    except Exception as e:
        auditor.warning(f"Worker Sharpness calculation error: {e}")
        return 0.0

def _compute_fast_hash_io(file_path) -> str:
    """blake3-хэш файла. Полное чтение для файлов ≤100 МБ, иначе 10 семплов +
    метаданные. Module-level (без self), потому что вызывается ВНУТРИ воркеров
    пула — расчёт распараллеливается по ядрам вместо серийного пре-пасса в
    главном потоке."""
    try:
        h = blake3.blake3()
        stat = file_path.stat()
        size = stat.st_size
        with open(file_path, 'rb') as f:
            if size <= 100 * 1024 * 1024:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
            else:
                step = size // 10
                for i in range(10):
                    f.seek(i * step, os.SEEK_SET)
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                meta_str = f"{size}_{stat.st_mtime}_{file_path.suffix}"
                h.update(meta_str.encode('utf-8'))
        return h.hexdigest()
    except Exception as e:
        return f"FAIL_{file_path.name}_{e}"


def process_single_file_io(task_data: tuple) -> dict:
    file_path, size, mtime, file_hash, vector, scan_mode = task_data
    if _scan_io_cancelled():
        return _cancelled_io_result(file_path, size, mtime, file_hash, vector)
    # Хэш считаем ЗДЕСЬ, в воркере (распараллелено пулом), а не серийно в главном
    # потоке до старта пула: чтение до 100 МБ/файл больше не блокирует пайплайн.
    if file_hash is None:
        file_hash = _compute_fast_hash_io(file_path)
    res, dur, codec, sharpness, fps_val = "", 0.0, "", 0.0, 0.0
    watermark = 0.0          # доля «вотермарочного» оверлея (видео); 0 для картинок
    shm_blocks = []
    ext = file_path.suffix.lower()
    # Кэш статической миниатюры предпросмотра: один раз на файл (первый
    # репрезентативный кадр, что уходит в модель). Делает превью видео мгновенным —
    # см. utils.thumb_cache. Best-effort: любой сбой не должен ронять векторизацию.
    _thumb_saved = [False]

    def _save_preview_thumb(img_obj):
        if _thumb_saved[0]:
            return
        _thumb_saved[0] = True
        try:
            from utils.thumb_cache import thumb_path_for, save_thumb_pil
            save_thumb_pil(thumb_path_for(file_path, size, mtime), img_obj)
        except Exception as e:
            auditor.debug(f"Preview thumb save failed for {file_path}: {e}", exc_info=True)

    def _allocate_shm(img_obj):
        _save_preview_thumb(img_obj)
        arr = np.array(img_obj)
        # WINDOWS (NT): именованная shared memory — это file-mapping без
        # персистентности: сегмент живёт, ПОКА открыт хотя бы один хэндл.
        # Воркер делает shm.close() и завершает задачу ДО того, как родитель
        # откроет сегмент по имени → ядро освобождает мэппинг, родитель ловит
        # FileNotFoundError ("SHM Read Fault"), изображение теряется и файл
        # не векторизуется (нулевые совпадения FaceNet/SigLIP на Windows).
        # На NT всегда передаём пиксели inline-байтами через pickle пула.
        if os.name == "nt":
            shm_blocks.append({
                "shape": arr.shape,
                "dtype": str(arr.dtype),
                "data": arr.tobytes(),
                "is_shm": False
            })
            return
        try:
            import uuid
            shm_name = f"tm_shm_{uuid.uuid4().hex[:20]}"
            shm = shared_memory.SharedMemory(name=shm_name, create=True, size=arr.nbytes)
            shm_arr = np.ndarray(arr.shape, dtype=arr.dtype, buffer=shm.buf)
            np.copyto(shm_arr, arr)
            shm_blocks.append({
                "name": shm.name,
                "shape": arr.shape,
                "dtype": str(arr.dtype),
                "is_shm": True
            })
            shm.close()
            # The parent process owns unlink() for this segment. Detach it from
            # this worker's resource_tracker so the tracker neither warns about
            # a "leaked" segment at shutdown nor double-unlinks a name the
            # parent may have already freed (and possibly reused).
            try:
                from multiprocessing import resource_tracker
                resource_tracker.unregister(f"/{shm_name}", "shared_memory")
            except Exception as e:
                auditor.debug(f"resource_tracker.unregister failed for {shm_name}: {e}", exc_info=True)
        except OSError as e:
            auditor.warning(f"Failed to allocate shared memory for image: {e}")
            shm_blocks.append({
                "shape": arr.shape,
                "dtype": str(arr.dtype),
                "data": arr.tobytes(),
                "is_shm": False
            })

    dhashes = []

    def _add_dhash(img_obj):
        try:
            arr = np.array(img_obj.convert('L'))
            small = cv2.resize(arr, (9, 8), interpolation=cv2.INTER_AREA)
            diff = small[:, 1:] > small[:, :-1]
            dhashes.append(np.packbits(diff.flatten()))
        except Exception:
            pass

    def _add_dhash_cv(frame):
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
            diff = small[:, 1:] > small[:, :-1]
            dhashes.append(np.packbits(diff.flatten()))
        except Exception:
            pass

    try:
        if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
            if _scan_io_cancelled():
                return _cancelled_io_result(file_path, size, mtime, file_hash, vector)
            
            # КРИТИЧЕСКИЙ ПАТЧ: Синхронное открытие видео предотвращает Deadlock на Windows
            cap = cv2.VideoCapture(str(file_path))
            
            if cap is not None and cap.isOpened():
                try:
                    res = f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
                    fps_val = float(cap.get(cv2.CAP_PROP_FPS))
                    total_frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    if fps_val > 0: dur = float(total_frames / fps_val)
                    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                    codec = "".join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)]).strip().lower()
                    
                    # КРИТИЧЕСКИЙ ПАТЧ: Ускорение + Надежность
                    # Берем 5 независимых кадров (Временная Сетка) для кросс-матчинга
                    check_points = [0.15, 0.30, 0.50, 0.70, 0.85]
                    max_sharp = 0.0
                    wm_frames = []          # фикс-сетка серых кадров для watermark-оценки
                    t_start = time.monotonic()
                    for cp in check_points:
                        if _scan_io_cancelled() or (time.monotonic() - t_start) > 10.0:
                            break
                        target = int(total_frames * cp) if total_frames > 0 else 0
                        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                        ret, frame = cap.read()

                        # Строгая проверка: кадр не должен быть монотонным/черным (средняя яркость > 20)
                        if ret and frame.mean() > 20.0:
                            try:
                                wm_frames.append(cv2.resize(
                                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90),
                                    interpolation=cv2.INTER_AREA))
                            except Exception:
                                pass
                            _add_dhash_cv(frame)
                            img_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                            # КРИТИЧЕСКИЙ ПАТЧ ПРОПОРЦИЙ
                            if scan_mode == "faces":
                                img_pil.thumbnail((1024, 1024), Image.Resampling.BICUBIC)
                            elif scan_mode == "visual":
                                img_pil.thumbnail((512, 512), Image.Resampling.BICUBIC)
                            # Для exact (dHash) миниатюра не нужна, но сделаем для единообразия
                            else:
                                img_pil.thumbnail((256, 256), Image.Resampling.BICUBIC)
                            _allocate_shm(img_pil)

                            h, w = frame.shape[:2]
                            if max(w, h) > 256:
                                scale = 256.0 / max(w, h)
                                frame_sm = cv2.resize(frame, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                            else: frame_sm = frame
                            sharp = calculate_optical_sharpness(cv2.cvtColor(frame_sm, cv2.COLOR_BGR2GRAY))
                            if sharp > max_sharp: max_sharp = sharp
                    sharpness = float(max_sharp)
                    watermark = estimate_watermark_score(wm_frames)
                finally:
                    cap.release()
            else:
                # Декодер не смог открыть файл (неподдерживаемый кодек, напр.
                # VP8/VP9 .webm на AVFoundation, или битый контейнер). Освобождаем
                # объект VideoCapture, чтобы не утёк нативный дескриптор, и мягко
                # пропускаем файл (vector останется None → файл не индексируется).
                if cap is not None:
                    cap.release()
                auditor.debug(f"Skipping unreadable video (unsupported codec/container): {file_path}")

        elif ext in {'.jpg', '.png', '.webp', '.bmp', '.heic', '.jpeg'}:
            if _scan_io_cancelled():
                return _cancelled_io_result(file_path, size, mtime, file_hash, vector)
            with Image.open(file_path) as img: 
                res = f"{img.width}x{img.height}"
                _add_dhash(img)
                img_rgb = img.convert("RGB")
                # КРИТИЧЕСКИЙ ПАТЧ ПРОПОРЦИЙ: MTCNN не видит лица на сплюснутых квадратах
                if scan_mode == "faces":
                    img_rgb.thumbnail((1024, 1024), Image.Resampling.BICUBIC)
                else:
                    img_rgb.thumbnail((512, 512), Image.Resampling.BICUBIC)
                _allocate_shm(img_rgb)
                
                img.thumbnail((256, 256))
                sharpness = calculate_optical_sharpness(np.array(img.convert('L')))
        
        elif ext == '.gif':
            if _scan_io_cancelled():
                return _cancelled_io_result(file_path, size, mtime, file_hash, vector)
            try:
                with Image.open(file_path) as img:
                    tot_frames = getattr(img, "n_frames", 1)
                    if tot_frames > 1:
                        check_points = [0.20, 0.40, 0.60, 0.80] if tot_frames > 3 else [0.0]
                        max_sharp = 0.0
                        for cp in check_points:
                            if _scan_io_cancelled():
                                break
                            target_frame = min(max(0, int(tot_frames * cp)), tot_frames - 1)
                            img.seek(target_frame)
                            frame_pil = img.convert("RGB")
                            _add_dhash(frame_pil)
                            if not res: res = f"{frame_pil.width}x{frame_pil.height}"
                            if scan_mode == "faces":
                                frame_pil.thumbnail((1024, 1024), Image.Resampling.BICUBIC)
                            else:
                                frame_pil.thumbnail((512, 512), Image.Resampling.BICUBIC)
                            _allocate_shm(frame_pil)
                                    
                            frame_pil.thumbnail((256, 256))
                            sharp = calculate_optical_sharpness(np.array(frame_pil.convert('L')))
                            if sharp > max_sharp: max_sharp = sharp
                        sharpness = float(max_sharp)
                    else:
                        frame_pil = img.convert("RGB")
                        _add_dhash(frame_pil)
                        res = f"{frame_pil.width}x{frame_pil.height}"
                        if scan_mode == "faces":
                            frame_pil.thumbnail((1024, 1024), Image.Resampling.BICUBIC)
                        else:
                            frame_pil.thumbnail((512, 512), Image.Resampling.BICUBIC)
                        _allocate_shm(frame_pil)
                        frame_pil.thumbnail((256, 256))
                        sharpness = calculate_optical_sharpness(np.array(frame_pil.convert('L')))
            except Exception as e: 
                auditor.warning(f"Worker GIF error {file_path}: {e}")
        
        elif ext == '.cbz':
            if _scan_io_cancelled():
                return _cancelled_io_result(file_path, size, mtime, file_hash, vector)
            with zipfile.ZipFile(file_path, 'r') as z:
                names = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                if names:
                    if len(names) > 50:
                        step = max(1, len(names) // 50)
                        names = names[::step][:50]
                    total_pages = len(names)
                    check_points = [0.0, 0.10, 0.30, 0.50] if total_pages > 4 else [0.0]
                    max_sharp = -1.0
                    best_img_for_model = None
                    
                    for cp in check_points:
                        if _scan_io_cancelled():
                            break
                        idx = int(total_pages * cp) if total_pages > 0 else 0
                        with z.open(names[idx]) as f:
                            with Image.open(f) as img:
                                img_rgb = img.convert("RGB")
                                _add_dhash(img_rgb)
                                if not res: res = f"{img_rgb.width}x{img_rgb.height}"
                                img_thumb = img_rgb.copy()
                                img_thumb.thumbnail((256, 256))
                                sharp = calculate_optical_sharpness(np.array(img_thumb.convert('L')))
                                if sharp > max_sharp: 
                                    max_sharp = sharp
                                    if scan_mode == "faces":
                                        img_rgb.thumbnail((1024, 1024), Image.Resampling.BICUBIC)
                                    else:
                                        img_rgb.thumbnail((512, 512), Image.Resampling.BICUBIC)
                                    best_img_for_model = img_rgb
                    
                    if best_img_for_model:
                        _allocate_shm(best_img_for_model)
                    sharpness = float(max_sharp)

        elif ext == '.pdf':
            if _scan_io_cancelled():
                return _cancelled_io_result(file_path, size, mtime, file_hash, vector)
            try:
                from utils.pdf_render import open_document, render_page
                doc = open_document(file_path)
                try:
                    total_pages = len(doc)
                    if total_pages == 0:
                        return _cancelled_io_result(file_path, size, mtime, file_hash, vector)

                    check_points = [0.0, 0.30, 0.60] if total_pages > 3 else [0.0]
                    max_sharp = -1.0
                    best_img_for_model = None

                    for cp in check_points:
                        if _scan_io_cancelled():
                            break
                        page_num = int(total_pages * cp)
                        if page_num >= total_pages: page_num = total_pages - 1
                        with render_page(doc, page_num, 0.5) as img:
                            _add_dhash(img)
                            if not res: res = f"{img.width}x{img.height}"
                            img_thumb = img.copy()
                            img_thumb.thumbnail((256, 256))
                            sharp = calculate_optical_sharpness(np.array(img_thumb.convert('L')))
                            if sharp > max_sharp:
                                max_sharp = sharp
                                if scan_mode == "faces":
                                    img.thumbnail((1024, 1024), Image.Resampling.BICUBIC)
                                else:
                                    img.thumbnail((512, 512), Image.Resampling.BICUBIC)
                                best_img_for_model = img

                    if best_img_for_model:
                        _allocate_shm(best_img_for_model)
                    sharpness = float(max_sharp)
                finally:
                    doc.close()
            except Exception as e:
                auditor.warning(f"Worker PDF error {file_path}: {e}")
                return {
                    "path": str(file_path), "size": size, "mtime": mtime, "phash": file_hash,
                    "vector": None, "shm_blocks": [], "res": "",
                    "dur": 0.0, "codec": "", "sharpness": 0.0, "fps": 0.0, "watermark": 0.0
                }

    except (UnidentifiedImageError, OSError) as e:
        # Битый/обрезанный/подменённый файл: PIL не опознаёт формат
        # (UnidentifiedImageError) или чтение обрывается (OSError "image file is
        # truncated"). Это ОЖИДАЕМЫЙ скип, а не сбой пайплайна — логируем на debug
        # и возвращаем результат без вектора (файл не попадёт в индекс).
        auditor.debug(f"Skipping unreadable file {file_path}: {e}")
        for block in shm_blocks:
            if block.get("is_shm"):
                try:
                    shared_memory.SharedMemory(name=block['name']).unlink()
                except Exception as ce:
                    auditor.debug(f"Failed to unlink SHM during decode-skip cleanup: {ce}", exc_info=True)
        shm_blocks.clear()
    except Exception as e:
        auditor.warning(f"Worker I/O Error for {file_path}: {e}")
        for block in shm_blocks:
            if block.get("is_shm"):
                try:
                    shared_memory.SharedMemory(name=block['name']).unlink()
                except Exception as e:
                    auditor.debug(f"Failed to unlink SHM during I/O cleanup: {e}", exc_info=True)
        shm_blocks.clear()

    dhash_arr = np.stack(dhashes) if dhashes else None

    return {
        "path": str(file_path), "size": size, "mtime": mtime, "phash": file_hash,
        "vector": vector, "dhash": dhash_arr, "shm_blocks": shm_blocks, "res": res,
        "dur": dur, "codec": codec, "sharpness": sharpness, "fps": fps_val,
        "watermark": watermark
    }

class SmartClusterEngine:
    def __init__(self):
        self.device = HardwareProfiler.get_device()
        self.scan_mode = None
        self.processor = None
        self.model = None
        self.mtcnn = None
        self.resnet = None
        self.current_file_data = []
        self.is_paused = False
        self.is_stopped = False
        self._io_pool = None
        self._scan_cancel_flag = None
        self.faiss_manager = FaissManager(scan_mode="visual")

    def request_scan_abort(self):
        # Called from the GUI/orchestrator thread. Only flip cooperative flags
        # here. The multiprocessing.Pool is owned exclusively by the
        # extract_features() worker thread, which tears it down in its finally
        # block. Calling pool.terminate() from this foreign thread races the
        # imap_unordered consumer and the close()/join() teardown (double
        # teardown / hang) and can SIGKILL workers mid shared-memory create,
        # orphaning /dev/shm segments.
        self.is_stopped = True
        self.is_paused = False
        cf = getattr(self, "_scan_cancel_flag", None)
        if cf is not None:
            try:
                cf.value = 1
            except Exception as e:
                # If the cancel flag can't be set, worker processes never see
                # the abort request and the scan cannot be stopped.
                auditor.warning(f"Failed to set scan cancel flag: {e}", exc_info=True)

    def shutdown_pool(self):
        """Корректно гасит I/O-пул multiprocessing при ЗАКРЫТИИ приложения.

        Вызывается синхронно из MLOrchestrator.stop_all (GUI/orchestrator-поток)
        уже ПОСЛЕ остановки ScannerBridge, поэтому extract_features() свой пул, как
        правило, уже закрыл в finally и self._io_pool == None — тогда метод ничего
        не делает. Если же воркер завис (wait() вышел по таймауту) и пул ещё жив —
        принудительно terminate()/join(): это освобождает внутренние семафоры пула,
        иначе при hard-exit (os._exit) resource_tracker печатает в консоль
        'leaked semaphore objects'.
        """
        self.is_stopped = True
        pool = self._io_pool
        self._io_pool = None
        self._scan_cancel_flag = None
        if pool is not None:
            try:
                pool.terminate()
                # multiprocessing.Pool.join() НЕ принимает timeout и виснет
                # НАВСЕГДА, если воркер застрял в нативном вызове (torch/cv2/faiss)
                # и не умер от SIGTERM — это и есть deadlock при закрытии. Гоним
                # join в daemon-watchdog'е и ждём ограниченно; если не уложился —
                # бросаем пул на os._exit (семафоры всё равно снимет gc.collect +
                # reaping в stop_all), но процесс не зависает.
                import threading
                jt = threading.Thread(target=pool.join, daemon=True)
                jt.start()
                jt.join(2.0)
                if jt.is_alive():
                    auditor.warning("I/O pool join() timeout; abandoning pool to hard-exit")
            except Exception as e:
                auditor.warning(f"I/O pool hard shutdown failed: {e}", exc_info=True)

    def unload_models(self):
        self.processor = None
        self.model = None
        self.mtcnn = None
        self.resnet = None
        # ВНИМАНИЕ: self.scan_mode здесь НЕ обнуляем. unload_models вызывается и
        # idle-таймером (выгрузка весов через 5 мин простоя), а current_file_data
        # при этом СОХРАНЯЕТСЯ. Обнуление scan_mode ломало последующий recluster
        # по ползунку: _filter_dead_entries уходил в meta_v2_visual.db вместо
        # faces (scan_mode or 'visual') и вычищал ВСЕ записи лиц → пустой результат,
        # а early-return для faces (build_clusters) не срабатывал → к лицевым
        # векторам ошибочно применялись геометрия/dHash. load_models всё равно
        # переустанавливает режим (двойной self.scan_mode = mode как guard).

        HardwareProfiler.enforce_garbage_collection(threshold_mb=0.0, force=True)
        auditor.info(f"Models unloaded. Device ({self.device.type}) resources released.")

    def load_models(self, mode="visual"):
        if self.scan_mode == mode and self.model is not None: return

        # Imported lazily here (not at module level) so launching the app — or
        # importing this module for anything other than scanning — does not pull
        # the heavy `transformers` stack into memory until weights are needed.
        #
        # Frozen-bundle guard: transformers decides torch is "absent" purely from
        # its importlib.metadata version, NOT from whether torch imports. If the
        # dist-info wasn't bundled (copy_metadata("torch") in TensorMedia.spec), a
        # physically present torch (FaceNet still works!) is ignored and
        # SiglipVisionModel raises "requires the PyTorch library". Surface the
        # real cause instead of that cryptic message.
        import importlib.metadata as _ilm
        try:
            _ilm.version("torch")
        except _ilm.PackageNotFoundError:
            auditor.error(
                "torch is importable but its package metadata is MISSING from this "
                "build -> transformers will falsely report 'PyTorch not found'. "
                "Rebuild with copy_metadata('torch') (TensorMedia.spec) or run the "
                "published v1.2.1 build, which bundles it."
            )
        import torch
        if hasattr(torch, "compiler") and not hasattr(torch.compiler, "is_compiling"):
            torch.compiler.is_compiling = lambda: False
            
        from transformers import AutoProcessor, SiglipVisionModel

        self.scan_mode = mode
        self.unload_models()
        self.scan_mode = mode
        self.faiss_manager.set_scan_mode(mode)

        auditor.info(f"Loading weights into {self.device.type} for mode: {mode}")

        if mode == "visual":
            siglip_local_path = str(get_models_dir() / "siglip-base-patch16-224")
            self.processor = AutoProcessor.from_pretrained(
                siglip_local_path,
                local_files_only=True,
                use_fast=True
            )
            target_dtype = torch.float16 if self.device.type in ("cuda", "mps") else torch.float32
            
            # low_cpu_mem_usage=True: модель инициализируется на meta-устройстве,
            # а веса стримятся из safetensors сразу в целевые тензоры. Это убирает
            # промежуточную random-init аллокацию полного fp32-графа (~372 МБ для
            # vision-tower SigLIP base) и примерно вдвое срезает пик RAM в момент
            # загрузки. Резидентный footprint остаётся ~186 МБ (fp16) — это норма.
            try:
                self.model = SiglipVisionModel.from_pretrained(
                    siglip_local_path,
                    local_files_only=True,
                    torch_dtype=target_dtype,
                    low_cpu_mem_usage=True
                ).eval().to(self.device)
            except Exception as e:
                auditor.error(f"Failed to load to {self.device.type} with {target_dtype}: {e}. Falling back to CPU.")
                self.device = torch.device("cpu")
                self.model = SiglipVisionModel.from_pretrained(
                    siglip_local_path,
                    local_files_only=True,
                    torch_dtype=torch.float32,
                    low_cpu_mem_usage=True
                ).eval().to(self.device)
                
        elif mode == "faces":
            os.environ["TORCH_HOME"] = str(get_models_dir() / "torch")
            try:
                import sys
                from facenet_pytorch import MTCNN, InceptionResnetV1
                
                if getattr(sys, 'frozen', False):
                    import facenet_pytorch.models.mtcnn as mtcnn_module
                    if hasattr(sys, '_MEIPASS'):
                        base_dir = Path(sys._MEIPASS)
                    else:
                        base_dir = Path(sys.executable).parent.parent / "Resources"
                    
                    mtcnn_module.os.path.dirname = lambda x: str(base_dir / "facenet_pytorch" / "data")
                
                # КРИТИЧЕСКИЙ ПАТЧ: MTCNN падает на MPS с ошибкой 'Adaptive pool MPS: input sizes must be divisible by output sizes'
                # Переводим инициализацию MTCNN принудительно на CPU. Это легкая сеть, скорость не пострадает.
                self.mtcnn = MTCNN(keep_all=False, margin=32, thresholds=[0.5, 0.6, 0.6], device=torch.device("cpu"))
                self.resnet = InceptionResnetV1(pretrained='vggface2').eval().to(self.device)
            except ImportError:
                auditor.critical("Module facenet-pytorch missing.")
                self.scan_mode = "error"



    def _compute_vector_batch(self, images: list) -> list:
        if not images: return []
        
        if self.scan_mode == "faces":
            results = [None] * len(images)
            if self.mtcnn is None or self.resnet is None: return results
            
            try:
                for i, img in enumerate(images):
                    if self.is_stopped: break
                    try:
                        # Включаем возврат вероятности для отсечения ложных срабатываний
                        face_tensor, prob = self.mtcnn(img, return_prob=True)
                        if face_tensor is not None and prob is not None and prob > 0.95:
                            with torch.inference_mode():
                                emb = self.resnet(face_tensor.unsqueeze(0).to(self.device))
                                emb_norm = torch.nn.functional.normalize(emb, p=2, dim=-1)
                                results[i] = emb_norm.cpu().numpy().astype(np.float32)[0]
                    except Exception as e:
                        auditor.warning(f"Failed to process face extraction for image index {i}: {e}")
            except Exception as e:
                auditor.error(f"Face vector extraction failed: {e}")
            return results
            
        elif self.scan_mode == "visual":
            try:
                def run_on_device(dev):
                    all_f_norms = []
                    chunk_size = 32 
                    for i in range(0, len(images), chunk_size):
                        if self.is_stopped: break
                        chunk = images[i:i+chunk_size]
                        if self.processor is None or self.model is None:
                            raise RuntimeError("NPU Engine not initialized")
                            
                        inputs = self.processor(images=chunk, return_tensors="pt")
                        pixel_values = inputs["pixel_values"]
                        
                        target_dtype = torch.float16 if dev.type in ("cuda", "mps") else torch.float32
                        
                        if dev.type == "cuda":
                            pixel_values = pixel_values.pin_memory().to(dev, non_blocking=True, dtype=target_dtype)
                        else:
                            pixel_values = pixel_values.to(dev, dtype=target_dtype)
                
                        with torch.inference_mode():
                            outputs = self.model(pixel_values=pixel_values)
                            f = outputs.pooler_output
                            f_norm = torch.nn.functional.normalize(f, p=2, dim=-1)
                            all_f_norms.extend(f_norm.cpu().numpy().astype(np.float32))
                        
                        del inputs, pixel_values, outputs, f, f_norm
                    return all_f_norms

                try:
                    return run_on_device(self.device)
                except Exception as e:
                    auditor.error(f"H/W NPU Fail: {e}. Fallback to CPU execution.")
                    if self.model is None:
                        raise RuntimeError("Model was forcefully unloaded from memory.")
                    self.device = torch.device("cpu")
                    self.model = self.model.to("cpu")
                    return run_on_device(self.device)
            except Exception as e:
                auditor.critical(f"FATAL NPU ERROR in vectorization: {e}")
                raise e 
        return [None] * len(images)

    def extract_features(self, target_dirs: list, allowed_exts: set = None, progress_callback=None) -> list:
        # ИЗОЛЯЦИЯ ПРОЦЕССОВ (spawn): utils.i18n тянет PySide6 (QObject/QSettings).
        # Этот импорт ОБЯЗАН оставаться локальным. Воркеры Pool импортируют данный
        # модуль для process_single_file_io, прогоняя его глобальную область; любой
        # PySide6 на уровне модуля вызвал бы рекурсивный импорт и краш Shiboken в
        # дочернем процессе. translator используется только здесь, в главном процессе.
        from utils.i18n import translator
        self.is_paused = False
        self.is_stopped = False
        self.current_file_data = []

        if self._scan_cancel_flag is not None:
            self._scan_cancel_flag.value = 0
        
        if progress_callback: progress_callback(0, 0, "Indexing disk...")
            
        def fast_scandir(directory):
            discovered = []
            try:
                for entry in os.scandir(directory):
                    if self.is_stopped: break
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'):
                        discovered.extend(fast_scandir(entry.path))
                    elif entry.is_file(follow_symlinks=False) and not entry.name.startswith('.'):
                        ext = os.path.splitext(entry.name)[1].lower()
                        if allowed_exts and ext not in allowed_exts: continue
                        # normpath: на Windows os.scandir может вернуть смешанные
                        # разделители ('C:/dir\\file'), а путь — это ПЕРВИЧНЫЙ
                        # КЛЮЧ SQLite-кэша и FAISS-подписи. Без канонизации один
                        # файл живёт в кэше под двумя ключами ('/' и '\\') и
                        # инвалидация при удалении промахивается.
                        discovered.append(Path(os.path.normpath(entry.path)))
            except PermissionError as e:
                auditor.warning(f"Permission denied while scanning directory {directory}: {e}")
            return discovered

        files = []
        for d in target_dirs:
            # Канонизация корня скана (UTF-8 строки Python безопасны для NTFS;
            # критичен только единый разделитель — см. комментарий в fast_scandir).
            d = os.path.normpath(os.path.abspath(str(d)))
            p_dir = Path(d)
            if p_dir.is_dir():
                files.extend(fast_scandir(d))
                
        if not files: return self.current_file_data
        
        if progress_callback: progress_callback(0, 0, f"Found: {len(files)} files...")
        
        db_name = f"meta_v2_{self.scan_mode}.db"
        cache_db = DBConnectionPool.get_connection(db_name)
        
        file_strs = [str(f) for f in files]
        meta_cache = cache_db.get_metadata_for_paths(file_strs)

        if progress_callback: progress_callback(0, len(files), translator.tr("scan_io"))
        
        tasks, all_results = [], []

        # ПЕРВЫЙ ПРОХОД: stat + классификация (кэш-хит / требует обработки). Вектор
        # здесь НЕ тянем — раньше тут был get_vector() на КАЖДЫЙ кэшированный файл,
        # а это N барьеров sync() и N свежих sqlite-соединений (O(N) тормоз на
        # повторном скане библиотеки). Теперь хиты собираем и забираем одним батчем.
        hit_meta = {}        # file_str -> (size, mtime, c_m)
        pending_files = []   # (file_path, size, mtime) — на обработку пулом
        for file_path in files:
            if self.is_stopped: break
            try:
                stat = file_path.stat()
                size, mtime = stat.st_size, stat.st_mtime
                if size == 0: continue

                file_str = str(file_path)
                c_m = meta_cache.get(file_str)
                if c_m and c_m['size'] == size and c_m['mtime'] == mtime:
                    hit_meta[file_str] = (size, mtime, c_m)
                else:
                    pending_files.append((file_path, size, mtime))
            except Exception as e:
                auditor.warning(f"Failed to prepare file task for {file_path}: {e}")
                continue

        # ОДНА батч-выборка векторов для всех кэш-хитов (вместо N поштучных).
        cached_vectors = cache_db.get_vectors_for_paths(list(hit_meta.keys())) if hit_meta else {}
        for file_str, (size, mtime, c_m) in hit_meta.items():
            vec_dict = cached_vectors.get(file_str)
            if vec_dict is not None and isinstance(vec_dict, dict):
                v_data = vec_dict.get("vector")
                dh_data = vec_dict.get("dhash")
                if v_data is not None:
                    all_results.append({
                        "path": file_str, "size": size, "mtime": mtime, "phash": c_m['phash'],
                        "vector": v_data, "dhash": dh_data, "shm_blocks": [], "res": c_m.get('res', ''),
                        "dur": c_m.get('dur', 0.0), "codec": c_m.get('codec', ''),
                    "sharpness": c_m.get('sharpness', 0.0), "fps": c_m.get('fps', 0.0),
                    "watermark": c_m.get('watermark', 0.0)
                })
            else:
                # Метаданные в кэше есть, а вектор отсутствует — переотправляем
                # файл на обработку (тот же путь, что и для кэш-промаха).
                pending_files.append((Path(file_str), size, mtime))

        if progress_callback:
            progress_callback(len(all_results), len(files), translator.tr("scan_cache"))

        # Хэш в главном потоке БОЛЬШЕ НЕ считаем (был серийный I/O-пре-пасс до
        # 100 МБ/файл до старта пула). Передаём None — blake3 посчитается внутри
        # воркера process_single_file_io, распараллелившись по ядрам пула.
        for file_path, size, mtime in pending_files:
            if self.is_stopped: break
            tasks.append((file_path, size, mtime, None, None, self.scan_mode))

        vram_gb = 0
        if self.device.type == "cuda":
            try: vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            except Exception as e: auditor.debug(f"VRAM detection failed: {e}", exc_info=True)

        chunk_size = 256
        batch_size = 64 if vram_gb >= 8 else 32
        if self.device.type == "cpu": batch_size = 16
            
        bypassed_count = len(all_results)
        available_ram_mb = psutil.virtual_memory().available / (1024 * 1024)
        safe_workers = max(1, int(available_ram_mb // 1500))
        max_workers = min(max(1, os.cpu_count() - 1 if os.cpu_count() else 1), safe_workers)
        
        # lock=False → RawValue поверх анонимного mmap-арены, БЕЗ POSIX-семафора.
        # Value(lock=True) создаёт SemLock, который gc.collect() лишь sem_close'ит,
        # но НЕ sem_unlink'ает; при финальном os._exit(0) resource_tracker считает
        # его «утёкшим» и печатает 'leaked semaphore objects'. Флаг отмены —
        # одиночный int: пишет только главный поток (0/1), читают воркеры; гонок
        # нет, блокировка не нужна, поэтому семафор тут лишний.
        cancel_flag = Value('i', 0, lock=False)
        self._scan_cancel_flag = cancel_flag
        
        pool = None
        try:
            pool = Pool(
                processes=max_workers,
                initializer=_scan_pool_init,
                initargs=(cancel_flag,),
                maxtasksperchild=64, 
            )
            self._io_pool = pool
            
            for chunk_start in range(0, len(tasks), chunk_size):
                if self.is_stopped: break
                chunk_tasks = tasks[chunk_start : chunk_start + chunk_size]
                if not chunk_tasks:
                    continue
                chunk_results = []

                try:
                    for res in pool.imap_unordered(process_single_file_io, chunk_tasks, chunksize=1):
                        while self.is_paused and not self.is_stopped:
                            time.sleep(0.1)
                        if self.is_stopped:
                            break
                        if res:
                            chunk_results.append(res)
                        if progress_callback and res:
                            progress_callback(
                                bypassed_count + chunk_start + len(chunk_results),
                                len(files),
                                f"{translator.tr('scan_npu')}{Path(res['path']).name}",
                            )
                except Exception as e:
                    if self.is_stopped:
                        auditor.debug(f"Scan I/O pool iteration ended: {e}")
                    else:
                        auditor.error(f"Scan I/O pool error: {e}")

                if self.is_stopped:
                    for r in chunk_results:
                        _unlink_shm_blocks(r.get('shm_blocks', []))
                    break

                needs_vector = [r for r in chunk_results if r['vector'] is None and len(r.get('shm_blocks', [])) > 0]

                # Guarantee every shared-memory segment for this chunk is freed
                # on EVERY exit path: normal completion, a pause/stop break, or
                # a raise out of _compute_vector_batch (FATAL NPU). Previously a
                # raise here unwound past the loop and the bare
                # `shm_blocks = []` reset dropped references without unlinking,
                # leaking /dev/shm segments for all unprocessed batches.
                try:
                    for i in range(0, len(needs_vector), batch_size):
                        while self.is_paused and not self.is_stopped:
                            time.sleep(0.1)
                        if self.is_stopped:
                            break

                        batch = needs_vector[i:i+batch_size]
                        flat_images, counts = [], []
                        for b in batch:
                            imgs = []
                            for shm_meta in b['shm_blocks']:
                                if shm_meta.get("is_shm"):
                                    shm = None
                                    try:
                                        shm = shared_memory.SharedMemory(name=shm_meta['name'])
                                        arr = np.ndarray(shm_meta['shape'], dtype=shm_meta['dtype'], buffer=shm.buf)
                                        imgs.append(Image.fromarray(arr.copy()))
                                        shm.close()
                                        shm.unlink()
                                    except Exception as e:
                                        auditor.error(f"SHM Read Fault: {e}")
                                        if shm is not None:
                                            try:
                                                shm.unlink()
                                            except Exception as e:
                                                auditor.debug(f"Failed to unlink SHM after read fault: {e}", exc_info=True)
                                else:
                                    try:
                                        arr = np.frombuffer(shm_meta['data'], dtype=shm_meta['dtype']).reshape(shm_meta['shape'])
                                        imgs.append(Image.fromarray(arr.copy()))
                                    except Exception as e:
                                        auditor.error(f"Critical failure parsing SHM fallback data: {e}", exc_info=True)
                            flat_images.extend(imgs)
                            counts.append(len(imgs))

                        flat_vectors = self._compute_vector_batch(flat_images)

                        idx = 0
                        for b, count in zip(batch, counts):
                            file_vecs = flat_vectors[idx:idx+count]
                            idx += count
                            valid_vecs = [v for v in file_vecs if v is not None]
                            if valid_vecs:
                                # КРИТИЧЕСКИЙ ПАТЧ: ОТКАЗ ОТ УСРЕДНЕНИЯ
                                # Сохраняем все валидные векторы (до 5 шт) как единый 2D-массив
                                b['vector'] = np.stack(valid_vecs)
                            else:
                                b['vector'] = None

                        del flat_images, flat_vectors, batch
                finally:
                    for r in chunk_results:
                        _unlink_shm_blocks(r.get('shm_blocks', []))
                        r['shm_blocks'] = []

                insert_batch = []
                for r in chunk_results:
                    if r['vector'] is not None:
                        insert_batch.append((
                            str(r['path']), int(r['size']), float(r['mtime']), str(r['phash']),
                            str(r['res']), float(r['dur']), str(r['codec']), float(r['sharpness']),
                            float(r['fps']), r['vector'], r.get('dhash'),
                            float(r.get('watermark', 0.0) or 0.0)
                        ))
                if insert_batch:
                    cache_db.save_batch(insert_batch)
                all_results.extend(chunk_results)
        finally:
            self._scan_cancel_flag = None
            self._io_pool = None
            if pool is not None:
                try:
                    if self.is_stopped:
                        pool.terminate()
                    else:
                        pool.close()
                        pool.join()
                except Exception as ex:
                    auditor.warning(f"I/O pool shutdown: {ex}")
                # Drop the last reference and force a collection HERE, while the
                # process is healthy, so the Pool's internal SemLock finalizers
                # run sem_unlink now instead of deferring to the gc.collect() that
                # sits right before os._exit in MLOrchestrator.stop_all — there it
                # races the resource_tracker and leaves the benign trailing
                # 'leaked semaphore objects' warning at shutdown.
                pool = None
                gc.collect()

        if self.is_stopped: 
            return []
            
        if progress_callback: progress_callback(len(files), len(files), translator.tr("scan_faiss"))

        for r in all_results:
            if r['vector'] is not None:
                self.current_file_data.append({
                    "path": r['path'], "phash": r['phash'], "vector": r['vector'], "dhash": r.get('dhash'),
                    "size": r['size'], "resolution": r['res'], "duration": r['dur'],
                    "codec": r['codec'], "sharpness": r['sharpness'], "fps": r['fps'], "mtime": r['mtime'],
                    "watermark": r.get('watermark', 0.0)
                })

        return self.current_file_data

    # ------------------------------------------------------------------
    # КАСКАДНЫЙ ГИБРИДНЫЙ ФИЛЬТР (Stage 0-3)
    #
    # Stage 0 — «мёртвые души»: каждый кандидат, который FAISS способен
    #   вернуть при смене порога, валидируется по физическому существованию
    #   файла (os.path.exists) И по наличию живой записи в SQLite-кэше
    #   (запись вычищается в момент удаления через _invalidate_cache_paths,
    #   т.е. её отсутствие == статус 'deleted'). Без этого in-memory список
    #   current_file_data и дисковый .npy-кэш FAISS «воскрешали» удалённые
    #   файлы при каждом recluster по ползунку.
    # Stage 1 — семантическое сито: kNN-выборка FAISS по косинусной близости
    #   (см. FaissManager.build_clusters; векторы L2-нормированы, IP==cosine).
    # Stage 2 — геометрия: сравнение площади в пикселях и соотношения сторон.
    #   Совпавшая композиция при разном разрешении → 'quality' («дубликаты
    #   разного качества»); элемент с максимальным разрешением получает
    #   keep_priority=True (приоритет сохранения).
    # Stage 3 — детерминированное попиксельное подтверждение: пограничные по
    #   similarity пары проверяются 64-битным градиентным dHash (OpenCV/PIL).
    #   Это отсекает ложные срабатывания эмбеддингов, инвариантных к мелким
    #   деталям (однородные текстуры, схожая композиция разных кадров).
    # ------------------------------------------------------------------
    ASPECT_TOLERANCE = 0.03   # относительный допуск соотношения сторон (Stage 2)


    def _filter_dead_entries(self) -> list:
        """Stage 0: выбрасывает из current_file_data записи удалённых файлов."""
        # ОПТИМИЗАЦИЯ: Полное удаление блокирующего O(N) os.path.exists и 
        # SQL-запроса из UI-потока. Это устраняет фризы при перетаскивании ползунка 
        # на больших библиотеках. Файлы уже валидны на момент сканирования,
        # а инвалидацию при удалении отрабатывает watchdog и fs_service.
        return self.current_file_data

    @staticmethod
    def _parse_resolution(res_str):
        try:
            w, h = str(res_str).lower().split("x")
            w, h = int(w), int(h)
            return (w, h) if w > 0 and h > 0 else None
        except (ValueError, AttributeError):
            return None

    def _annotate_geometry(self, cluster: list) -> None:
        """Stage 2: маркировка пар по разрешению/соотношению сторон."""
        base = cluster[0]  # FaissManager сортирует кластер по similarity desc
        base_res = self._parse_resolution(base.get("resolution"))

        def _area(item):
            r = self._parse_resolution(item.get("resolution"))
            return (r[0] * r[1]) if r else 0

        for item in cluster:
            item.setdefault("dup_kind", "reference" if item is base else "semantic")
            if item is not base and item.get("phash") and item["phash"] == base.get("phash"):
                item["dup_kind"] = "exact"

        if base_res:
            base_area = base_res[0] * base_res[1]
            base_ar = base_res[0] / base_res[1]
            for item in cluster[1:]:
                r = self._parse_resolution(item.get("resolution"))
                if not r:
                    continue
                ar = r[0] / r[1]
                # Та же композиция (вектор сошёлся) и те же пропорции, но другая
                # площадь → «дубликат разного качества».
                if abs(ar - base_ar) <= self.ASPECT_TOLERANCE * base_ar and r[0] * r[1] != base_area:
                    item["dup_kind"] = "quality"

        # Приоритет сохранения: максимум пикселей, при равенстве — больший файл.
        best = max(cluster, key=lambda it: (_area(it), it.get("size", 0)))
        for item in cluster:
            item["keep_priority"] = item is best

    def build_clusters(self, threshold: float) -> list:
        active = self._filter_dead_entries()
        clusters = self.faiss_manager.build_clusters(
            list(active), threshold, self.scan_mode,
        )
        # FaceNet-эмбеддинги описывают ЛИЦО, а не кадр — геометрия для них не имеет
        # смысла, отдаём кластеры как есть.
        if self.scan_mode == "faces":
            return clusters

        # Структурную сверку dHash теперь ПОЛНОСТЬЮ выполняет FaissManager на
        # КЭШИРОВАННЫХ покадровых хэшах (item["dhash"]) — без повторного чтения
        # файлов с диска. Прежний Stage 3 (_pixel_confirm/_dhash64/_load_gray_small)
        # удалён: он дублировал ту же сверку, заново читал картинки с диска (тот же
        # фриз UI, что убрали из Stage 0) и конфликтовал по математике порога
        # (старая формула 0.95+0.05·t² против новой калибровки FaissManager).
        # Остаётся лишь дешёвая геометрическая разметка (dup_kind/keep_priority),
        # без обращения к диску.
        for cluster in clusters:
            self._annotate_geometry(cluster)                       # Stage 2
        return clusters
