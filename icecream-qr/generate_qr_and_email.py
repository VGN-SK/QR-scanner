# generate_qr_and_email.py
import os
import time
import base64
from io import BytesIO
import sqlite3
from email.message import EmailMessage
import smtplib

import pandas as pd
import qrcode
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

# ENV / config
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
FERNET_KEY = os.getenv("ENCRYPTION_KEY")

STUDENTS_CSV = "students.csv"
OUT_DIR = "qrs"
DB_FILE = "attendance.db"
SENDER_NAME = "Hostel Ice Cream Night Team"

if not EMAIL_ADDRESS or not EMAIL_PASSWORD or not FERNET_KEY:
    raise RuntimeError("Please set EMAIL_ADDRESS, EMAIL_PASSWORD, and ENCRYPTION_KEY in .env or env variables")

fernet = Fernet(FERNET_KEY)
os.makedirs(OUT_DIR, exist_ok=True)

# DB helpers
def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS students (
        roll_no TEXT PRIMARY KEY,
        name TEXT,
        email TEXT,
        token_b64 TEXT,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roll_no TEXT,
        timestamp_iso TEXT,
        source TEXT,
        note TEXT
    )
    """)
    conn.commit()
    conn.close()

def save_student_token_db(roll, name, email, token_b64):
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO students (roll_no, name, email, token_b64, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (roll, name, email, token_b64))
    conn.commit()
    conn.close()

def make_token(roll_no: str) -> bytes:
    nonce = base64.urlsafe_b64encode(os.urandom(6)).decode("utf-8")
    payload = f"{roll_no}|{nonce}"
    token = fernet.encrypt(payload.encode("utf-8"))
    return token

def generate_qr_image_from_token(token: bytes):
    token_b64 = base64.urlsafe_b64encode(token).decode("utf-8")
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(token_b64)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return img, token_b64

def send_email_with_attachment(to_email, subject, body, attachment_bytes, attachment_filename):
    msg = EmailMessage()
    msg["From"] = f"{SENDER_NAME} <{EMAIL_ADDRESS}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(attachment_bytes, maintype="image", subtype="png", filename=attachment_filename)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as smtp:
        smtp.ehlo()
        if SMTP_PORT == 587:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(msg)

def main():
    init_db()
    df = pd.read_csv(STUDENTS_CSV, dtype=str)
    for col in ("name", "roll_no", "email"):
        if col not in df.columns:
            raise RuntimeError(f"students.csv must contain column: {col}")

    sent = 0
    errs = []

    for idx, row in df.iterrows():
        try:
            name = row["name"].strip()
            roll = row["roll_no"].strip()
            email = row["email"].strip()

            token = make_token(roll)
            img, token_b64 = generate_qr_image_from_token(token)

            filename = f"{roll}.png"
            out_path = os.path.join(OUT_DIR, filename)
            img.save(out_path)

            # save token to DB
            save_student_token_db(roll, name, email, token_b64)

            # email
            bio = BytesIO()
            img.save(bio, format="PNG")
            bio.seek(0)
            send_email_with_attachment(email,
                                       "Your QR for Ice Cream Night — verify at the gate!",
                                       f"Hello {name},\n\nAttached is your unique QR code for Ice Cream Night. Save it on your phone and present it at the gate.\n\n-- {SENDER_NAME}",
                                       bio.read(), filename)
            sent += 1
            print(f"[SENT] {roll} -> {email}")
            time.sleep(0.6)  # adjust if SMTP throttles
        except Exception as e:
            errs.append((row.get("roll_no", ""), row.get("email", ""), str(e)))
            print(f"[ERROR] Row {idx}: {e}")

    print(f"Done. Sent: {sent}. Errors: {len(errs)}")
    if errs:
        print("Errors sample:", errs[:5])

if __name__ == "__main__":
    main()

