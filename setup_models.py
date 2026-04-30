import os
from pathlib import Path

def download_offline_models():
    # 1. Фиксируем пути
    base_dir = Path(os.path.abspath(__file__)).parent
    models_dir = base_dir / "models"
    models_dir.mkdir(exist_ok=True)
    
    siglip_dir = models_dir / "siglip-base-patch16-224"
    torch_dir = models_dir / "torch"
    
    print(f"📦 Инициализация загрузки в: {models_dir}")
    
    # 2. Загрузка SigLIP (Напрямую в папку, без симлинков)
    print("\n⏳ [1/2] Загрузка SigLIP (google/siglip-base-patch16-224)...")
    print("Ожидайте, объем около ~800 MB...")
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id="google/siglip-base-patch16-224",
            local_dir=str(siglip_dir),
            local_dir_use_symlinks=False, # Отключаем symlinks для переносимости
            resume_download=True
        )
        print("✅ SigLIP успешно загружен и распакован.")
    except ImportError:
        print("❌ Ошибка: Не установлен пакет huggingface-hub. Выполните: pip install huggingface-hub")
        return
    except Exception as e:
        print(f"❌ Критический сбой скачивания SigLIP: {e}")

    # 3. Загрузка FaceNet (Изоляция через TORCH_HOME)
    print("\n⏳ [2/2] Загрузка FaceNet (VGGFace2)...")
    print("Ожидайте, объем около ~110 MB...")
    try:
        # Перенаправляем кэш PyTorch в нашу папку models/torch
        os.environ["TORCH_HOME"] = str(torch_dir)
        from facenet_pytorch import MTCNN, InceptionResnetV1
        
        # Инициализация автоматически триггерит скачивание весов
        MTCNN(device='cpu')
        InceptionResnetV1(pretrained='vggface2').eval()
        print("✅ FaceNet успешно загружен.")
    except ImportError:
        print("❌ Ошибка: Не установлен пакет facenet-pytorch. Выполните: pip install facenet-pytorch")
        return
    except Exception as e:
        print(f"❌ Критический сбой скачивания FaceNet: {e}")

    print("\n✅ ПРОЦЕДУРА ЗАВЕРШЕНА. Папка 'models' готова к оффлайн-сборке.")

if __name__ == "__main__":
    download_offline_models()