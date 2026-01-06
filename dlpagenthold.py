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
import urllib.parse  # Để xử lý đường dẫn có dấu cách (%20)
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
import pyperclip
import psutil
from openai import OpenAI

# Thư viện lắng nghe bàn phím
try:
    from pynput import keyboard
except ImportError:
    print("⚠️ Thiếu thư viện pynput. Hãy chạy: pip install pynput")
    print("⚠️ Falling back to clipboard-based detection...")
    keyboard = None

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
LOCK_FILE_PATH = os.path.expanduser("~/.dlp_agent.lock")

# Biến toàn cục để đồng bộ trạng thái giữa Main Loop và Keyboard Listener
GLOBAL_STATE = {
    "is_blocked": False,           # Clipboard có đang bị khóa không?
    "violation_content": None,     # Nội dung vi phạm để gửi mail
    "current_app": "Unknown",      # App hiện tại
    "source_app": "Unknown",        # Lưu tên App nơi copy
    "last_alert_time": 0           # Để tránh spam thông báo khi giữ phím V
}

# --- BLACKLIST (Ngăn chặn chụp màn hình) ---
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

# Load Config từ .env
AZURE_ENDPOINT = os.getenv("AZURE_INFERENCE_ENDPOINT")
AZURE_KEY = os.getenv("AZURE_INFERENCE_KEY")
AZURE_MODEL = os.getenv("AZURE_INFERENCE_MODEL")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")

# ==============================
#   SYSTEM UTILS
# ==============================
_lock_fd = None

def _close_lock():
    global _lock_fd
    try:
        if _lock_fd:
            try: fcntl.lockf(_lock_fd, fcntl.LOCK_UN)
            except: pass
            _lock_fd.close()
            _lock_fd = None
    except: pass

atexit.register(_close_lock)

def ensure_single_instance():
    global _lock_fd
    try:
        _lock_fd = open(LOCK_FILE_PATH, 'w')
        fcntl.lockf(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.truncate(0)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return _lock_fd
    except (IOError, OSError):
        print("DlpAgent is already running.")
        sys.exit(0)

def get_active_app_name_mac() -> str | None:
    try:
        cmd = 'tell application "System Events" to get name of first application process whose frontmost is true'
        result = subprocess.check_output(['osascript', '-e', cmd], stderr=subprocess.DEVNULL, timeout=2)
        return result.decode('utf-8').strip()
    except: return None

def get_clipboard_mac():
    try:
        # Kiểm tra file trước (vì file có thể chứa text)
        file_path = get_clipboard_file_path_mac()
        if file_path and os.path.exists(file_path):
            # Đảm bảo là absolute path
            abs_path = os.path.abspath(file_path)
            return "file", abs_path
        
        # Nếu không phải file, kiểm tra text
        t = pyperclip.paste()
        if t and t.strip():
            text_stripped = t.strip()
            
            # Kiểm tra xem text có phải là đường dẫn file không (chỉ absolute path)
            if os.path.isabs(text_stripped) and os.path.exists(text_stripped) and os.path.isfile(text_stripped):
                return "file", text_stripped
            
            return "text", text_stripped
    except: pass
    return "empty", None

def set_clipboard_hard_block_mac(warning_text):
    try: pyperclip.copy(warning_text)
    except: pass

def set_clipboard_restore_mac(t, d):
    try:
        if t=="text": pyperclip.copy(d)
    except: pass

# --- A. Lấy đường dẫn file từ Clipboard ---
def get_clipboard_file_path_mac():
    try:
        # Hỏi macOS: "Trong clipboard có phải là file không? Trả về URL file đây"
        cmd = 'osascript -e \'try\' -e \'get the clipboard as «class furl»\' -e \'end try\''
        result = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode('utf-8').strip()
        
        if not result or result == "empty":
            return None
        
        # Kết quả có thể là:
        # 1. file:///Users/name/file.txt (URL format)
        # 2. Macintosh HD:Users:name:file.txt (colon-separated format)
        
        if result.startswith("file://"):
            # Giải mã URL (bỏ file:// và đổi %20 thành dấu cách)
            path = urllib.parse.unquote(result.replace("file://", ""))
            return path
        elif ":" in result and not result.startswith("http"):
            # Colon-separated format: Macintosh HD:Users:name:file.txt
            # Có thể có chữ "file" ở đầu: "file Macintosh HD:Users:..."
            clean_result = result
            if result.startswith("file "):
                clean_result = result.replace("file ", "", 1)
            
            try:
                # Dùng AppleScript để convert sang POSIX path
                convert_cmd = f'''osascript -e 'POSIX path of (POSIX file "{clean_result}" as alias)' '''
                posix_path = subprocess.check_output(convert_cmd, shell=True, stderr=subprocess.DEVNULL, timeout=1).decode('utf-8').strip()
                if posix_path and os.path.exists(posix_path):
                    return posix_path
            except subprocess.TimeoutExpired:
                pass
            except:
                # Thử parse trực tiếp
                if clean_result.startswith("Macintosh HD:"):
                    posix_path = "/" + clean_result.replace("Macintosh HD:", "").replace(":", "/")
                    if os.path.exists(posix_path):
                        return posix_path
    except:
        pass
    return None

# --- B. Đọc nội dung file an toàn (Tránh file ảnh/exe/zip làm lỗi AI) ---
def read_file_content_safely(file_path):
    try:
        # 1. Kiểm tra kích thước: Bỏ qua file > 1MB (để tránh treo máy)
        if os.path.getsize(file_path) > 1024 * 1024: 
            return None 

        # 2. Mở file đọc thử
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            # Chỉ đọc 4000 ký tự đầu tiên (đủ để AI biết là code hay text)
            content = f.read(4000)
            
            # 3. Check Binary: Nếu có ký tự Null (\0) -> File nhị phân (Ảnh, PDF, Exe...)
            if '\0' in content: 
                return None 
            
            return content
    except:
        return None # Không đọc được (Permission denied hoặc file đang mở)

# --- C. Khôi phục FILE vào Clipboard (Quan trọng) ---
def set_clipboard_restore_file_mac(file_path):
    """
    Dùng AppleScript để ép macOS đưa FILE OBJECT trở lại clipboard.
    (Pyperclip không làm được việc này)
    """
    try:
        if not os.path.exists(file_path):
            return False
        
        # Xử lý đường dẫn cho AppleScript (escape dấu ngoặc kép và backslash)
        safe_path = file_path.replace('\\', '\\\\').replace('"', '\\"')
        # Lệnh: set the clipboard to POSIX file "/path/to/file"
        cmd = f'''osascript -e 'set the clipboard to POSIX file "{safe_path}"' '''
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1)
        return result.returncode == 0
    except:
        return False

