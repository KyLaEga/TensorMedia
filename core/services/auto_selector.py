# ============================================================
# MODULE: core/services/auto_selector.py
# ============================================================
import os
from PySide6.QtCore import QThread, Signal

def calculate_smart_score(item_data):
    """Эвристическая функция для оценки приоритета сохранения медиафайла."""
    score = 0.0

    # 1. Вес размера (в МБ)
    score += item_data.get('size', 0) / (1024 * 1024)

    # 2. Вес разрешения экрана
    res = item_data.get('resolution', '')
    if res and 'x' in res:
        try:
            w, h = map(int, res.split('x'))
            score += (w * h) / 1000000.0 * 50
        except Exception as e:
            from utils.logger import auditor
            auditor.warning(f"Failed to parse resolution '{res}': {e}")
            pass

    # 3. Вес длительности для видеофайлов
    score += item_data.get('duration', 0.0) * 10

    # 4. Вес резкости изображения (sharpness)
    sharpness = item_data.get('sharpness', 0.0)
    if sharpness > 0:
        # Резкость может варьироваться от 0 до нескольких тысяч в зависимости от контента
        # Логарифмируем, чтобы избежать экстремальных перекосов
        import math
        score += math.log1p(sharpness) * 20.0

    return score


def _resolution_area(item_data):
    """Площадь кадра в пикселях (0, если разрешение неизвестно)."""
    res = item_data.get('resolution', '')
    if res and 'x' in res:
        try:
            w, h = map(int, res.split('x'))
            return w * h
        except (ValueError, TypeError):
            return 0
    return 0


from functools import lru_cache


@lru_cache(maxsize=8192)
def _ratio_from_thumb(thumb_str):
    """Доля кадра БЕЗ чёрных рамок по конкретной миниатюре. Кэш по ПУТИ миниатюры
    (он содержит size|mtime → при изменении файла ключ другой, устаревания нет)."""
    try:
        import cv2
        img = cv2.imread(thumb_str, cv2.IMREAD_GRAYSCALE)
        if img is None or img.size == 0:
            return 1.0
        h, w = img.shape
        # Колонка/строка «контентная», если её СРЕДНЯЯ яркость заметно выше нуля
        # (чистая чёрная рамка ≈ 0). mean устойчивее max к одиночному пикселю
        # субтитра на полосе.
        cw = int((img.mean(axis=0) > 8.0).sum())
        ch = int((img.mean(axis=1) > 8.0).sum())
        if cw == 0 or ch == 0:
            return 1.0
        ratio = (cw / w) * (ch / h)
        # Подозрительно маленький контент → вероятно просто тёмный кадр, а не
        # рамки: не доверяем оценке, не штрафуем файл.
        return ratio if ratio >= 0.40 else 1.0
    except Exception:
        return 1.0


def _content_ratio(item_data):
    """Доля кадра БЕЗ чёрных рамок (letterbox/pillarbox), 0..1; 1.0 — рамок нет
    или оценить нельзя. Берём из дискового кэша миниатюр (utils.thumb_cache):
    миниатюра — реальный кадр файла, значит несёт те же рамки, что и видео.

    Зачем: каскад выбора оригинала ранжирует по ПЛОЩАДИ пикселей, и файл 1280×840,
    где реально 850 пикселей контента + чёрные полосы по бокам, ложно обыгрывал
    честные 850×640. Эффективная площадь (area·ratio) сравнивает по КОНТЕНТУ."""
    try:
        from utils.thumb_cache import thumb_path_for
        tp = thumb_path_for(item_data.get('path', ''))
        if tp is None or not tp.exists():
            return 1.0
        return _ratio_from_thumb(str(tp))
    except Exception:
        return 1.0


# Вотермарка: score 0..~0.05 (cluster_engine.estimate_watermark_score). Мёртвая
# зона отсекает шумовой пол (чистые файлы не штрафуем и не «дёргаем» на нём ничьи),
# выше — штраф к эффективной площади (брендированный re-upload крадёт чистый контент,
# как и чёрные полосы). CAP 0.30 не даёт вотермарке в одиночку перебить целый тир
# разрешения (4K с лого всё ещё > чистого 1080p), но РЕШАЕТ при равном разрешении —
# ровно жалоба «выбирает где вотермарка больше».
WATERMARK_DEADZONE = 0.012
WATERMARK_GAIN = 12.0
WATERMARK_PENALTY_CAP = 0.30


def _watermark_penalty(item_data):
    wm = item_data.get('watermark', 0.0) or 0.0
    if wm <= WATERMARK_DEADZONE:
        return 0.0
    return min(WATERMARK_PENALTY_CAP, (wm - WATERMARK_DEADZONE) * WATERMARK_GAIN)


