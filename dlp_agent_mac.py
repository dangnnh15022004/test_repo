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

try:
    from AppKit import NSWorkspace, NSWorkspaceDidActivateApplicationNotification, NSPasteboard, NSPasteboardTypeString, NSFilenamesPboardType
    from Foundation import NSObject, NSURL
    from PyObjCTools import AppHelper
    from pynput import keyboard
    import pyperclip
except ImportError:
    print("❌ Thiếu thư viện! Chạy: pip install pyobjc-framework-Cocoa openai python-dotenv pynput pyperclip")
    sys.exit(1)

# ==============================
#   CONFIG
# ==============================
APP_NAME = "DlpAgent"
RUN_FLAG = True

# Path cho file lock - Dùng thư mục người dùng
LOCK_FILE_PATH = os.path.expanduser("~/.dlp_agent.lock")

def load_configuration():
    """Load configuration từ .env file - hỗ trợ cả frozen executable"""
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

ALLOWED_APPS = {
    "Code", "Visual Studio Code", "PyCharm", "IntelliJ IDEA", "CLion",
    "Terminal", "iTerm2", "Warp", "Xcode", "Sublime Text", "Cursor", "VSCodium",
    "Finder" # Cho phép Finder để copy file mượt hơn
}

BROWSER_APPS = {
    "Google Chrome", "Safari", "Microsoft Edge", "Brave Browser", "Arc", "Firefox", "Opera", "CocCoc"
}

BANNED_APPS_MAC = [
    "Screenshot", "Grab", "Skitch", "Lightshot", "Gyazo",
    "screencapture", "Snippets", "CleanShot X", "Monosnap", "Snip"
]


ALLOWED_DOMAINS = [
    "chatgpt.com", "openai.com",
    "gemini.google.com",
    "copilot.microsoft.com", "bing.com",
    "claude.ai", "poe.com", "chatpro.ai", "github.com", "stackoverflow.com"
]

STATE = {
    "hidden_data": None,
    "hidden_type": None,
    "current_app": "Unknown",
    "source_app": "Unknown",
    "monitor_active": False,
    "safe_hash": None,
    "content_type": None,
    "llm_checking": False,
    "last_alert_time": 0,
    "last_alert_app": None,
    "last_clipboard_hash": None,  # Track clipboard hash changes
    "browser_allowed": False,
    "code_detected_time": 0,
    "warning_shown": False,
    "warned_hashes": set(),
    "warning_threads": set()
}

# ==============================
#   CORE FUNCTIONS
# ==============================
def get_content_hash(data):
    if not data: return None
    return hashlib.md5(data.encode('utf-8')).hexdigest()

def get_active_browser_url(app_name):
    script = None
    if app_name in ["Google Chrome", "Brave Browser", "Microsoft Edge", "Arc", "Opera", "CocCoc"]:
        script = f'tell application "{app_name}" to get URL of active tab of front window'
    elif app_name == "Safari":
        script = 'tell application "Safari" to get URL of front document'
    
    if not script: return ""
    try:
        # Timeout 0.3s
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=0.3)
        return result.stdout.strip()
    except: return ""

def is_domain_allowed(url):
    if not url: return False
    for domain in ALLOWED_DOMAINS:
        if domain in url: return True
    return False

def clear_clipboard():
    """Xóa clipboard - dùng pyperclip cho text, NSPasteboard cho file"""
    try:
        # Dùng pyperclip cho text (đảm bảo pkg chạy được)
        pyperclip.copy("")
        # Cũng clear NSPasteboard để chắc chắn
        try:
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
        except: pass
    except: pass

