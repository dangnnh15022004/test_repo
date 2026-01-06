# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sys
import time
import hashlib
import platform
import subprocess
import socket
import threading
import fcntl
import smtplib
import uuid
import atexit
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
import pyperclip
import psutil
from openai import OpenAI

# Ép buộc môi trường chạy phải dùng UTF-8
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["LANG"] = "en_US.UTF-8"
os.environ["LC_ALL"] = "en_US.UTF-8"

if sys.stdout.encoding is None:
    try: sys.stdout.reconfigure(encoding='utf-8')
    except Exception: pass

# ==============================
#   OS CHECK
# ==============================
if platform.system() != "Darwin":
    print("Script này chỉ dành cho MacOS!")
    sys.exit(1)

# ==============================
#   CONFIG
# ==============================
APP_NAME = "DlpAgent"
RUN_FLAG = True

# Path cho file lock - Dùng thư mục người dùng
LOCK_FILE_PATH = os.path.expanduser("~/.dlp_agent.lock")

# Whitelist
ALLOWED_CODE_APPS_MAC = {
    "Code", "Visual Studio Code", "PyCharm", "IntelliJ IDEA", "CLion",
    "PhpStorm", "WebStorm", "Sublime Text", "Xcode", "Terminal", "iTerm2"
}

# --- BLACKLIST ---
BANNED_APPS_MAC = [
    "Screenshot", "Grab", "Skitch", "Lightshot", "Gyazo",
    "screencapture", "Snippets", "CleanShot X", "Monosnap", "Snip"
]

def load_configuration():
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
        if hasattr(sys, '_MEIPASS'):
            embedded_env = os.path.join(sys._MEIPASS, ".env")
            if os.path.exists(embedded_env):
                load_dotenv(embedded_env)
                return base_dir
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(base_dir, ".env"))
    return base_dir

BASE_DIR = load_configuration()

# Load Config
AZURE_ENDPOINT = os.getenv("AZURE_INFERENCE_ENDPOINT")
AZURE_KEY = os.getenv("AZURE_INFERENCE_KEY")
AZURE_MODEL = os.getenv("AZURE_INFERENCE_MODEL")

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")


# ==============================
#   UTILS: Single instance & process checks
# ==============================
_lock_fd = None

def _close_lock():
    global _lock_fd
    try:
        if _lock_fd:
            # Mở khóa trước khi đóng file
            try: fcntl.lockf(_lock_fd, fcntl.LOCK_UN)
            except Exception: pass
            try: _lock_fd.close()
            except Exception: pass
            _lock_fd = None
    except Exception: pass

atexit.register(_close_lock)

