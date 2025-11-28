# verify_qr_app.py
import os
import base64
import sqlite3
import time
import threading
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from cryptography.fernet import Fernet
from pyzbar.pyzbar import decode
from PIL import Image
import numpy as np
import cv2
import av

# Streamlit-WebRTC
from streamlit_webrtc import (
    webrtc_streamer,
    VideoTransformerBase,
    RTCConfiguration,
    WebRtcMode,
)

# --------------------------------------------------------------------
# LOAD CONFIG
# --------------------------------------------------------------------
load_dotenv()

FERNET_KEY = os.getenv("ENCRYPTION_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "icecream@iitm")
DB_FILE = "attendance.db"

if not FERNET_KEY:
    st.error("❌ ENCRYPTION_KEY missing in .env file")
    st.stop()

fernet = Fernet(FERNET_KEY)

st.set_page_config(
    page_title="🍦 Ice Cream Night — QR Verification",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.title("🍦 Ice Cream Night — QR Verification System")

# RTC (WebRTC camera setup)
RTC_CONFIGURATION = RTCConfiguration(
    {"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]}
)

# --------------------------------------------------------------------
# DATABASE HELPERS
# --------------------------------------------------------------------
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

def get_student_by_roll(roll):
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT roll_no, name, email FROM students WHERE roll_no = ?", (roll,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"roll_no": row[0], "name": row[1], "email": row[2]}
    return None

def get_student_by_token_b64(token_b64):
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT roll_no, name, email FROM students WHERE token_b64 = ?", (token_b64,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"roll_no": row[0], "name": row[1], "email": row[2]}
    return None

def attendance_exists(roll):
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM attendance WHERE roll_no = ? LIMIT 1", (roll,))
    exists = cur.fetchone() is not None
    conn.close()
    return exists

def add_attendance(roll, source="webrtc_scan", note=""):
    tries = 0
    while True:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cur = conn.cursor()
            ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            cur.execute(
                "INSERT INTO attendance (roll_no, timestamp_iso, source, note) VALUES (?, ?, ?, ?)",
                (roll, ts, source, note),
            )
            conn.commit()
            conn.close()
            return ts
        except sqlite3.OperationalError as e:
            tries += 1
            if tries > 8:
                raise
            time.sleep(0.1)

# --------------------------------------------------------------------
# INITIAL SETUP
# --------------------------------------------------------------------
init_db()
conn = sqlite3.connect(DB_FILE, timeout=30)
students_count = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
conn.close()
st.sidebar.markdown(f"**Students loaded:** {students_count}")

mode = st.sidebar.radio("Mode", ["Live Scan", "Admin / Logs"])

# --------------------------------------------------------------------
# TOAST UTIL (non-blocking)
# --------------------------------------------------------------------
def safe_toast(msg, color="green"):
    icon = "✅" if color == "green" else "⚠️" if color == "orange" else "❌"
    def _toast():
        st.toast(msg, icon=icon)
    threading.Thread(target=_toast, daemon=True).start()

# --------------------------------------------------------------------
# MODE: LIVE SCAN
# --------------------------------------------------------------------
if mode == "Live Scan":
    st.header("🎥 Live Camera Scan")
    st.caption("Allow camera access and point at the student QR code.")

    class QRTransformer(VideoTransformerBase):
        def __init__(self):
            self.last_seen = {}
            self.last_color = (255, 255, 255)

        def recv(self, frame):
            img = frame.to_ndarray(format="bgr24")
            pil = Image.fromarray(img[:, :, ::-1])  # BGR → RGB
            decoded = decode(pil)

            color = (255, 255, 255)  # default white
            text_overlay = None

            if decoded:
                for d in decoded:
                    try:
                        qr_data = d.data.decode("utf-8")
                    except Exception:
                        continue
                    now = time.time()
                    if qr_data in self.last_seen and now - self.last_seen[qr_data] < 3:
                        continue
                    self.last_seen[qr_data] = now

                    try:
                        token_bytes = base64.urlsafe_b64decode(qr_data)
                        payload = fernet.decrypt(token_bytes).decode("utf-8")
                        roll_no, nonce = payload.split("|")
                        roll_no = roll_no.strip()
                    except Exception:
                        safe_toast("❌ Invalid or tampered QR", "red")
                        color = (0, 0, 255)
                        text_overlay = "Invalid QR"
                        continue

                    student = get_student_by_roll(roll_no)
                    if not student:
                        student = get_student_by_token_b64(qr_data)
                        if not student:
                            safe_toast("⚠️ Unknown student", "orange")
                            color = (0, 165, 255)
                            text_overlay = "Unknown QR"
                            continue

                    if attendance_exists(roll_no):
                        safe_toast(f"⚠️ {roll_no} already marked", "orange")
                        color = (0, 165, 255)
                        text_overlay = f"{roll_no} already marked"
                    else:
                        ts = add_attendance(roll_no, source="webrtc_live")
                        safe_toast(f"✅ Verified: {student['name']} ({roll_no})", "green")
                        color = (0, 255, 0)
                        text_overlay = f"{student['name']} ✓"

                    (x, y, w, h) = d.rect
                    cv2.rectangle(img, (x, y), (x + w, y + h), color, 4)
                    if text_overlay:
                        cv2.putText(
                            img,
                            text_overlay,
                            (x, y - 10),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.8,
                            color,
                            2,
                            cv2.LINE_AA,
                        )

            return av.VideoFrame.from_ndarray(img, format="bgr24")

    webrtc_streamer(
        key="qr-live",
        mode=WebRtcMode.SENDRECV,
        rtc_configuration=RTC_CONFIGURATION,
        video_transformer_factory=QRTransformer,
        media_stream_constraints={
        "video": {"facingMode": {"exact": "environment"}},
        "audio": False,
    },
        async_transform=True,
    )

    st.info(
        "✅ Valid → Green overlay + toast\n"
        "⚠️ Duplicate → Orange overlay + toast\n"
        "❌ Invalid → Red overlay + toast"
    )

# --------------------------------------------------------------------
# MODE: ADMIN / LOGS
# --------------------------------------------------------------------
elif mode == "Admin / Logs":
    st.header("🔒 Admin Dashboard")

    admin_pass = st.text_input("Enter admin password", type="password")
    if admin_pass != ADMIN_PASSWORD:
        st.error("Invalid password.")
        st.stop()

    conn = sqlite3.connect(DB_FILE, timeout=30)
    df_att = pd.read_sql_query(
        "SELECT * FROM attendance ORDER BY timestamp_iso DESC", conn
    )
    df_students = pd.read_sql_query(
        "SELECT roll_no, name, email FROM students", conn
    )
    conn.close()

    st.write(f"**Total verified:** {len(df_att)} students")

    if not df_att.empty:
        df_show = df_att.merge(df_students, on="roll_no", how="left")
        st.dataframe(df_show)

        with st.expander("🔍 Search / Filter"):
            q = st.text_input("Search roll or name")
            if q:
                qlow = q.lower()
                filt = df_show[
                    df_show.apply(
                        lambda r: qlow
                        in (
                            str(r["roll_no"]).lower()
                            + " "
                            + str(r.get("name", "") or "").lower()
                        ),
                        axis=1,
                    )
                ]
                st.write(f"Matches: {len(filt)}")
                st.dataframe(filt)

    if st.button("📥 Export attendance CSV"):
        csv_bytes = df_att.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download CSV", data=csv_bytes, file_name="attendance_log.csv", mime="text/csv"
        )

    st.markdown("---")
    st.subheader("👥 Student Database")
    st.write(f"Total students: {len(df_students)}")
    st.dataframe(df_students)