def restore_clipboard(data_type, data):
    """Restore clipboard - dùng pyperclip cho text, AppleScript cho file (không cần NSPasteboard)"""
    if not data: return
    try:
        if data_type == "text":
            # Dùng pyperclip cho text (đảm bảo pkg chạy được)
            pyperclip.copy(data)
            print(f"✅ Restored Text to clipboard")
            
        elif data_type == "file":
            # Dùng AppleScript để restore file (không cần NSPasteboard, đảm bảo pkg chạy được)
            file_path = os.path.abspath(data) if not os.path.isabs(data) else data
            
            # Đảm bảo đường dẫn file tồn tại
            if not os.path.exists(file_path):
                print(f"❌ File not found: {file_path}")
                return
            
            # Escape đường dẫn cho AppleScript (quan trọng!)
            escaped_path = file_path.replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'")
            
            # Cách 1: Dùng AppleScript với POSIX file (tốt nhất cho file objects)
            # Syntax này sẽ tạo file object thực sự, không phải text path
            applescript = f'set the clipboard to POSIX file "{escaped_path}"'
            
            try:
                result = subprocess.run(
                    ['osascript', '-e', applescript],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False
                )
                if result.returncode == 0:
                    print(f"✅ Restored File Object to clipboard via AppleScript: {file_path}")
                    return
                else:
                    # Nếu có lỗi, thử cách khác
                    raise Exception(f"AppleScript error: {result.stderr}")
            except Exception as e1:
                # Cách 2: Dùng AppleScript với alias (backup)
                try:
                    applescript2 = f'''
                    set filePath to POSIX file "{escaped_path}"
                    set the clipboard to filePath
                    '''
                    result2 = subprocess.run(
                        ['osascript', '-e', applescript2],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        check=False
                    )
                    if result2.returncode == 0:
                        print(f"✅ Restored File Object to clipboard via AppleScript (method 2): {file_path}")
                        return
                except:
                    pass
                
                # Cách 3: Fallback - dùng pbcopy với file path (chỉ text path, không phải file object)
                try:
                    with subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE, text=True) as proc:
                        proc.communicate(input=file_path, timeout=1)
                    print(f"⚠️ Restored File path as text via pbcopy (not file object): {file_path}")
                except:
                    # Fallback cuối: dùng pyperclip (chỉ text path)
                    pyperclip.copy(file_path)
                    print(f"⚠️ Restored File path as text (fallback): {file_path}")
            
    except Exception as e:
        print(f"❌ Restore Error: {e}")
        import traceback
        traceback.print_exc()

def get_pasteboard_change_count():
    """Track clipboard changes - dùng NSPasteboard changeCount nếu có, không thì dùng hash"""
    try:
        # Thử dùng NSPasteboard changeCount trước
        pb = NSPasteboard.generalPasteboard()
        return pb.changeCount()
    except:
        # Fallback: dùng hash tracking với pyperclip
        try:
            content = pyperclip.paste()
            if content:
                content_hash = get_content_hash(content)
                if not hasattr(get_pasteboard_change_count, '_last_hash'):
                    get_pasteboard_change_count._last_hash = None
                    get_pasteboard_change_count._counter = 0
                
                if content_hash != get_pasteboard_change_count._last_hash:
                    get_pasteboard_change_count._last_hash = content_hash
                    get_pasteboard_change_count._counter += 1
                
                return get_pasteboard_change_count._counter
        except: pass
        return 0

def get_clipboard_hash():
    """Get hash of current clipboard content - ưu tiên NSPasteboard, fallback pyperclip"""
    try:
        # Thử lấy từ NSPasteboard trước (cho file)
        pb = NSPasteboard.generalPasteboard()
        types = pb.types()
        
        content = None
        if "public.file-url" in types or NSFilenamesPboardType in types:
            url_str = pb.stringForType_("public.file-url")
            if url_str:
                content = url_str
        elif NSPasteboardTypeString in types:
            content = pb.stringForType_(NSPasteboardTypeString)
        
        if content:
            return get_content_hash(content)
    except: pass
    
    # Fallback: dùng pyperclip cho text
    try:
        content = pyperclip.paste()
        if content:
            return get_content_hash(content)
    except: pass
    return None