def ensure_single_instance():
    """
    Đảm bảo chỉ 1 instance chạy.
    TỐI ƯU: Nếu phát hiện trùng lặp, thoát êm đẹp (exit 0) để Launchd không cố restart.
    """
    global _lock_fd
    try:
        _lock_fd = open(LOCK_FILE_PATH, 'w')
        # Thử lock exclusive non-blocking
        fcntl.lockf(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Ghi PID vào file
        _lock_fd.truncate(0)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return _lock_fd
    except (IOError, OSError):
        # Đã có process khác giữ lock
        print("DlpAgent is already running. Exiting silently.")
        # QUAN TRỌNG: Exit 0 để hệ điều hành biết đây là việc thoát chủ động, không phải lỗi.
        sys.exit(0)
    except Exception as e:
        print(f"Lock error: {e}")
        sys.exit(1)

def get_active_app_name_mac() -> str | None:
    try:
        cmd = 'tell application "System Events" to get name of first application process whose frontmost is true'
        # Timeout để tránh treo process
        result = subprocess.check_output(['osascript', '-e', cmd], stderr=subprocess.DEVNULL, timeout=2)
        return result.decode('utf-8').strip()
    except: return None

# --- CLIPBOARD MAC IMPLEMENTATION ---
def get_clipboard_mac():
    try:
        t = pyperclip.paste()
        if t and t.strip():
            return "text", t.strip()
    except: pass
    return "empty", None

def set_clipboard_hard_block_mac(warning_text):
    try: pyperclip.copy(warning_text)
    except: pass

def set_clipboard_restore_mac(t, d):
    try:
        if t=="text": pyperclip.copy(d)
    except: pass

# --- UPDATED KILL FUNCTION ---
def kill_banned_windows():
    # Duyệt process an toàn hơn
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                p_name = proc.info['name'].lower() if proc.info['name'] else ""
                p_cmd = " ".join(proc.info['cmdline']).lower() if proc.info['cmdline'] else ""
                for banned in BANNED_APPS_MAC:
                    banned_lower = banned.lower()
                    if (banned_lower in p_name) or (banned_lower in p_cmd):
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception: pass

def start_smart_killer():
    def loop_kill():
        while RUN_FLAG:
            kill_banned_windows()
            # Tăng thời gian nghỉ lên 1s để giảm tải CPU, tránh tạo áp lực hệ thống
            time.sleep(1.0)
    t = threading.Thread(target=loop_kill); t.daemon = True; t.start()

# ==============================
#   STARTUP FUNCTIONS (CLEAN & OPTIMIZED)
# ==============================
def add_to_startup():
    try:
        PLIST_LABEL = f"com.{APP_NAME.lower()}.agent"
        launch_agents_dir = os.path.expanduser("~/Library/LaunchAgents")
        plist_name = f"{PLIST_LABEL}.plist"
        plist_path = os.path.join(launch_agents_dir, plist_name)

        os.makedirs(launch_agents_dir, exist_ok=True)

        exe_path = sys.executable
        if not getattr(sys, 'frozen', False): cmd_args = [exe_path, os.path.abspath(__file__)]
        else: cmd_args = [exe_path]

        program_arg_strings = "\n".join([f"        <string>{arg}</string>" for arg in cmd_args])
        stdout_log = "/tmp/dlp_agent.out"
        stderr_log = "/tmp/dlp_agent.err"

        # CẤU HÌNH: RunAtLoad=True (Chạy khi mở máy) + KeepAlive=True (Chết là sống lại)
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
{program_arg_strings}
    </array>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>2</integer>

    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
</dict>
</plist>
"""
        with open(plist_path, "w") as f: f.write(plist_content)
        print(f"✅ LaunchAgent created (Persistent Mode Active).")

    except Exception as e:
        print(f"❌ Startup Error: {e}")

def remove_from_startup():
    try:
        PLIST_LABEL = f"com.{APP_NAME.lower()}.agent"
        plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{PLIST_LABEL}.plist")
        if os.path.exists(plist_path):
            uid = os.getuid()
            # Thêm '-' trước bootout để tránh lỗi nếu service chưa chạy
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", plist_path], capture_output=True, check=False)
            os.remove(plist_path)
            print("Removed from Startup.")
    except Exception as e: print(f"Remove Error: {e}")

# ==============================
#   NOTIFICATIONS & EMAILS
# ==============================
def show_native_alert(title, message):
    try:
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        cmd = f'''display notification "{safe_msg}" with title "{safe_title}" sound name "Sosumi"'''
        subprocess.run(["osascript", "-e", cmd])
    except: pass

def trigger_popup_async(title, message):
    threading.Thread(target=show_native_alert, args=(title, message)).start()
    
def show_custom_alert(header, body):
    trigger_popup_async(header, body)

def get_system_detail():
    try:
        hostname = socket.gethostname()
        if not hostname: hostname = platform.node()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80)); ip_address = s.getsockname()[0]; s.close()
        except: ip_address = "127.0.0.1"

        user = os.getenv('SUDO_USER') or os.getenv('USER') or os.getlogin()
        local_time = time.strftime("%d/%m/%Y %I:%M:%S %p", time.localtime())
        return {
            "user": user,
            "email_mock": f"{user}@{hostname}",
            "device": hostname,
            "ip": ip_address,
            "time_local": local_time
        }
    except: return {"user": "Unknown", "email_mock": "Unknown", "device": "Unknown", "ip": "Unknown", "time_local": "Unknown"}

def send_email_alert(content_preview, violated_app="Unknown App"):
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECEIVER: return
    sys_info = get_system_detail()
    if isinstance(content_preview, str):
        preview = content_preview[:800] + "..." if len(content_preview) > 800 else content_preview
        preview = preview.replace("<", "&lt;").replace(">", "&gt;")
    else: preview = "[Image Content]"

    alert_id = str(uuid.uuid4())
    subject = f"Medium-severity alert: DLP policy matched for clipboard content in a device"
    html_body = f"""
    <html><body style="font-family: 'Segoe UI', sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
        <div style="background-color: #fff; padding: 40px; border-radius: 8px; border-top: 6px solid #d83b01; max-width: 750px; margin: auto; box-shadow: 0 2px 10px rgba(0,0,0,0.05);">
            <h2 style="color: #212529; margin-top: 0;">A medium-severity alert has been triggered</h2>
            <p style="font-size: 15px; color: #666;">DLP policy matched for clipboard content on a managed device (MacOS).</p>
            <div style="background-color: #faf9f8; padding: 15px; border-left: 4px solid #a4262c; margin: 20px 0;">
                <strong style="color: #a4262c;">Severity: Medium</strong>
            </div>
            <table style="width: 100%; font-size: 14px; line-height: 1.8; border-collapse: collapse;">
                <tr><td style="width: 220px; font-weight: bold; color: #444;">Time of occurrence:</td><td>{sys_info['time_local']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Activity:</td><td>DlpRuleMatch (Clipboard Copy)</td></tr>
                <tr><td style="font-weight: bold; color: #444;">User:</td><td style="color: #0078d4;">{sys_info['email_mock']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Policy:</td><td>DLP_Block_SourceCode_Mac</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Alert ID:</td><td style="color: #666; font-family: monospace;">{alert_id}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Application:</td><td style="color: #d83b01; font-weight: bold;">{violated_app}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Device:</td><td>{sys_info['device']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">IP:</td><td>{sys_info['ip']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Status:</td><td style="color: #a4262c; font-weight: bold;">BLOCK</td></tr>
            </table>
            <hr style="border: 0; border-top: 1px solid #e1dfdd; margin: 25px 0;">
            <h3 style="font-size: 16px;">Violating Content Preview:</h3>
            <div style="background-color: #f3f2f1; padding: 15px; border: 1px solid #e1dfdd; font-family: Consolas, monospace; font-size: 13px; color: #d13438; white-space: pre-wrap;">{preview}</div>
        </div>
    </body></html>
    """

    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER; msg['To'] = EMAIL_RECEIVER; msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html'))

    try:
        server = smtplib.SMTP('smtp.office365.com', 587)
        server.ehlo(); server.starttls(); server.ehlo()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg); server.quit()
    except Exception as e: print(f"Email Error: {e}")
    
def trigger_email_async(content, app_name="Unknown"):
    threading.Thread(target=send_email_alert, args=(content, app_name)).start()

# ==============================
#   CLIPBOARD & AI
# ==============================
def hash_data(d):
    if isinstance(d, str): return hashlib.sha256(d.encode('utf-8','ignore')).hexdigest()
    return ""

def call_llm(client, model, content):
    sys_p = "Analyze input. If contains programming code (Python, Java, HTML, etc) output '[CONCLUSION: CODE]'. Else '[CONCLUSION: TEXT]'."
    if isinstance(content, str):
        msgs = [{"role":"system","content":sys_p}, {"role":"user","content":content[:3000]}]
        try:
            res = client.chat.completions.create(model=model, messages=msgs, max_tokens=20, temperature=0)
            out = res.choices[0].message.content or ""
            return "CODE" if "CODE" in out else "TEXT"
        except: return "CODE"
    return "TEXT"

# ==============================
#   MAIN LOOP (BLOCK-FIRST + EMPTY CLIPBOARD SUPPORT)
# ==============================
def main_loop(url, key, model):
    client = OpenAI(base_url=url, api_key=key)
    cache = {}
    
    # Source tracking
    source_content = None
    source_type = None
    source_hash = None
    source_app_name = None
    
    # State tracking - QUAN TRỌNG để restore khi clipboard empty
    is_blocked = False  # Clipboard đang bị block (empty)?

    print("🚀 DLP Agent Mac Started...")
    start_smart_killer()

    while RUN_FLAG:
        try:
            current_app_name = get_active_app_name_mac()
            curr_type, curr_data = get_clipboard_mac()
            
            # --- 1. CLIPBOARD EMPTY + ĐANG BLOCK → Kiểm tra restore ---
            if curr_type == "empty" and is_blocked and source_content:
                # Ở app gốc hoặc allowed app → Restore
                if current_app_name == source_app_name or current_app_name in ALLOWED_CODE_APPS_MAC:
                    set_clipboard_restore_mac(source_type, source_content)
                    is_blocked = False
                time.sleep(0.1)
                continue
            
            # Empty bình thường (chưa block) → Skip
            if curr_type == "empty":
                time.sleep(0.1)
                continue

            curr_hash = hash_data(curr_data)

            # --- 2. PHÁT HIỆN SAO CHÉP MỚI ---
            if curr_hash != source_hash:
                source_content = curr_data
                source_type = curr_type
                source_hash = curr_hash
                source_app_name = current_app_name
                
                if current_app_name in ALLOWED_CODE_APPS_MAC:
                    is_blocked = False
                else:
                    # Block = set clipboard empty
                    set_clipboard_hard_block_mac("")
                    is_blocked = True
                
                time.sleep(0.1)
                continue

            # --- 3. KIỂM TRA NGỮ CẢNH ---
            
            # A. Cùng ứng dụng + đang block → Restore
            if current_app_name == source_app_name:
                if is_blocked and source_content:
                    set_clipboard_restore_mac(source_type, source_content)
                    is_blocked = False
                
            # B. Chuyển ứng dụng
            elif current_app_name != source_app_name:
                
                # B1. Ứng dụng đích cho phép → Restore
                if current_app_name in ALLOWED_CODE_APPS_MAC:
                    if source_content:
                        set_clipboard_restore_mac(source_type, source_content)
                        is_blocked = False
                
                # B2. Ứng dụng đích bị cấm → Gọi LLM
                else:
                    # Đảm bảo vẫn block
                    if not is_blocked:
                        set_clipboard_hard_block_mac("")
                        is_blocked = True

                    check_result = cache.get(source_hash)
                    if check_result is None:
                        check_result = call_llm(client, model, source_content)
                        cache[source_hash] = check_result

                    if check_result == "TEXT":
                        set_clipboard_restore_mac(source_type, source_content)
                        is_blocked = False
                    elif not is_blocked or curr_hash == source_hash:
                        # Chỉ alert 1 lần
                        set_clipboard_hard_block_mac("")
                        is_blocked = True
                        
                        show_custom_alert("Policy Violation", "Copying Source Code to external apps is restricted.")
                        trigger_email_async(source_content, app_name=current_app_name)
                        
                        # Update source_hash để không alert lại
                        source_hash = "BLOCKED_" + source_hash

            time.sleep(0.1)
        except Exception as e:
            print(f"Main loop error: {e}", file=sys.stderr)
            time.sleep(1)

if __name__ == "__main__":
    
    # 1. Cài đặt LaunchAgent (Chỉ tạo file, không ép chạy)
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        add_to_startup()
        sys.exit(0)

    # 2. Gỡ bỏ
    if len(sys.argv) > 1 and sys.argv[1] == "--remove":
        remove_from_startup()
        try: subprocess.run(f"pkill -f {APP_NAME}", shell=True)
        except: pass
        sys.exit(0)

    # 3. Chạy chính: Luôn kiểm tra khóa đơn (Single Instance Lock)
    lock_file = ensure_single_instance()
    
    if AZURE_ENDPOINT and AZURE_KEY and AZURE_MODEL:
        main_loop(AZURE_ENDPOINT, AZURE_KEY, AZURE_MODEL)
    else:
        print("Missing AZURE config.", file=sys.stderr)
        sys.exit(1)