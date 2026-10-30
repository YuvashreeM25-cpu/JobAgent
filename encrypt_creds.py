import getpass
from cryptography.fernet import Fernet

def generate_encrypted_credentials():
    print("--- Glassdoor / Gemini Credential Setup ---")
    email = input("Enter your Login Email: ").strip()
    password = getpass.getpass("Enter your Password (hidden): ").strip()
    gemini_key = input("Enter your Gemini API Key: ").strip()

    encryption_key = Fernet.generate_key()
    cipher = Fernet(encryption_key)

    enc_email = cipher.encrypt(email.encode()).decode()
    enc_password = cipher.encrypt(password.encode()).decode()

    with open(".env", "w", encoding="utf-8") as f:
        f.write(f"ENCRYPTION_KEY={encryption_key.decode()}\n")
        f.write(f"ENC_EMAIL={enc_email}\n")
        f.write(f"ENC_PASSWORD={enc_password}\n")
        f.write(f"GEMINI_API_KEY={gemini_key}\n")

    print("\n[SUCCESS] Credentials encrypted and saved to .env!")

if __name__ == "__main__":
    generate_encrypted_credentials()