def get_and_clear_clipboard():
    """Lấy dữ liệu và xóa clipboard - ưu tiên NSPasteboard cho file, pyperclip cho text"""
    try:
        # Thử lấy từ NSPasteboard trước (cho file)
        pb = NSPasteboard.generalPasteboard()
        types = pb.types()
        content = None
        data_type = None

        # Ưu tiên kiểm tra File trước
        if "public.file-url" in types or NSFilenamesPboardType in types:
            url_str = pb.stringForType_("public.file-url")
            if url_str:
                ns_url = NSURL.URLWithString_(url_str)
                if ns_url and ns_url.isFileURL():
                    content = ns_url.path()
                    data_type = "file"
        
        # Nếu không phải file thì lấy Text từ NSPasteboard
        if not content and NSPasteboardTypeString in types:
            content = pb.stringForType_(NSPasteboardTypeString)
            data_type = "text"

        if content:
            pb.clearContents()  # Xóa sau khi đã lấy được data
            return data_type, content
    except: pass
    
    # Fallback: dùng pyperclip cho text
    try:
        content = pyperclip.paste()
        if content and content.strip():
            data = content.strip()
            pyperclip.copy("")  # Clear
            return "text", data
    except: pass
    return None, None

def read_file_safe(file_path):
    try:
        if not os.path.exists(file_path): return None
        if os.path.getsize(file_path) > 2 * 1024 * 1024: return None
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            if '\0' in f.read(4096): return None
            f.seek(0)
            return f.read(5000)
    except: return None

# ==============================
#   UTILS: Single instance & process checks
# ==============================
_lock_fd = None

def _close_lock():
    global _lock_fd
    try:
        if _lock_fd:
            try: fcntl.lockf(_lock_fd, fcntl.LOCK_UN)
            except Exception: pass
            try: _lock_fd.close()
            except Exception: pass
            _lock_fd = None
    except Exception: pass

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
        print("DlpAgent is already running. Exiting silently.")
        sys.exit(0)
    except Exception as e:
        print(f"Lock error: {e}")
        sys.exit(1)

def get_active_app_name_mac() -> str | None:
    try:
        cmd = 'tell application "System Events" to get name of first application process whose frontmost is true'
        result = subprocess.check_output(['osascript', '-e', cmd], stderr=subprocess.DEVNULL, timeout=2)
        return result.decode('utf-8').strip()
    except: return None

# ==============================
#   STARTUP FUNCTIONS
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
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", plist_path], capture_output=True, check=False)
            os.remove(plist_path)
            print("Removed from Startup.")
    except Exception as e: print(f"Remove Error: {e}")

# ==============================
#   KILLER & EMAIL
# ==============================
def kill_banned_windows():
    """
    Diệt Process mạnh mẽ hơn:
    1. Check Process Name
    2. Check Executable Path (tránh đổi tên app)
    3. Check Command Line Arguments
    """
    try:
        # Lấy thêm thông tin 'exe' để check đường dẫn file chạy
        for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
            try:
                p_name = proc.info['name'].lower() if proc.info['name'] else ""
                p_exe = proc.info['exe'].lower() if proc.info['exe'] else ""
                
                should_kill = False

                for banned in BANNED_APPS_MAC:
                    b_key = banned.lower()
                    
                    # 1. Check Tên
                    if b_key in p_name:
                        should_kill = True
                        break
                    
                    # 2. Check Đường dẫn file (VD: /Applications/Skitch.app/...)
                    if b_key in p_exe:
                        should_kill = True
                        break

                    # 3. Check Lệnh chạy (trừ các tool dev như python/node/electron)
                    cmd_list = proc.info['cmdline']
                    if cmd_list and len(cmd_list) > 0:
                        first_arg = cmd_list[0].lower()
                        if b_key in first_arg:
                            # Whitelist các tool dev chạy lệnh có tên trùng
                            if "electron" in first_arg or "node" in first_arg or "python" in first_arg:
                                continue 
                            should_kill = True
                            break

                if should_kill:
                    print(f"🚫 Killing banned app: {p_name} (PID: {proc.pid})")
                    proc.kill()
            
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
    except Exception: pass

def start_smart_killer():
    def loop_kill():
        while RUN_FLAG:
            kill_banned_windows()
            time.sleep(1.0)
    t = threading.Thread(target=loop_kill); t.daemon = True; t.start()

def send_email_alert(content_preview, violated_app="Unknown App"):
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECEIVER: return
    try:
        # (Giản lược code email để tập trung vào logic chính)
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER
        msg['Subject'] = f"DLP Alert: Code blocked in {violated_app}"
        body = f"User attempted to paste restricted code into {violated_app}.\n\nContent Preview:\n{str(content_preview)[:500]}..."
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP('smtp.office365.com', 587)
        server.ehlo(); server.starttls(); server.ehlo()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"📧 [EMAIL] Alert sent")
    except: pass

