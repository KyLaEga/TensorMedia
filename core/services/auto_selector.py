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


def calculate_quality_score(item_data):
    """Оценка визуального качества: разрешение с поправкой на резкость."""
    return _resolution_area(item_data) + item_data.get('sharpness', 0.0)


# Внутренние ключи стратегий разметки. Порядок СТРОГО соответствует
# порядку элементов QComboBox combo_strategy в main_window (strat_smart,
# strat_quality, strat_size, strat_date). Для каждой стратегии задаётся
# функция выбора файла, который нужно ОСТАВИТЬ; остальные помечаются на удаление.
STRATEGY_KEEPERS = {
    "strat_smart":   lambda cluster: max(cluster, key=calculate_smart_score),
    "strat_quality": lambda cluster: max(cluster, key=calculate_quality_score),
    "strat_size":    lambda cluster: max(cluster, key=lambda x: x.get('size', 0)),
    "strat_date":    lambda cluster: min(cluster, key=lambda x: x.get('mtime', float('inf'))),
}

# Маппинг индекса комбобокса -> внутренний ключ стратегии.
STRATEGY_BY_INDEX = ["strat_smart", "strat_quality", "strat_size", "strat_date"]


class AutoSelectWorker(QThread):
    finished = Signal(list)

    def __init__(self, clusters_data, strategy_idx, parent=None):
        super().__init__(parent)
        self.clusters_data = clusters_data
        self.strategy_idx = strategy_idx

    def run(self):
        # Разрешаем индекс комбобокса во внутренний ключ стратегии. Раньше
        # индексы 1/2 трактовались как «старое/новое», а индекс 3 не
        # обрабатывался вовсе — поэтому авторазметка ломалась после
        # переименования стратегий на (Умный / Качество / Размер / Дата).
        key = STRATEGY_BY_INDEX[self.strategy_idx] if 0 <= self.strategy_idx < len(STRATEGY_BY_INDEX) else "strat_smart"
        keeper = STRATEGY_KEEPERS.get(key, STRATEGY_KEEPERS["strat_smart"])

        to_check = []
        for cluster in self.clusters_data:
            if not cluster or len(cluster) < 2:
                continue

            best_item = keeper(cluster)
            for item in cluster:
                if item['path'] != best_item['path']:
                    to_check.append(item['path'])

        self.finished.emit(to_check)
