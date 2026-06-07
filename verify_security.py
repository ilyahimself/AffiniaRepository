import requests
import os
import sys

# Попытка загрузить переменные окружения
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def get_base_url():
    # 1. Приоритет: аргумент командной строки
    if len(sys.argv) > 1:
        return sys.argv[1]
    
    # 2. Переменная окружения
    env_url = os.getenv('WEBAPP_URL')
    if env_url:
        return env_url.rstrip('/')
        
    # 3. Дефолт (если ничего не найдено)
    return "https://affinia.herokuapp.com"

def test_unauthorized_access():
    BASE_URL = get_base_url()
    print(f"🔒 Testing security on: {BASE_URL}")
    print("Tip: You can change URL by running: python verify_security.py https://your-app.herokuapp.com")
    
    # 1. Попытка запроса без init_data (должна быть заблокирована)
    print("\n1. Sending request WITHOUT init_data...")
    try:
        response = requests.post(f"{BASE_URL}/get_balance", json={"user_id": "12345"})
        
        print(f"Status Code: {response.status_code}")
        if response.status_code == 401:
            print("✅ SUCCESS: Request blocked as expected (401 Unauthorized)")
            try:
                print(f"Response: {response.json()}")
            except:
                print(f"Response text: {response.text}")
        elif response.status_code == 404:
            print("⚠️ WARNING: Endpoint not found (404). Check the URL.")
        else:
            print(f"❌ FAILURE: Request was NOT blocked (Status: {response.status_code})")
    except Exception as e:
        print(f"Error: {e}")

    # 2. Попытка запроса с фейковым init_data (должна быть заблокирована)
    print("\n2. Sending request with FAKE init_data...")
    try:
        fake_data = {
            "user_id": "12345",
            "init_data": "query_id=AAF&user=%7B%22id%22%3A12345%7D&auth_date=1600000000&hash=fake_hash"
        }
        response = requests.post(f"{BASE_URL}/get_balance", json=fake_data)
        
        print(f"Status Code: {response.status_code}")
        if response.status_code == 401:
            print("✅ SUCCESS: Fake data blocked as expected (401 Unauthorized)")
        else:
            print(f"❌ FAILURE: Fake data was NOT blocked (Status: {response.status_code})")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_unauthorized_access()
