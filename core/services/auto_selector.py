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

class AutoSelectWorker(QThread):
    finished = Signal(list)
    
    def __init__(self, clusters_data, strategy_idx, parent=None):
        super().__init__(parent)
        self.clusters_data = clusters_data
        self.strategy_idx = strategy_idx
        
    def run(self):
        to_check = []
        for cluster in self.clusters_data:
            if not cluster or len(cluster) < 2: 
                continue
            
            # Стратегия 0: Оставить лучшее качество (Smart Score)
            if self.strategy_idx == 0: 
                best_item = max(cluster, key=calculate_smart_score)
                for item in cluster:
                    if item['path'] != best_item['path']:
                        to_check.append(item['path'])
                        
            # Стратегия 1: Оставить самое старое
            elif self.strategy_idx == 1: 
                oldest = min(cluster, key=lambda x: x.get('mtime', float('inf')))
                for item in cluster:
                    if item['path'] != oldest['path']:
                        to_check.append(item['path'])
                        
            # Стратегия 2: Оставить самое новое
            elif self.strategy_idx == 2: 
                newest = max(cluster, key=lambda x: x.get('mtime', 0.0))
                for item in cluster:
                    if item['path'] != newest['path']:
                        to_check.append(item['path'])
                        
        self.finished.emit(to_check)