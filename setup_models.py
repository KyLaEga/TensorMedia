import os
import sys
import shutil
from pathlib import Path

MANIFEST_CHECKS = {
    "model.safetensors": {"min_size": 350 * 1024 * 1024},
    "config.json": {"min_size": 100},
    "20180402-114759-vggface2.pt": {"min_size": 100 * 1024 * 1024}
}

def verify_and_clean(dir_path: Path, target_files: list) -> bool:
    if not dir_path.exists():
        return False
        
    for f in target_files:
        file_obj = dir_path / f
        if not file_obj.exists():
            print(f"[VALIDATION FAILED] Missing file: {f}", file=sys.stderr)
            return False
        
        if f in MANIFEST_CHECKS:
            actual_size = file_obj.stat().st_size
            if actual_size < MANIFEST_CHECKS[f]["min_size"]:
                print(f"[SECURITY FATAL] Tensor {f} corrupted. Size {actual_size} below threshold.", file=sys.stderr)
                return False
                
    return True

def download_offline_models():
    base_dir = Path(os.path.abspath(__file__)).parent
    models_dir = base_dir / "models"
    models_dir.mkdir(exist_ok=True)
    
    siglip_dir = models_dir / "siglip-base-patch16-224"
    torch_dir = models_dir / "torch"
    
    print(f"Инициализация загрузки и валидации в: {models_dir}")

    def fetch_siglip(force=False):
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id="google/siglip-base-patch16-224",
            local_dir=str(siglip_dir),
            local_dir_use_symlinks=False,
            resume_download=not force,
            force_download=force,
            # Грузим ТОЛЬКО safetensors-веса. Репозиторий SigLIP содержит и
            # дублирующий pytorch_model.bin (~775 МБ), и форматы TF/Flax/ONNX —
            # всё это балласт: SiglipVisionModel.from_pretrained читает
            # model.safetensors (см. cluster_engine), а валидация ниже проверяет
            # именно его. Фильтр срезает ~775 МБ из бандла без потери функционала.
            ignore_patterns=["*.bin", "*.h5", "*.msgpack", "*.onnx", "*.pth"],
        )
        
    print("\n[1/2] Валидация SigLIP (google/siglip-base-patch16-224)...")
    try:
        fetch_siglip()
        if not verify_and_clean(siglip_dir, ["model.safetensors", "config.json"]):
            raise ValueError("Corrupted or incomplete .safetensors detected.")
        print("SigLIP успешно загружен и верифицирован.")
    except Exception as e:
        print(f"Сбой проверки SigLIP: {e}. Инициализация жесткого сброса...")
        if siglip_dir.exists():
            shutil.rmtree(siglip_dir)
        try:
            fetch_siglip(force=True)
            if not verify_and_clean(siglip_dir, ["model.safetensors", "config.json"]):
                raise ValueError("Corrupted on second attempt.")
            print("SigLIP успешно восстановлен.")
        except Exception as e2:
            print(f"Фатальный сбой загрузки SigLIP: {e2}")

    print("\n[2/2] Валидация FaceNet (VGGFace2)...")
    os.environ["TORCH_HOME"] = str(torch_dir)
    
    def fetch_facenet():
        from facenet_pytorch import MTCNN, InceptionResnetV1
        MTCNN(keep_all=False, device='cpu')
        InceptionResnetV1(pretrained='vggface2').eval()
        
    try:
        fetch_facenet()
        checkpoints_dir = torch_dir / "checkpoints"
        if not verify_and_clean(checkpoints_dir, ["20180402-114759-vggface2.pt"]):
            raise ValueError("Corrupted FaceNet weights detected.")
        print("FaceNet успешно загружен и верифицирован.")
    except Exception as e:
        print(f"Сбой проверки FaceNet: {e}. Инициализация жесткого сброса...")
        checkpoints_dir = torch_dir / "checkpoints"
        if checkpoints_dir.exists():
            shutil.rmtree(checkpoints_dir)
        try:
            fetch_facenet()
            if not verify_and_clean(checkpoints_dir, ["20180402-114759-vggface2.pt"]):
                raise ValueError("Corrupted on second attempt.")
            print("FaceNet успешно восстановлен.")
        except Exception as e2:
            print(f"Фатальный сбой загрузки FaceNet: {e2}")

    print("\nПРОЦЕДУРА ЗАВЕРШЕНА. Векторный слой готов к компиляции.")

if __name__ == "__main__":
    download_offline_models()