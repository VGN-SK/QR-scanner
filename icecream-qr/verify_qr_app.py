# verify_qr_app.py
import os
import base64
import sqlite3
import time
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from cryptography.fernet import Fernet
from pyzbar.pyzbar import decode
from PIL import Image
import numpy as np

# streamlit-webrtc
from streamlit_webrtc import webrtc_streamer, VideoTransformerBase, RTCConfiguration, WebRtcMode

load_dotenv()

FERNET_KEY = os.getenv("ENCRYPTION_KEY")
if not FERNET_KEY:
    st.error("ENCRYPTION_KEY missing in environment (.env).")
    st.stop()

fernet = Fernet(FERNET_KEY)
DB_FILE = "attendance.db"

st.set_page_config(page_title="Ice Cream Night — Live QR Verify", layout="wide")
st.title("🍦 Ice Cream Night — Live QR Verification (webrtc)")

# Simple RTC server config (use public STUN servers by default)
RTC_CONFIGURATION = RTCConfiguration(
    {"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]}
)

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
    # simple retry loop for sqlite busy
    tries = 0
    while True:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cur = conn.cursor()
            ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            cur.execute("INSERT INTO attendance (roll_no, timestamp_iso, source, note) VALUES (?, ?, ?, ?)",
                        (roll, ts, source, note))
            conn.commit()
            conn.close()
            return ts
        except sqlite3.OperationalError as e:
            tries += 1
            if tries > 5:
                raise
            time.sleep(0.1)

# ensure DB exists
init_db()

# Load students count
conn = sqlite3.connect(DB_FILE, timeout=30)
students_count = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
conn.close()
st.sidebar.markdown(f"Students in DB: **{students_count}**")

mode = st.sidebar.radio("Mode", ["Live Scan", "Admin / Logs"])

if mode == "Live Scan":
    st.header("Live camera — point at a QR to verify")
    st.write("Tip: Allow camera access. The stream will detect QR codes and verify automatically.")

    class QRTransformer(VideoTransformerBase):
        def __init__(self):
            self.last_seen = {}  # small cache to avoid reprocessing same token quickly

        def recv(self, frame):
            # frame: av.VideoFrame
            img = frame.to_ndarray(format="bgr24")  # numpy array HxWx3 BGR

            # convert to PIL Image (RGB) for pyzbar
            pil = Image.fromarray(img[:, :, ::-1])  # BGR -> RGB

            decoded = decode(pil)
            if decoded:
                for d in decoded:
                    try:
                        qr_data = d.data.decode("utf-8")
                    except Exception:
                        continue
                    # throttle same qr for 3 seconds to avoid duplicates from same camera stream
                    now = time.time()
                    if qr_data in self.last_seen and now - self.last_seen[qr_data] < 3:
                        continue
                    self.last_seen[qr_data] = now

                    # attempt to decode token_b64 -> token_bytes -> decrypt
                    try:
                        token_bytes = base64.urlsafe_b64decode(qr_data)
                        payload = fernet.decrypt(token_bytes).decode("utf-8")
                        roll_no, nonce = payload.split("|")
                        roll_no = roll_no.strip()
                    except Exception:
                        # invalid token; we can write a small file-based note or ignore
                        print("Invalid or tampered QR detected.")
                        continue

                    # check in students table
                    student = get_student_by_roll(roll_no)
                    if not student:
                        # student not found: maybe token stored differently; try matching token_b64
                        student = get_student_by_token_b64(qr_data)
                        if not student:
                            print(f"Unknown roll/token: {roll_no}")
                            continue

                    # check duplicate attendance
                    if attendance_exists(roll_no):
                        print(f"{roll_no} already verified; ignoring.")
                        # optionally annotate frame or send event (not shown on UI automatically)
                        continue
                    else:
                        ts = add_attendance(roll_no, source="webrtc_live", note="")
                        print(f"Marked present: {roll_no} at {ts}")
                        # optional: trigger a visual indicator by drawing or saving event (webrtc can't directly update UI)
            # return the original frame unchanged
            return frame

    # start webrtc streamer (will open camera on phone)
    webrtc_ctx = webrtc_streamer(
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


    st.markdown("""
    **How it works**
    - Allow camera.
    - Point camera at QR: if valid & not already used, the system logs attendance.
    - If already used, it is ignored.
    """)
    st.info("Logs appear in Admin → Logs (switch mode).")

elif mode == "Admin / Logs":
    st.header("Admin Dashboard: View & Export Logs")
    conn = sqlite3.connect(DB_FILE, timeout=30)
    df_att = pd.read_sql_query("SELECT * FROM attendance ORDER BY timestamp_iso DESC", conn)
    df_students = pd.read_sql_query("SELECT roll_no, name, email FROM students", conn)
    conn.close()

    st.write(f"Total verified: **{len(df_att)}**")

    # show latest entries
    if not df_att.empty:
        df_show = df_att.merge(df_students, on="roll_no", how="left")
        st.dataframe(df_show)

    with st.expander("Filters & Search"):
        q = st.text_input("Search roll or name (substring)")
        if q:
            qlow = q.lower()
            filt = df_show[df_show.apply(lambda r: qlow in (str(r["roll_no"]).lower() + " " + str(r.get("name","") or "").lower()), axis=1)]
            st.write(f"Matches: {len(filt)}")
            st.dataframe(filt)

    if st.button("Export attendance CSV"):
        csv_bytes = df_att.to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", data=csv_bytes, file_name="attendance_log.csv", mime="text/csv")

    st.markdown("---")
    st.subheader("Student DB")
    st.write(f"Total students: {len(df_students)}")
    st.dataframe(df_students)