def kill_banned_windows():
    try:
        # Duyệt qua các process đang chạy
        for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
            try:
                # 1. Lấy thông tin cơ bản
                # Chuyển về chữ thường để so sánh không phân biệt hoa thường
                p_name = proc.info['name'].lower() if proc.info['name'] else ""
                
                # 'exe' là đường dẫn file chạy gốc (Vd: /Applications/Skitch.app/.../Skitch)
                # Đây là định danh chính xác nhất.
                p_exe = proc.info['exe'].lower() if proc.info['exe'] else ""

                should_kill = False

                for banned in BANNED_APPS_MAC:
                    b_key = banned.lower()

                    # LOGIC MỚI: CHỈ SO SÁNH TÊN VÀ ĐƯỜNG DẪN GỐC
                    
                    # 1. Kiểm tra Tên hiển thị (Process Name)
                    # Ví dụ: Process tên "Skitch" hoặc "Screenshot"
                    if b_key in p_name:
                        should_kill = True
                        break
                    
                    # 2. Kiểm tra Đường dẫn file thực thi (Executable Path)
                    # Ví dụ: /usr/sbin/screencapture
                    if b_key in p_exe:
                        should_kill = True
                        break

                    # 3. Xử lý đặc biệt cho dòng lệnh (Cmdline)
                    # CHỈ kiểm tra phần tử đầu tiên (Tên chương trình), KHÔNG kiểm tra tham số
                    # cmdline thường là list: ['/path/to/app', '--argument', 'file_path']
                    # Ta chỉ check cái đầu tiên.
                    cmd_list = proc.info['cmdline']
                    if cmd_list and len(cmd_list) > 0:
                        first_arg = cmd_list[0].lower() # Chỉ lấy lệnh gọi, bỏ qua tham số phía sau
                        if b_key in first_arg:
                            # Double check: Nếu là lệnh 'node' hay 'electron' hay 'python' thì BỎ QUA
                            # Vì các app này tên chung chung, không được giết dựa trên tên file chạy
                            if "electron" in first_arg or "node" in first_arg or "python" in first_arg:
                                continue 
                            
                            should_kill = True
                            break

                if should_kill:
                    print(f"🚫 Detected banned app: {p_name} (PID: {proc.pid}) -> Killing...")
                    proc.kill()
            
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
    except Exception as e:
        # Bắt lỗi tổng để Thread không bị chết (Crash)
        print(f"Error in kill loop: {e}")

