# ============================================================
# MODULE: core/profiler.py
# ============================================================
import torch
import gc
import psutil
from utils.logger import auditor

class HardwareProfiler:
    @staticmethod
    def get_device() -> torch.device:
        if torch.backends.mps.is_available():
            return torch.device("mps")
        elif torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")

    @staticmethod
    def get_ram_usage_mb() -> float:
        process = psutil.Process()
        return process.memory_info().rss / (1024 * 1024)

    @staticmethod
    def enforce_garbage_collection(threshold_mb: float = 1200.0, force: bool = False) -> dict:
        current_ram = HardwareProfiler.get_ram_usage_mb()
        metrics = {"triggered": False, "pre_mb": current_ram, "post_mb": current_ram, "recovered_mb": 0.0}
        
        # Контроль лимита Unified Memory / VRAM
        if current_ram > threshold_mb or force:
            if not force:
                auditor.warning(f"RAM Threshold Exceeded: {current_ram:.1f} MB. Executing GC.")
            else:
                auditor.debug(f"Forced GC requested. Active RAM: {current_ram:.1f} MB")
            
            # Избегаем агрессивной сборки мусора в рантайме, чтобы не ломать предсказание ветвлений CPU
            gc.collect(generation=1 if not force else 2)
            
            device = HardwareProfiler.get_device()
            
            # Физическая очистка кэшей акселераторов
            if device.type == "cuda":
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect() 
            elif device.type == "mps":
                # Выполняется только при переполнении/сбросе. Защищает от Metal API CPU Spikes.
                torch.mps.empty_cache()
                
            post_ram = HardwareProfiler.get_ram_usage_mb()
            metrics.update({
                "triggered": True,
                "post_mb": post_ram,
                "recovered_mb": max(0.0, current_ram - post_ram)
            })
            
            if metrics['recovered_mb'] > 0.5:
                auditor.info(f"Memory Flush Complete. Recovered: {metrics['recovered_mb']:.1f} MB.")
            
        return metrics