def trigger_email_async(content, app_name="Unknown"):
    threading.Thread(target=send_email_alert, args=(content, app_name)).start()

def show_alert(app_name, source_app="Unknown"):
    """Hiện warning alert - chỉ một loại alert duy nhất - không có cooldown để hiện nhanh nhất"""
    try:
        # Bỏ cooldown để alert xuất hiện nhanh nhất có thể
        # Logic tránh spam được xử lý ở delayed_warning qua hash check
        
        # Chỉ dùng một loại alert: Warning
        safe_msg = f"Warning: Code detected from {source_app} to {app_name}. Activity logged."
        cmd = f'''display alert "DLP Warning" message "{safe_msg}" buttons {{"OK"}} default button "OK" giving up after 5'''
        subprocess.Popen(["osascript", "-e", cmd])
    except: pass

# ==============================
#   AI ENGINE
# ==============================
llm_cache = {}
def call_azure_llm(content):
    if not content or not AZURE_KEY: return "TEXT"
    content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
    if content_hash in llm_cache: return llm_cache[content_hash]

    try:
        client = OpenAI(base_url=AZURE_ENDPOINT, api_key=AZURE_KEY)
        system_prompt = "You are a DLP Agent. Input can be file content or text. If it contains source code (Python, JS, Keys, SQL), return 'CODE'. Otherwise return 'TEXT'."
        response = client.chat.completions.create(
            model=AZURE_MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": content[:3000]}],
            temperature=0, max_tokens=10
        )
        res_text = response.choices[0].message.content or ""
        result = "CODE" if "CODE" in res_text.upper() else "TEXT"
        llm_cache[content_hash] = result
        return result
    except: return "CODE"

# ==============================
#   LOGIC PHÂN TÍCH
# ==============================
def async_analysis_universal(data, d_type):
    STATE["llm_checking"] = True
    try:
        content = read_file_safe(data) if d_type == "file" else data
        
        # Nếu là File An toàn (Binary/Ảnh/File Safe)
        if content is None:
            # print(f"   ✅ File Binary/Safe -> Auto Restore")
            STATE["content_type"] = "TEXT"
            STATE["hidden_data"] = None 
            STATE["safe_hash"] = get_content_hash(data)
            restore_clipboard(d_type, data)
            time.sleep(0.1)
            STATE["last_clipboard_hash"] = get_clipboard_hash()
            return

        # Check AI
        verdict = call_azure_llm(content)
        STATE["content_type"] = verdict
        
        if verdict == "TEXT":
            STATE["hidden_data"] = None
            STATE["safe_hash"] = get_content_hash(data)
            restore_clipboard(d_type, data)
            time.sleep(0.1)
            STATE["last_clipboard_hash"] = get_clipboard_hash()
        else:
            # CODE detected
            data_hash = get_content_hash(data)
            print(f"   🤖 AI: CODE -> Detected (Warning will show after delay)")
            STATE["code_detected_time"] = time.time()
            
            # Chỉ trigger warning nếu:
            # 1. Chưa có thread warning đang chạy cho hash này
            # 2. Chưa hiện warning cho hash này (hoặc đã quá 10 giây)
            current_time = time.time()
            should_warn = False
            
            if data_hash not in STATE["warning_threads"]:
                # Kiểm tra xem đã warn chưa, nếu rồi thì chỉ warn lại sau 10 giây
                if data_hash not in STATE["warned_hashes"]:
                    should_warn = True
                else:
                    # Đã warn rồi, nhưng có thể warn lại sau 10 giây
                    # (không track thời gian cụ thể, chỉ clear sau một khoảng thời gian)
                    # Đơn giản: chỉ warn một lần cho mỗi hash trong session
                    pass
            
            if should_warn:
                STATE["warning_threads"].add(data_hash)
                # Trigger warning sau 2 giây (chạy ngầm, không chặn paste)
                threading.Thread(target=delayed_warning, args=(STATE["current_app"], STATE["source_app"], data_hash), daemon=True).start()
            
    finally:
        STATE["llm_checking"] = False