# Hàm start giữ nguyên logic cũ nhưng chỉnh time sleep hợp lý
def start_smart_killer():
    def loop_kill():
        while RUN_FLAG:
            try:
                kill_banned_windows()
            except: pass
            # Để 0.5s - 1.0s là cân bằng nhất. 
            # Nhanh quá (0.05s) sẽ ngốn CPU làm máy nóng và MacOS sẽ tự kill agent.
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
#   KEYBOARD LISTENER (DETECT PASTE)
# ==============================
def on_activate_paste():
    """Hàm này được gọi khi phát hiện tổ hợp phím Cmd+V"""
    
    # 1. Nếu không bị block thì thôi
    if not GLOBAL_STATE["is_blocked"]:
        return

    # 2. [QUAN TRỌNG] Kiểm tra xem có đang ở Cùng App Nguồn không?
    # Nếu đang ở đúng App nguồn (VD: Copy ở VSCode, Paste ở VSCode) -> BỎ QUA, KHÔNG ALERT
    if GLOBAL_STATE["current_app"] == GLOBAL_STATE["source_app"]:
        # print("Pasting in same app - Ignored Alert")
        return

    # 3. Nếu khác App mà đang bị Block -> Mới Alert
    current_time = time.time()
    if current_time - GLOBAL_STATE["last_alert_time"] > 2.0:
        GLOBAL_STATE["last_alert_time"] = current_time
        
        show_native_alert("Policy Violation", "Copying Source Code to external apps is restricted.")
        
        content = GLOBAL_STATE["violation_content"]
        app = GLOBAL_STATE["current_app"]
        if content:
            trigger_email_async(content, app_name=app)

def start_keyboard_listener():
    """Khởi động keyboard listener để detect Cmd+V"""
    if keyboard is None:
        print("⚠️ Keyboard listener not available (pynput not installed)")
        return None
    
    try:
        # Định nghĩa tổ hợp phím cần bắt (Cmd+V)
        paste_hotkey = keyboard.HotKey(
            keyboard.HotKey.parse('<cmd>+v'),
            on_activate_paste
        )

        def for_canonical(f):
            return lambda k: f(listener.canonical(k))

        # Listener chạy trên thread riêng
        listener = keyboard.Listener(
            on_press=for_canonical(paste_hotkey.press),
            on_release=for_canonical(paste_hotkey.release)
        )
        listener.start()
        print("⌨️ Keyboard Listener Started (Monitoring Cmd+V)...")
        return listener
    except Exception as e:
        print(f"❌ Keyboard Listener Error: {e}")
        return None

