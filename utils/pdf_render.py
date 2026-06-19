# ============================================================
# MODULE: utils/pdf_render.py
# ============================================================
"""Единая точка рендеринга PDF — через pypdfium2 (движок PDFium, как в Chrome).

Заменяет PyMuPDF/fitz по двум причинам:
  1. Лицензия. pypdfium2 — BSD-3-Clause / Apache-2.0 (свободна для коммерческого
     закрытого ПО), тогда как PyMuPDF — AGPL-3.0 / платная коммерческая.
  2. Упаковка. Нативная часть pypdfium2 — один самодостаточный модуль
     (pypdfium2_raw с pdfium.dylib/.so/.dll), который надёжно собирается
     PyInstaller и работает в multiprocessing-worker'ах БЕЗ Qt event loop
     (критично для скан-воркера в core/ml/cluster_engine). PyMuPDF тянул набор
     опциональных нативных под-модулей (mupdf/cppyy/pymupdf_fonts), часть которых
     отваливалась в замороженном бандле — превью PDF падало в собранном .app/.exe.

API сведён к тому, что реально нужно приложению: открыть документ, узнать число
страниц и растеризовать страницу в PIL.Image('RGB'). Все вызовы старого fitz
(cluster_engine, ui/workers, ui/components/dialogs, ui/components/discrete_preview)
делали ровно это.
"""
import pypdfium2 as pdfium
from PIL import Image


def open_document(path):
    """Открывает PDF и возвращает pypdfium2.PdfDocument.

    Вызывающий ОБЯЗАН закрыть документ (.close()) — как и прежний fitz.open().
    """
    return pdfium.PdfDocument(str(path))


def render_page(doc, index: int, scale: float) -> Image.Image:
    """Растеризует страницу `index` документа в независимый PIL.Image('RGB').

    `scale` задаёт плотность пикселей так же, как fitz.Matrix(scale, scale):
    базовая точка — 72 DPI (scale=1.0 → 72 DPI, 2.0 → 144 DPI). Совпадение
    размеров с прежним fitz при равном scale проверено.

    PDFium по умолчанию отдаёт буфер в BGR-порядке, но bitmap.to_pil()
    возвращает корректный RGB; convert('RGB') дополнительно гарантирует режим и
    ОТВЯЗЫВАЕТ изображение от буфера битмапа — поэтому и страницу, и битмап
    безопасно закрыть сразу, не дожидаясь GC (важно для пакетного скана тысяч
    файлов: иначе нативная память pdfium накапливалась бы до сборки мусора).
    """
    page = doc[index]
    try:
        bitmap = page.render(scale=scale)
        try:
            return bitmap.to_pil().convert("RGB")
        finally:
            bitmap.close()
    finally:
        page.close()