def _effective_area(item_data):
    """Площадь РЕАЛЬНОГО ЧИСТОГО контента: пиксели матрицы за вычетом чёрных полос
    и со штрафом за вотермарку."""
    return (_resolution_area(item_data)
            * _content_ratio(item_data)
            * (1.0 - _watermark_penalty(item_data)))


def calculate_quality_score(item_data):
    """Оценка визуального качества: эффективное разрешение (без рамок) + резкость."""
    return _effective_area(item_data) + item_data.get('sharpness', 0.0)


# ── Каскадная матрица приоритетов (Pareto-cascade) ──────────────────────────
# В отличие от взвешенной суммы (strat_smart) и одноключевых стратегий, здесь
# критерии применяются СТРОГО по убыванию значимости: следующий учитывается
# только при паритете предыдущего. Это «оставить технически лучший оригинал»
# без риска, что мелкий, но «резкий» файл перевесит большее разрешение.
#
# KEEP_NEWER управляет Критерием 3 (хронология): по умолчанию при равенстве
# техники оставляем СТАРЫЙ файл (исходник). Это лишь ДЕФОЛТ/фолбэк — реальное
# значение для кнопки «Авто» AutoSelectWorker читает динамически из
# QSettings("TensorMedia","Arbitrage") ключом 'auto_select/keep_newer'
# (см. _read_keep_newer). Константа остаётся точкой отказа, когда настройки
# недоступны, и дефолтом для прямых/тестовых вызовов _cascade_sort_key.
KEEP_NEWER = False


def _read_keep_newer():
    """Динамическое чтение Критерия 3 (хронология) из пользовательских настроек.

    Хранилище — QSettings("TensorMedia","Arbitrage") (то же, где главное окно
    держит geometry/splitters), ключ 'auto_select/keep_newer'. По умолчанию
    False — оставлять СТАРЫЙ файл-исходник. Значение может прийти строкой
    (INI-бэкенд) или int (Windows-реестр), поэтому коэрсим явно; любой сбой
    доступа к QSettings → дефолтная константа KEEP_NEWER."""
    try:
        from PySide6.QtCore import QSettings
        raw = QSettings("TensorMedia", "Arbitrage").value(
            "auto_select/keep_newer", KEEP_NEWER
        )
    except Exception:
        return KEEP_NEWER
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return bool(raw)

# Критерий 4: бело-/чёрные списки директорий. Сравнение регистронезависимое
# по подстроке нормализованного пути. Файл в PROTECTED_DIRS не может быть
# выбран на удаление, пока в кластере есть хоть один незащищённый кандидат;
# файл из BLACKLIST_DIRS уходит под нож первым.
PROTECTED_DIRS = ("/archive/", "/originals/", "/masters/")
BLACKLIST_DIRS = ("/downloads/", "/telegram/", "/whatsapp/", "/cache/", "/tmp/")


def _path_in(item_data, needles):
    p = os.path.normpath(item_data.get('path', '')).replace(os.sep, '/').lower()
    p = '/' + p.strip('/') + '/'
    return any(n in p for n in needles)


# Допуск равенства разрешений: эффективные площади в пределах ±RES_TOLERANCE
# считаем РАВНЫМИ (через лог-бакетизацию), и тогда решает битрейт (size). Без него
# 1280×840-с-полосами (контент ~850×640) обыгрывал честные 850×640 «на волосок»,
# хотя у второго лучше качество — ровно жалоба пользователя. 0.12 ≈ один тир.
RES_TOLERANCE = 0.12


def _area_bucket(item_data):
    """Лог-бакет эффективной площади: соседние бакеты отличаются на ~RES_TOLERANCE,
    значит близкие разрешения попадают в один бакет и сравниваются как равные."""
    import math
    a = _effective_area(item_data)
    if a <= 1:
        return 0
    return int(round(math.log(a) / math.log(1.0 + RES_TOLERANCE)))


def _cascade_sort_key(item_data, keep_newer=KEEP_NEWER):
    """Кортеж-ключ Pareto-каскада для max(). Больше == «оставить вероятнее».

    1) Resolution Priority — БАКЕТ эффективной площади (без чёрных полос/вотермарки),
       с допуском ±RES_TOLERANCE: близкие разрешения равны.
    2) Bitrate/Size Priority — размер как маркер меньшей компрессии (решает при
       равном разрешении: «меньше пикселей, но лучше качество» теперь побеждает).
    3) Chronological Priority — mtime; знак задаёт keep_newer.

    keep_newer пробрасывается из пользовательских QSettings (AutoSelectWorker);
    дефолт = константа KEEP_NEWER для прямых/тестовых вызовов."""
    mtime = item_data.get('mtime', 0.0) or 0.0
    chrono = mtime if keep_newer else -mtime
    return (_area_bucket(item_data), item_data.get('size', 0), chrono)


