import json
import os
import shutil

def fix_encoding():
    print("🔍 Проверка кодировки user_tokens.json...")
    
    # Делаем бэкап
    if os.path.exists('user_tokens.json'):
        shutil.copy('user_tokens.json', 'user_tokens.json.bak')
        print("📦 Создана резервная копия user_tokens.json.bak")
    
    content = None
    encoding_found = None
    
    # Пробуем разные кодировки
    encodings = ['utf-8', 'cp1251', 'latin1', 'cp1252']
    
    for enc in encodings:
        try:
            print(f"Trying {enc}...")
            with open('user_tokens.json', 'r', encoding=enc) as f:
                content = json.load(f)
            encoding_found = enc
            print(f"✅ Успешно прочитано в кодировке: {enc}")
            break
        except Exception as e:
            print(f"❌ Ошибка {enc}: {e}")
            
    if content:
        # Пересохраняем в UTF-8
        print("💾 Пересохранение в UTF-8...")
        with open('user_tokens.json', 'w', encoding='utf-8') as f:
            json.dump(content, f, ensure_ascii=False, indent=4)
        print("✅ Файл успешно сконвертирован в UTF-8!")
        return True
    else:
        print("❌ Не удалось прочитать файл ни в одной кодировке!")
        return False

if __name__ == "__main__":
    fix_encoding()
