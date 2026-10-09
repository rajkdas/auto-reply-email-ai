import imaplib
import os

from dotenv import load_dotenv

load_dotenv()

user = os.getenv("IMAP_USER")
password = os.getenv("IMAP_PASSWORD")

print(f"Attempting login for: {user}")

try:
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(user, password)
    print("SUCCESS: Login accepted!")
except Exception as e:
    print(f"FAILED: {e}")
