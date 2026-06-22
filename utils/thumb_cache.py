# ============================================================
# MODULE: utils/thumb_cache.py
# ============================================================
"""Дисковый кэш статических миниатюр предпросмотра.

Зачем: вытаскивать кадр видео «вживую» через cv2 (open контейнера + seek по
POS_FRAMES + decode от keyframe) ДОРОГО, особенно на macOS/AVFoundation и для 4K
— отсюда «превью долго грузится». Кэш кладёт один JPEG-кадр на файл в app-data и
на повторных показах читается мгновенно (маленький JPEG вместо парса контейнера).

Заполняется С ДВУХ сторон:
  • при СКАНЕ (cluster_engine: уже декодирует кадры для векторизации — сохраняем
    репрезентативный задаром);
  • ЛЕНИВО при первом показе (CompareVideoWorker сохраняет добытый кадр).

ИНВАРИАНТ ПОТОКА: модуль Qt-CHIST (только os/hashlib/cv2/PIL лениво внутри
функций) — безопасен для импорта в multiprocessing spawn-воркерах, как
utils.image_io. get_data_dir() из env_config тоже Qt-чист.

Ключ = md5(normpath(abspath(path)) | size | mtime). И скан, и превью считают его
ОДИНАКОВО (нормализация внутри), поэтому ключи совпадают; смена файла (другой
size/mtime) → другой ключ → старый thumb игнорируется и пере-генерируется.
"""
import os
import hashlib


def _thumbs_dir():
    from utils.env_config import get_data_dir
    d = get_data_dir() / "thumbs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def thumb_path_for(src_path, size=None, mtime=None):
    """Путь к JPEG-миниатюре для src_path. None — если файл недоступен (stat упал)
    и size/mtime не переданы явно."""
    try:
        norm = os.path.normpath(os.path.abspath(str(src_path)))
        if size is None or mtime is None:
            st = os.stat(norm)
            size, mtime = st.st_size, st.st_mtime
    except OSError:
        return None
    key = hashlib.md5(
        f"{norm}|{int(size)}|{mtime}".encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return _thumbs_dir() / f"{key}.jpg"


def _atomic_write_jpeg(dst_path, write_fn):
    """write_fn(tmp_str)->bool: пишет JPEG во временный файл; при успехе атомарно
    переименовываем. Любой сбой проглатываем — кэш сугубо опциональный."""
    if dst_path is None:
        return
    tmp = str(dst_path) + ".tmp"
    try:
        if write_fn(tmp):
            os.replace(tmp, dst_path)
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


def save_thumb_bgr(dst_path, frame_bgr, max_side=640):
    """Сохранить cv2-кадр (BGR ndarray) как миниатюру (используется воркером превью)."""
    if dst_path is None or frame_bgr is None:
        return
    import cv2
    try:
        h, w = frame_bgr.shape[:2]
        if max(h, w) > max_side:
            s = max_side / float(max(h, w))
            frame_bgr = cv2.resize(frame_bgr, (max(1, int(w * s)), max(1, int(h * s))),
                                   interpolation=cv2.INTER_AREA)
        # imencode (а НЕ imwrite в .tmp): imwrite выбирает кодек по РАСШИРЕНИЮ файла,
        # а атомарная запись идёт во временный '*.jpg.tmp' (расширение .tmp) → imwrite
        # падает «could not find a writer». Кодируем явным '.jpg' и пишем байты.
        ok, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return
        payload = buf.tobytes()
    except Exception:
        return

    def _w(tmp):
        with open(tmp, "wb") as f:
            f.write(payload)
        return True
    _atomic_write_jpeg(dst_path, _w)


def save_thumb_pil(dst_path, pil_img, max_side=640):
    """Сохранить PIL.Image как миниатюру (используется сканом — там кадры в PIL)."""
    if dst_path is None or pil_img is None:
        return
    def _w(tmp):
        im = pil_img.convert("RGB")
        im.thumbnail((max_side, max_side))
        im.save(tmp, "JPEG", quality=85)
        return True
    _atomic_write_jpeg(dst_path, _w)