def delayed_warning(app_name, source_app, data_hash):
    """Hiện warning ngay lập tức (chạy ngầm) - chỉ một lần cho mỗi hash"""
    try:
        time.sleep(0.1)  # Delay ngắn 0.3 giây để đảm bảo AI check hoàn tất
        
        # Remove khỏi warning_threads để có thể warn lại sau này
        STATE["warning_threads"].discard(data_hash)
        
        # Double check: chỉ hiện warning nếu vẫn là CODE và chưa warn hash này
        if STATE["content_type"] == "CODE" and data_hash not in STATE["warned_hashes"]:
            # Đánh dấu đã warn để không warn lại
            STATE["warned_hashes"].add(data_hash)
            
            # Chỉ warn nếu đúng app
            if STATE["current_app"] == app_name:
                show_alert(app_name, source_app)
                # Gửi email (chỉ một lần)
                if STATE["hidden_type"] == "file":
                    alert_content = read_file_safe(STATE["hidden_data"]) or "File Content"
                else:
                    alert_content = STATE["hidden_data"]
                trigger_email_async(alert_content, app_name=app_name)
    except: 
        # Đảm bảo luôn remove khỏi warning_threads dù có lỗi
        STATE["warning_threads"].discard(data_hash)

# ==============================
#   WATCHDOG (BROWSER)
# ==============================
def browser_watchdog_loop(app_name):
    print(f"👀 Bắt đầu giám sát {app_name}...")
    STATE["monitor_active"] = True
    STATE["last_clipboard_hash"] = get_clipboard_hash()
    STATE["browser_allowed"] = False
    consecutive_allowed_count = 0
    
    while STATE["monitor_active"] and STATE["current_app"] == app_name:
        try:
            # --- LUÔN CẬP NHẬT TRẠNG THÁI DOMAIN ---
            current_url = get_active_browser_url(app_name)
            is_allowed = is_domain_allowed(current_url)
            STATE["browser_allowed"] = is_allowed

            # 1. Kiểm tra Clipboard mới
            current_hash = get_clipboard_hash()
            if current_hash != STATE["last_clipboard_hash"]:
                STATE["last_clipboard_hash"] = current_hash
                d_type, data = get_and_clear_clipboard()
                
                if data:
                    current_hash_check = get_content_hash(data)
                    # Nếu là Safe Data -> Trả lại
                    if current_hash_check == STATE["safe_hash"]:
                        restore_clipboard(d_type, data)
                        if STATE["hidden_data"]:
                            STATE["hidden_data"] = None
                        continue
                    
                    # Data mới -> Check
                    STATE["source_app"] = app_name
                    STATE["hidden_data"] = data
                    STATE["hidden_type"] = d_type
                    STATE["content_type"] = None
                    threading.Thread(target=async_analysis_universal, args=(data, d_type)).start()
                    continue
            
            # Nếu không có hidden_data -> sleep
            if not STATE["hidden_data"]:
                time.sleep(0.3)
                continue

            # 2. Xử lý dữ liệu đang bị giữ (CODE)
            if is_allowed:
                # Domain xịn -> Restore liên tục
                restore_clipboard(STATE["hidden_type"], STATE["hidden_data"])
                consecutive_allowed_count += 1
                
                if consecutive_allowed_count > 33:  # ~5 giây
                    STATE["hidden_data"] = None
                    consecutive_allowed_count = 0
                    print(f"   ✅ [MULTI-PASTE] Cleared state after timeout")
            else:
                # Domain lởm -> Xóa clipboard
                clear_clipboard()
                consecutive_allowed_count = 0

            time.sleep(0.15)
        except: pass
    print(f"💤 Dừng giám sát {app_name}")

# ==============================
#   MAIN HANDLER
# ==============================
class TrapdoorHandler(NSObject):
    def handleAppActivation_(self, notification):
        try:
            app = notification.userInfo()['NSWorkspaceApplicationKey']
            app_name = app.localizedName()
            STATE["current_app"] = app_name
            STATE["monitor_active"] = False 
            time.sleep(0.1) 
            handle_switch(app_name)
        except: pass

