"""
Script to update ALL icons (favicon + app icons) from a source image
"""
from PIL import Image
import sys
import os

def create_icons(input_path):
    if not os.path.exists(input_path):
        print(f"❌ Error: File {input_path} not found!")
        return

    img = Image.open(input_path)
    static_dir = 'static'
    
    # 1. Create Favicon (32x32)
    img.resize((32, 32), Image.Resampling.LANCZOS).save(f'{static_dir}/favicon.ico', format='ICO')
    print("✅ Created static/favicon.ico")

    # 2. Create App Icons (192x192, 512x512)
    img.resize((192, 192), Image.Resampling.LANCZOS).save(f'{static_dir}/icon-192x192.png', format='PNG')
    print("✅ Created static/icon-192x192.png")
    
    img.resize((512, 512), Image.Resampling.LANCZOS).save(f'{static_dir}/icon-512x512.png', format='PNG')
    print("✅ Created static/icon-512x512.png")

if __name__ == '__main__':
    # Ищем исходную картинку (которую ты загружал ранее)
    # Я использую путь к файлу, который ты загружал в шаге 618
    source_image = r"C:/Users/kuily/.gemini/antigravity/brain/c4bcc16d-53f2-4700-927f-89715ff7c379/uploaded_image_1764441209122.jpg"
    
    create_icons(source_image)
