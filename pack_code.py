import os
from pathlib import Path

def serialize_codebase():
    target_dir = "."
    output_file = "architecture_dump.txt"
    
    # Жесткий фильтр разрешенных форматов
    valid_ext = {'.py', '.json', '.md'} 
    # Жесткий фильтр блокируемых директорий
    ignore_dirs = {'venv', '.venv', '__pycache__', '.git', 'assets', 'locales', 'tests', 'docs'}
    # Точечная блокировка конкретных файлов
    ignore_files = {'pack_code.py', 'error_log.txt'}
    
    with open(output_file, 'w', encoding='utf-8') as out:
        out.write("### ARCHITECTURE DUMP ###\n")
        
        # Принудительный захват requirements.txt для анализа зависимостей
        req_path = Path("requirements.txt")
        if req_path.exists():
            out.write(f"\n{'='*60}\nMODULE: {req_path}\n{'='*60}\n")
            out.write(req_path.read_text(encoding='utf-8') + "\n")

        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            
            for file in files:
                if Path(file).suffix in valid_ext and file not in ignore_files:
                    filepath = Path(root) / file
                    out.write(f"\n{'='*60}\n")
                    out.write(f"MODULE: {filepath}\n")
                    out.write(f"{'='*60}\n")
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            out.write(f.read() + "\n")
                    except Exception as e:
                        out.write(f"[ОШИБКА ЧТЕНИЯ: {e}]\n")
                        
    print(f"Сборка завершена. Файл сохранен как: {output_file}")

if __name__ == "__main__":
    serialize_codebase()