def handle_switch(app_name):
    if app_name in ALLOWED_APPS:
        if STATE["hidden_data"]:
            restore_clipboard(STATE["hidden_type"], STATE["hidden_data"])
            print(f"✅ [RESTORE] {app_name}")
            STATE["hidden_data"] = None
        return

    if app_name in BROWSER_APPS:
        d_type, data = get_and_clear_clipboard()
        if data:
            if get_content_hash(data) == STATE["safe_hash"]:
                 restore_clipboard(d_type, data)
            else:
                 STATE["hidden_data"] = data
                 STATE["hidden_type"] = d_type
                 STATE["content_type"] = None
                 threading.Thread(target=async_analysis_universal, args=(data, d_type)).start()
        
        threading.Thread(target=browser_watchdog_loop, args=(app_name,), daemon=True).start()
        return

    # App thường
    d_type, data = get_and_clear_clipboard()
    if not data:
        if STATE["hidden_data"]: 
            d_type = STATE["hidden_type"]
            data = STATE["hidden_data"]
        else: return

    # App KHÔNG được phép: xóa clipboard ngay để chặn paste tức thì,
    # dữ liệu thật được giữ trong STATE["hidden_data"]
    clear_clipboard()

    if get_content_hash(data) == STATE["safe_hash"]:
        restore_clipboard(d_type, data)
        return

    STATE["hidden_data"] = data
    STATE["hidden_type"] = d_type
    STATE["content_type"] = None
    print(f"🔒 [BLOCK] {app_name}. Checking...")
    threading.Thread(target=async_analysis_universal, args=(data, d_type)).start()

# ==============================
#   KEYBOARD LISTENER (FIXED ALERT LOGIC)
# ==============================
def on_paste_attempt():
    """Xử lý Alert khi nhấn Cmd+V (chỉ cho app không được phép, không chặn Gemini)"""
    try:
        app_name = STATE["current_app"]
        if app_name in ALLOWED_APPS: return
        
        # [FIX] Browser (Gemini, ChatGPT, etc.): Cho phép paste, KHÔNG hiện alert ở đây
        # Warning sẽ được xử lý bởi delayed_warning thôi
        if app_name in BROWSER_APPS:
            # Luôn return cho browser, không hiện alert ở đây
            return

        # [FIX] Alert warning cho app không được phép (không phải browser)
        if STATE["content_type"] == "CODE":
             source_app = STATE.get("source_app", "Unknown")
             print(f"🚫 [PASTE BLOCK] Triggered in {app_name}")
             show_alert(app_name, source_app)  # Warning alert (chung một loại)
             
             # Gửi email
             if STATE["hidden_type"] == "file":
                 alert_content = read_file_safe(STATE["hidden_data"]) or "File Content"
             else:
                 alert_content = STATE["hidden_data"]
             trigger_email_async(alert_content, app_name=app_name)

    except Exception as e: pass

def start_keyboard_listener():
    def on_hotkey(): on_paste_attempt()
    hotkey = keyboard.HotKey(keyboard.HotKey.parse('<cmd>+v'), on_hotkey)
    listener = keyboard.Listener(on_press=hotkey.press, on_release=hotkey.release)
    listener.daemon = True
    listener.start()

def main():
    print("🚀 DLP Agent (Sync State Fix) Started...")
    start_smart_killer()
    if keyboard:
        start_keyboard_listener()
    
    handler = TrapdoorHandler.new()
    ws = NSWorkspace.sharedWorkspace()
    ws.notificationCenter().addObserver_selector_name_object_(
        handler, "handleAppActivation:", NSWorkspaceDidActivateApplicationNotification, None
    )
    
    try: AppHelper.runConsoleEventLoop()
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    # 1. Cài đặt LaunchAgent
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        add_to_startup()
        sys.exit(0)

    # 2. Gỡ bỏ
    if len(sys.argv) > 1 and sys.argv[1] == "--remove":
        remove_from_startup()
        try: subprocess.run(f"pkill -f {APP_NAME}", shell=True)
        except: pass
        sys.exit(0)

    # 3. Chạy chính
    ensure_single_instance()
    
    if not AZURE_ENDPOINT or not AZURE_KEY or not AZURE_MODEL:
        print("❌ Missing Azure config in .env")
        sys.exit(1)
    
    try:
        main()
    except KeyboardInterrupt:
        pass