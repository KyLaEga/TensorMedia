# ============================================================
# MODULE: utils/image_io.py
# ============================================================
"""HEIF/HEIC opener registration + PIL→QImage bridge.

Единая точка двух связанных задач:
  1. register_heif() — подключает PIL-декодер pillow-heif, без которого
     Image.open(".heic"/".heif") бросает UnidentifiedImageError (а HEIC — это
     дефолтный формат фото iPhone, т.е. большая часть библиотеки на macOS).
  2. pil_to_qimage() — конвертирует PIL.Image в QImage, владеющий своими байтами
     (для GUI-превью HEIC, которое Qt-декодер не всегда тянет).

ИНВАРИАНТ ПОТОКА: Qt импортируется ЛЕНИВО — только внутри pil_to_qimage. Поэтому
модуль безопасно импортировать из multiprocessing spawn-воркеров
(core/ml/cluster_engine зовёт register_heif на уровне модуля), которым НЕЛЬЗЯ
тянуть PySide6 в дочерний процесс (рекурсивный импорт shiboken → краш). Сама
register_heif трогает только PIL + pillow_heif (без Qt) и идемпотентна.
"""

_heif_registered = False


def register_heif() -> bool:
    """Регистрирует PIL-опенер pillow-heif (декод .heic/.heif). Идемпотентно и
    защищённо: отсутствие плагина деградирует до «HEIC не поддержан», а не роняет
    процесс. Возвращает True, если поддержка HEIF активна."""
    global _heif_registered
    if _heif_registered:
        return True
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        _heif_registered = True
    except Exception:
        pass
    return _heif_registered


def pil_to_qimage(im, max_side: int = 1280):
    """PIL.Image → QImage, владеющий собственными байтами (.copy() отвязывает от
    буфера PIL). Учитывает EXIF-поворот (телефонные портреты) и опционально
    даунскейлит по длинной стороне. Qt импортируется здесь намеренно — лениво
    (см. инвариант потока в докстринге модуля)."""
    from PySide6.QtGui import QImage
    from PIL import ImageOps
    im = ImageOps.exif_transpose(im) or im
    if im.mode != "RGB":
        im = im.convert("RGB")
    if max_side and (im.width > max_side or im.height > max_side):
        im = im.copy()
        im.thumbnail((max_side, max_side))
    data = im.tobytes("raw", "RGB")
    return QImage(data, im.width, im.height, im.width * 3,
                  QImage.Format.Format_RGB888).copy()