# ==============================
#   NOTIFICATIONS & EMAILS
# ==============================
def show_native_alert(title, message):
    try:
        # Xử lý ký tự đặc biệt để tránh lỗi lệnh AppleScript
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        
        # LOGIC CŨ: display notification (Chỉ hiện banner góc phải - Dễ bị ẩn)
        # cmd = f'''display notification "{safe_msg}" with title "{safe_title}" sound name "Sosumi"'''
        
        # LOGIC MỚI: display alert (Hiện bảng Popup - Critical Icon)
        # as critical: Hiện icon cảnh báo đỏ
        # buttons {"OK"}: Nút bấm xác nhận
        # giving up after 10: Tự tắt sau 10 giây nếu không bấm (để tránh treo script)
        cmd = f'''display alert "{safe_title}" message "{safe_msg}" as critical buttons {{"OK"}} default button "OK" giving up after 10'''
        
        # Chạy lệnh trong một thread riêng hoặc subprocess để không chặn luồng chính
        # Dùng subprocess.Popen thay vì run để không bắt Python phải chờ user bấm OK mới chạy tiếp
        subprocess.Popen(["osascript", "-e", cmd])
        
        print(f"🔔 Popup Alert Triggered: {safe_title}")
    except Exception as e:
        print(f"❌ Alert Error: {e}")

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
#   AI CLASSIFICATION
# ==============================
def hash_data(d):
    if isinstance(d, str): 
        return hashlib.sha256(d.encode('utf-8','ignore')).hexdigest()
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
#   MAIN LOOP (SAME APP POLICY)
# ==============================
def main_loop(url, key, model):
    client = OpenAI(base_url=url, api_key=key)
    cache = {}
    
    source_content = None
    source_type = None
    source_hash = None
    source_app_name = None

    print("🚀 DLP Agent Mac Started (Reactive Mode: Alert on Paste)...")
    start_smart_killer()
    
    # Khởi động keyboard listener
    start_keyboard_listener()

    while RUN_FLAG:
        try:
            current_app_name = get_active_app_name_mac()
            
            # Cập nhật App hiện tại vào biến toàn cục để Listener dùng
            if current_app_name:
                GLOBAL_STATE["current_app"] = current_app_name
            
            curr_type, curr_data = get_clipboard_mac()
            
            # --- 1. CLIPBOARD RỖNG + ĐANG BLOCK → Restore nếu quay lại SAME APP ---
            if curr_type == "empty" and GLOBAL_STATE["is_blocked"] and source_content:
                if current_app_name == source_app_name:
                    # Restore file hoặc text
                    if source_type == "file":
                        set_clipboard_restore_file_mac(source_content)
                    else:
                        set_clipboard_restore_mac(source_type, source_content)
                    GLOBAL_STATE["is_blocked"] = False
                time.sleep(0.1); continue
            
            if curr_type == "empty":
                time.sleep(0.1); continue

            curr_hash = hash_data(curr_data)

            # --- 2. PHÁT HIỆN SAO CHÉP MỚI → CHẶN NGAY LẬP TỨC ---
            if curr_hash != source_hash:
                # CHẶN NGAY LẬP TỨC (Zero Trust) - Trước khi lưu thông tin
                set_clipboard_hard_block_mac("")
                GLOBAL_STATE["is_blocked"] = True
                
                # Sau đó mới lưu thông tin
                source_content = curr_data
                source_type = curr_type
                source_hash = curr_hash
                source_app_name = current_app_name
                
                time.sleep(0.1); continue

            # --- 3. KIỂM TRA NGỮ CẢNH ---
            
            # A. Nếu đang ở CÙNG ứng dụng nguồn → Mở khóa nội bộ
            if current_app_name == source_app_name:
                if GLOBAL_STATE["is_blocked"] and source_content:
                    # Restore file hoặc text
                    if source_type == "file":
                        set_clipboard_restore_file_mac(source_content)
                    else:
                        set_clipboard_restore_mac(source_type, source_content)
                    GLOBAL_STATE["is_blocked"] = False
                
            # B. Nếu chuyển sang ứng dụng KHÁC
            else:
                # Đảm bảo clipboard luôn bị khóa khi ở app khác
                if not GLOBAL_STATE["is_blocked"]:
                    set_clipboard_hard_block_mac("")
                    GLOBAL_STATE["is_blocked"] = True

                # Kiểm tra nội dung qua Cache hoặc AI
                # Kiểm tra xem hash đã bị block chưa (để tránh alert lặp lại)
                original_hash = source_hash
                if isinstance(source_hash, str) and source_hash.startswith("BLOCKED_"):
                    original_hash = source_hash.replace("BLOCKED_", "")
                
                check_result = cache.get(original_hash)
                if check_result is None:
                    # Nếu là file, đọc nội dung file trước
                    content_to_check = source_content
                    if source_type == "file":
                        content_to_check = read_file_content_safely(source_content)
                        if not content_to_check:
                            # File không đọc được (binary/lớn) -> coi như CODE
                            check_result = "CODE"
                            cache[original_hash] = check_result
                    
                    if check_result is None:
                        check_result = call_llm(client, model, content_to_check)
                        cache[original_hash] = check_result

                if check_result == "TEXT":
                    # Nếu là TEXT bình thường -> Cho phép dán tự do
                    if GLOBAL_STATE["is_blocked"]:
                        # Restore file hoặc text (file_path đã là absolute path từ đầu)
                        if source_type == "file":
                            set_clipboard_restore_file_mac(source_content)
                        else:
                            set_clipboard_restore_mac(source_type, source_content)
                        GLOBAL_STATE["is_blocked"] = False
                        GLOBAL_STATE["violation_content"] = None  # Reset
                else:
                    # Nếu là CODE -> Giữ trạng thái khóa (KHÔNG alert ngay, chỉ alert khi paste)
                    if not isinstance(source_hash, str) or not source_hash.startswith("BLOCKED_"):
                        set_clipboard_hard_block_mac("")
                        GLOBAL_STATE["is_blocked"] = True
                        
                        # Cập nhật thông tin vi phạm cho Global State (để Listener dùng)
                        alert_content = source_content
                        if source_type == "file":
                            alert_content = read_file_content_safely(source_content) or f"[File: {os.path.basename(source_content)}]"
                        GLOBAL_STATE["violation_content"] = alert_content
                        
                        # Đánh dấu hash để không lặp lại
                        source_hash = "BLOCKED_" + str(original_hash)

            time.sleep(0.1)
        except Exception as e:
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
