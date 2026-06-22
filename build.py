import os
import subprocess
import sys
import shutil
from pathlib import Path

def build():
    print("Starting TensorMedia build process...")
    
    # 1. Clean previous builds
    for folder in ['build', 'dist']:
        if os.path.exists(folder):
            print(f"Cleaning {folder}...")
            shutil.rmtree(folder)
            
    # 2. Set local cache directory to avoid PermissionError on macOS
    os.environ['PYINSTALLER_CONFIG_DIR'] = os.path.abspath("./.pyinstaller_cache")
    if not os.path.exists("./.pyinstaller_cache"):
        os.makedirs("./.pyinstaller_cache")

    # 3. Run PyInstaller
    print("Running PyInstaller...")
    try:
        subprocess.run(['pyinstaller', '--noconfirm', 'TensorMedia.spec'], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Build failed: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print("Error: pyinstaller not found. Please install it with 'pip install pyinstaller'")
        sys.exit(1)
        
    print("\nBuild successful!")
    
    if sys.platform == "darwin":
        app_path = Path('dist/TensorMedia.app')
        zip_path = Path('dist/TensorMedia_macOS.zip')
        
        print("Creating ZIP archive for GitHub release...")
        if zip_path.exists():
            os.remove(zip_path)
            
        try:
            # Use ditto to preserve macOS metadata and permissions
            subprocess.run([
                'ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                str(app_path), str(zip_path)
            ], check=True)
            print(f"✅ ZIP created successfully at: {zip_path.absolute()}")
        except Exception as e:
            print(f"❌ Failed to create ZIP: {e}")
            
        print(f"\nApp bundle located at: {app_path.absolute()}")
    else:
        print(f"Executable located at: {Path('dist/TensorMedia').absolute()}")

if __name__ == "__main__":
    build()