def _cascade_keeper(keep_newer):
    """Функция выбора оригинала по Парето-каскаду с заданной хронологией.

    Фабрика нужна, чтобы пробросить динамический keep_newer внутрь max(key=...)
    без мутации модульного состояния. Статический STRATEGY_KEEPERS['strat_cascade']
    остаётся на дефолте KEEP_NEWER (прямые/тестовые вызовы)."""
    return lambda cluster: max(cluster, key=lambda it: _cascade_sort_key(it, keep_newer))


# Внутренние ключи стратегий разметки. Порядок СТРОГО соответствует
# порядку элементов QComboBox combo_strategy в main_window (strat_smart,
# strat_quality, strat_size, strat_date). Для каждой стратегии задаётся
# функция выбора файла, который нужно ОСТАВИТЬ; остальные помечаются на удаление.
STRATEGY_KEEPERS = {
    "strat_smart":   lambda cluster: max(cluster, key=calculate_smart_score),
    "strat_quality": lambda cluster: max(cluster, key=calculate_quality_score),
    "strat_size":    lambda cluster: max(cluster, key=lambda x: x.get('size', 0)),
    "strat_date":    lambda cluster: min(cluster, key=lambda x: x.get('mtime', float('inf'))),
    # Строгий многокритериальный каскад из директивы (Критерии 1→2→3).
    "strat_cascade": lambda cluster: max(cluster, key=_cascade_sort_key),
}

# Маппинг индекса комбобокса -> внутренний ключ стратегии. Индекс 0 («Умный»)
# отдан строгому Парето-каскаду (Разрешение → Размер → Хронология + списки
# директорий): именно он выбирает эталон для кнопки «Авто» по умолчанию.
# Взвешенная эвристика strat_smart остаётся доступной программно (и покрыта
# tests/test_auto_selector.py), но из UI больше не вызывается.
STRATEGY_BY_INDEX = ["strat_cascade", "strat_quality", "strat_size", "strat_date"]


def select_keeper(cluster, base_keeper):
    """Файл, который НЕОБХОДИМО ОСТАВИТЬ, с наложением Критерия 4 поверх стратегии.

    base_keeper — функция выбора оригинала внутри пула равноправных кандидатов
    (любая из STRATEGY_KEEPERS). Директивные списки накладываются сверху:
      • есть защищённые файлы → оставляем лучший СРЕДИ защищённых;
      • иначе предпочитаем не-blacklist кандидатов, чтобы оригинал не
        назначался из /Downloads/ и подобных мусорных папок.
    """
    protected = [it for it in cluster if _path_in(it, PROTECTED_DIRS)]
    if protected:
        return base_keeper(protected)

    preferred = [it for it in cluster if not _path_in(it, BLACKLIST_DIRS)]
    pool = preferred if preferred else cluster
    return base_keeper(pool)


class AutoSelectWorker(QThread):
    finished = Signal(list)

    def __init__(self, clusters_data, strategy_idx, parent=None):
        super().__init__(parent)
        self.clusters_data = clusters_data
        self.strategy_idx = strategy_idx
        # Критерий 3 (хронология) читаем ЗДЕСЬ — в GUI-потоке, до start(): доступ
        # к QSettings идёт из основного потока, а run() уже работает с готовым
        # снимком значения (без обращения к настройкам из рабочего потока).
        self.keep_newer = _read_keep_newer()

    def run(self):
        # Разрешаем индекс комбобокса во внутренний ключ стратегии. Раньше
        # индексы 1/2 трактовались как «старое/новое», а индекс 3 не
        # обрабатывался вовсе — поэтому авторазметка ломалась после
        # переименования стратегий на (Умный / Качество / Размер / Дата).
        key = STRATEGY_BY_INDEX[self.strategy_idx] if 0 <= self.strategy_idx < len(STRATEGY_BY_INDEX) else "strat_smart"
        # strat_cascade использует ДИНАМИЧЕСКУЮ хронологию из QSettings; прочие
        # стратегии от Критерия 3 не зависят и берутся из статического реестра.
        if key == "strat_cascade":
            keeper = _cascade_keeper(self.keep_newer)
        else:
            keeper = STRATEGY_KEEPERS.get(key, STRATEGY_KEEPERS["strat_smart"])

        to_check = []
        for cluster in self.clusters_data:
            if not cluster or len(cluster) < 2:
                continue

            best_item = select_keeper(cluster, keeper)
            for item in cluster:
                if item['path'] != best_item['path']:
                    to_check.append(item['path'])

        self.finished.emit(to_check)
