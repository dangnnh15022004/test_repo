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
import stat
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
except ImportError:
    print("❌ Thiếu thư viện! Chạy: pip install pyobjc-framework-Cocoa openai python-dotenv pyperclip")
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

# Git Firewall Configuration
WHITELIST_REPO = ["gitlab.siguna.co", "mycompany.internal", "https://github.com/dangnnh15022004/test_repo"]  # Các repo được phép push
HOOKS_DIR = os.path.join(os.path.expanduser("~"), ".dlp_git_hooks")
HOOK_FILE = os.path.join(HOOKS_DIR, "pre-push")

STATE = {
    "hidden_data": None,
    "hidden_type": None,
    "current_app": "Unknown",
    "source_app": "Unknown",
    "monitor_active": False,
    "safe_hash": None,
    "content_type": None,
    "llm_checking": False,
    "last_clipboard_hash": None,  # Track clipboard hash changes
    "browser_allowed": False,
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
    
    # Nhóm 1: Chromium based & Safari (Chuẩn AppleScript - Rất ổn định)
    if app_name in ["Google Chrome", "Brave Browser", "Microsoft Edge", "Arc", "Opera", "CocCoc"]:
        script = f'tell application "{app_name}" to get URL of active tab of front window'
    elif app_name == "Safari":
        script = 'tell application "Safari" to get URL of front document'
        
    # Nhóm 2: Firefox (Xử lý đặc biệt: UI Scripting + Title Fallback)
    elif app_name == "Firefox":
        # Thử lấy URL thật trước
        script = '''
        tell application "System Events"
            tell process "Firefox"
                try
                    -- Cách 1: Thử lấy từ thanh địa chỉ (Address Bar)
                    set theURL to value of UI element 1 of combo box 1 of toolbar "Navigation" of first window
                    return theURL
                on error
                    -- Cách 2: Nếu lỗi, trả về TIÊU ĐỀ CỬA SỔ (Window Title) để Python xử lý fallback
                    return "TITLE:" & name of first window
                end try
            end tell
        end tell
        '''
    
    if not script: return ""
    
    try:
        # Timeout ngắn để tránh treo
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=0.3)
        output = result.stdout.strip()

        # Xử lý Logic Fallback cho Firefox
        if app_name == "Firefox" and output.startswith("TITLE:"):
            window_title = output.replace("TITLE:", "").lower()
            
            # Map tiêu đề sang domain giả định để whitelist hiểu
            if "gemini" in window_title: return "https://gemini.google.com"
            if "chatgpt" in window_title: return "https://chatgpt.com"
            if "claude" in window_title: return "https://claude.ai"
            if "github" in window_title: return "https://github.com"
            if "bing" in window_title or "copilot" in window_title: return "https://copilot.microsoft.com"
            
            return "" # Không nhận diện được web nào trong whitelist
            
        return output
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

# ==============================
#   GIT FIREWALL (DLP - Prevent Push to External Repos)
# ==============================
def setup_git_firewall():
    """Cài đặt Git Firewall để ngăn push lên repo ngoài"""
    try:
        # 1. Tạo thư mục và file hook
        if not os.path.exists(HOOKS_DIR):
            os.makedirs(HOOKS_DIR)
        
        # 2. XÁC ĐỊNH CÁCH GỌI LỆNH (Fix lỗi Binary không chạy được với python3)
        if getattr(sys, 'frozen', False):
            # Nếu là executable đã đóng gói (.app/binary)
            agent_script = sys.executable
            # Gọi trực tiếp binary, KHÔNG dùng python3
            run_cmd = f'"{agent_script}"'
        else:
            # Nếu là script Python thông thường
            agent_script = os.path.abspath(__file__)
            # Phải gọi bằng python3
            run_cmd = f'python3 "{agent_script}"'
        
        # Escape đường dẫn cho bash (dùng cho việc kiểm tra file tồn tại)
        agent_script_escaped = agent_script.replace('"', '\\"')
        
        # 3. Tạo pre-push script với whitelist và gửi email khi bị chặn
        # Escape và format whitelist cho bash array
        whitelist_str = ' '.join([f'"{repo}"' for repo in WHITELIST_REPO])
        whitelist_display = ', '.join(WHITELIST_REPO)
        
        pre_push_script = f"""#!/bin/bash
# DLP Agent Git Firewall
remote="$1"
url="$2"
if [ -z "$url" ]; then
    url=$(git config --get remote."$remote".url)
fi

# Whitelist repos
ALLOWED_REPOS=({whitelist_str})

for domain in "${{ALLOWED_REPOS[@]}}"; do
    if [[ "$url" == *"$domain"* ]]; then
        exit 0 # Allowed
    fi
done

echo "🚫 [DLP] BLOCKED: Push to $url is not allowed."
echo "💡 Allowed repos: {whitelist_display}"

# Gửi email cảnh báo - Logic đã sửa:
# Kiểm tra file tồn tại, sau đó chạy lệnh run_cmd đã được Python định nghĩa đúng
if [ -f "{agent_script_escaped}" ]; then
    # Dùng nohup hoặc & để chạy nền, chuyển hướng output để debug nếu cần
    {run_cmd} --git-push-alert "$url" > /tmp/dlp_git_email.log 2>&1 &
else
    echo "⚠️ [DLP] Agent script not found at: {agent_script_escaped}" >&2
fi

exit 1
"""
        
        with open(HOOK_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(pre_push_script)
        
        # 4. Cấp quyền thực thi
        st = os.stat(HOOK_FILE)
        os.chmod(HOOK_FILE, st.st_mode | stat.S_IEXEC)
        
        # 5. Cấu hình Git Global
        subprocess.run(["git", "config", "--global", "core.hooksPath", HOOKS_DIR], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        print("✅ Git Firewall is ACTIVE (Blocking external repo pushes)")
    except Exception as e:
        print(f"❌ Git Firewall Setup Error: {e}")

def cleanup_git_firewall():
    """Gỡ bỏ Git Firewall khi chương trình tắt"""
    try:
        # Gỡ bỏ cấu hình core.hooksPath
        subprocess.run(["git", "config", "--global", "--unset", "core.hooksPath"], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("🔓 Git Firewall disabled")
    except Exception as e:
        pass  # Silent fail on cleanup

def monitor_git_config():
    """Monitor và đảm bảo Git Firewall không bị tắt khi app đang chạy"""
    while RUN_FLAG:
        try:
            result = subprocess.run(["git", "config", "--global", "core.hooksPath"], 
                                    capture_output=True, text=True, timeout=1)
            current_path = result.stdout.strip()
            
            if current_path != HOOKS_DIR:
                # User đã thay đổi config, enforce lại
                subprocess.run(["git", "config", "--global", "core.hooksPath", HOOKS_DIR],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
        time.sleep(5)

def start_git_firewall():
    """Khởi động Git Firewall và monitor thread"""
    setup_git_firewall()
    # Đăng ký cleanup khi exit
    atexit.register(cleanup_git_firewall)
    # Chạy monitor thread
    t = threading.Thread(target=monitor_git_config, daemon=True)
    t.start()

def get_system_detail():
    """Thu thập thông tin hệ thống để đưa vào email alert."""
    try:
        hostname = socket.gethostname() or platform.node() or "Unknown"
        # Lấy IP nội bộ
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip_address = s.getsockname()[0]
            s.close()
        except:
            ip_address = "127.0.0.1"

        try:
            user = os.getenv('SUDO_USER') or os.getenv('USER') or os.getlogin()
        except Exception:
            user = "Unknown"

        local_time = time.strftime("%d/%m/%Y %I:%M:%S %p", time.localtime())
        return {
            "user": user,
            "email_mock": f"{user}@{hostname}",
            "device": hostname,
            "ip": ip_address,
            "time_local": local_time
        }
    except Exception:
        return {
            "user": "Unknown",
            "email_mock": "Unknown",
            "device": "Unknown",
            "ip": "Unknown",
            "time_local": "Unknown"
        }


def send_email_clipboard_paste(content_preview, violated_app="Unknown App"):
    """Gửi email cảnh báo DLP cho Clipboard Paste (text)."""
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECEIVER:
        return

    sys_info = get_system_detail()

    if isinstance(content_preview, str):
        preview = content_preview[:800] + "..." if len(content_preview) > 800 else content_preview
        preview = preview.replace("<", "&lt;").replace(">", "&gt;")
    else:
        preview = "[Non-text Content]"

    alert_id = str(uuid.uuid4())
    subject = "Medium-severity alert: DLP policy matched for clipboard content in a device"

    html_body = f"""
    <html><body style="font-family: 'Segoe UI', sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
        <div style="background-color: #fff; padding: 40px; border-radius: 8px; border-top: 6px solid #d83b01; max-width: 750px; margin: auto; box-shadow: 0 2px 10px rgba(0,0,0,0.05);">
            <h2 style="color: #212529; margin-top: 0;">A medium-severity alert has been triggered</h2>
            <p style="font-size: 15px; color: #666;">DLP policy matched for clipboard content on a managed device (macOS).</p>
            <div style="background-color: #faf9f8; padding: 15px; border-left: 4px solid #a4262c; margin: 20px 0;">
                <strong style="color: #a4262c;">Severity: Medium</strong>
            </div>
            <table style="width: 100%; font-size: 14px; line-height: 1.8; border-collapse: collapse;">
                <tr><td style="width: 220px; font-weight: bold; color: #444;">Time of occurrence:</td><td>{sys_info['time_local']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Activity:</td><td>DlpRuleMatch (Clipboard Paste)</td></tr>
                <tr><td style="font-weight: bold; color: #444;">User:</td><td style="color: #0078d4;">{sys_info['email_mock']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Policy:</td><td>DLP_Block_SourceCode</td></tr>
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
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    try:
        server = smtplib.SMTP("smtp.office365.com", 587)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("📧 [EMAIL] Clipboard Paste alert sent")
    except Exception as e:
        print(f"Email Error: {e}")

def send_email_file_copy(file_path, violated_app="Unknown App"):
    """Gửi email cảnh báo DLP cho Copy FileCode."""
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECEIVER:
        return

    sys_info = get_system_detail()

    # Đọc preview file nếu có thể
    file_content_preview = read_file_safe(file_path)
    if file_content_preview:
        preview = file_content_preview[:800] + "..." if len(file_content_preview) > 800 else file_content_preview
        preview = preview.replace("<", "&lt;").replace(">", "&gt;")
    else:
        preview = f"[File: {os.path.basename(file_path)}]"

    alert_id = str(uuid.uuid4())
    subject = "Medium-severity alert: DLP policy matched for file copy in a device"

    html_body = f"""
    <html><body style="font-family: 'Segoe UI', sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
        <div style="background-color: #fff; padding: 40px; border-radius: 8px; border-top: 6px solid #d83b01; max-width: 750px; margin: auto; box-shadow: 0 2px 10px rgba(0,0,0,0.05);">
            <h2 style="color: #212529; margin-top: 0;">A medium-severity alert has been triggered</h2>
            <p style="font-size: 15px; color: #666;">DLP policy matched for file copy on a managed device (macOS).</p>
            <div style="background-color: #faf9f8; padding: 15px; border-left: 4px solid #a4262c; margin: 20px 0;">
                <strong style="color: #a4262c;">Severity: Medium</strong>
            </div>
            <table style="width: 100%; font-size: 14px; line-height: 1.8; border-collapse: collapse;">
                <tr><td style="width: 220px; font-weight: bold; color: #444;">Time of occurrence:</td><td>{sys_info['time_local']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Activity:</td><td>DlpRuleMatch (Copy FileCode)</td></tr>
                <tr><td style="font-weight: bold; color: #444;">User:</td><td style="color: #0078d4;">{sys_info['email_mock']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Policy:</td><td>DLP_Block_SourceCode</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Alert ID:</td><td style="color: #666; font-family: monospace;">{alert_id}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Application:</td><td style="color: #d83b01; font-weight: bold;">{violated_app}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">File Path:</td><td style="color: #666; font-family: monospace;">{file_path}</td></tr>
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
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    try:
        server = smtplib.SMTP("smtp.office365.com", 587)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("📧 [EMAIL] File Copy alert sent")
    except Exception as e:
        print(f"Email Error: {e}")

def send_email_git_push(repo_url, violated_app="Git"):
    """Gửi email cảnh báo DLP cho Git Push repo ngoài whitelist."""
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECEIVER:
        return

    sys_info = get_system_detail()

    alert_id = str(uuid.uuid4())
    subject = "Medium-severity alert: DLP policy matched for git push in a device"

    html_body = f"""
    <html><body style="font-family: 'Segoe UI', sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
        <div style="background-color: #fff; padding: 40px; border-radius: 8px; border-top: 6px solid #d83b01; max-width: 750px; margin: auto; box-shadow: 0 2px 10px rgba(0,0,0,0.05);">
            <h2 style="color: #212529; margin-top: 0;">A medium-severity alert has been triggered</h2>
            <p style="font-size: 15px; color: #666;">DLP policy matched for git push to external repository on a managed device (macOS).</p>
            <div style="background-color: #faf9f8; padding: 15px; border-left: 4px solid #a4262c; margin: 20px 0;">
                <strong style="color: #a4262c;">Severity: Medium</strong>
            </div>
            <table style="width: 100%; font-size: 14px; line-height: 1.8; border-collapse: collapse;">
                <tr><td style="width: 220px; font-weight: bold; color: #444;">Time of occurrence:</td><td>{sys_info['time_local']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Activity:</td><td>DlpRuleMatch (Git Push)</td></tr>
                <tr><td style="font-weight: bold; color: #444;">User:</td><td style="color: #0078d4;">{sys_info['email_mock']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Policy:</td><td>DLP_Block_SourceCode</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Alert ID:</td><td style="color: #666; font-family: monospace;">{alert_id}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Repository URL:</td><td style="color: #d83b01; font-weight: bold; font-family: monospace;">{repo_url}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Device:</td><td>{sys_info['device']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">IP:</td><td>{sys_info['ip']}</td></tr>
                <tr><td style="font-weight: bold; color: #444;">Status:</td><td style="color: #a4262c; font-weight: bold;">BLOCK</td></tr>
            </table>
            <hr style="border: 0; border-top: 1px solid #e1dfdd; margin: 25px 0;">
            <h3 style="font-size: 16px;">Details:</h3>
            <div style="background-color: #f3f2f1; padding: 15px; border: 1px solid #e1dfdd; font-family: Consolas, monospace; font-size: 13px; color: #d13438;">
                Attempted to push code to external repository outside whitelist.
            </div>
        </div>
    </body></html>
    """

    msg = MIMEMultipart()
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    try:
        server = smtplib.SMTP("smtp.office365.com", 587)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("📧 [EMAIL] Git Push alert sent")
    except Exception as e:
        print(f"Email Error: {e}")
    
def trigger_email_async(content, app_name="Unknown", email_type="clipboard"):
    """Trigger email async với loại email khác nhau.
    
    Args:
        content: Nội dung (text hoặc file path)
        app_name: Tên app vi phạm
        email_type: "clipboard" (Clipboard Paste), "file" (Copy FileCode), hoặc "git" (Git Push)
    """
    if email_type == "file":
        threading.Thread(target=send_email_file_copy, args=(content, app_name)).start()
    elif email_type == "git":
        threading.Thread(target=send_email_git_push, args=(content, app_name)).start()
    else:  # clipboard (default)
        threading.Thread(target=send_email_clipboard_paste, args=(content, app_name)).start()

def show_native_alert(title, message):
    """Hiển thị popup ở giữa màn hình với nội dung cố định, đơn giản."""
    try:
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        # Dùng display alert để hiện hộp thoại giữa màn hình với icon cảnh báo mặc định
        cmd = f'''display alert "{safe_title}" message "{safe_msg}" as critical buttons {{"OK"}} default button "OK"'''
        subprocess.run(["osascript", "-e", cmd], check=False)
    except Exception:
        pass

def trigger_popup_async(title, message):
    threading.Thread(target=show_native_alert, args=(title, message), daemon=True).start()
    
def show_custom_alert(header, body):
    """Hiển thị alert đơn giản, dùng thread để không chặn luồng chính."""
    trigger_popup_async(header, body)
    
def show_alert(app_name, source_app="Unknown"):
    """Alert DLP cố định, không hiển thị From/To."""
    show_custom_alert("Policy Violation", "Copying Source Code to external apps is restricted.")

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
    except:
        return "CODE"

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
    """Hiện cảnh báo sau khi AI xác định là CODE (không phụ thuộc Cmd+V).
    Áp dụng cho cả browser (chatbot domain) và app ngoài whitelist."""
    try:
        time.sleep(0.1)  # Delay ngắn 0.3 giây để đảm bảo AI check hoàn tất
        
        # Remove khỏi warning_threads để có thể warn lại sau này
        STATE["warning_threads"].discard(data_hash)

        # Double check: chỉ hiện warning nếu vẫn là CODE và chưa warn hash này
        if STATE["content_type"] == "CODE" and data_hash not in STATE["warned_hashes"]:
            # Đánh dấu đã warn để không warn lại trong cùng session
            STATE["warned_hashes"].add(data_hash)
            
            # Chỉ warn nếu vẫn đang ở đúng app đích
            if STATE["current_app"] == app_name:
                show_alert(app_name, source_app)

                # Gửi email CHỈ khi:
                #  - App không nằm trong ALLOWED_APPS
                #  - Và (không phải browser) HOẶC là browser nhưng domain KHÔNG thuộc ALLOWED_DOMAINS
                should_email = False
                if app_name not in ALLOWED_APPS:
                    if app_name in BROWSER_APPS:
                        # Browser: chỉ email nếu domain KHÔNG cho phép
                        if not STATE.get("browser_allowed", False):
                            should_email = True
                    else:
                        # App thường: luôn email nếu không nằm whitelist
                        should_email = True

                if should_email and STATE.get("hidden_data"):
                    # Phân biệt file vs text để gửi email đúng loại
                    if STATE["hidden_type"] == "file":
                        # Copy FileCode
                        trigger_email_async(STATE["hidden_data"], app_name=app_name, email_type="file")
                    else:
                        # Clipboard Paste (text)
                        trigger_email_async(STATE["hidden_data"], app_name=app_name, email_type="clipboard")
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
#   MAIN
# ==============================
def main():
    print("🚀 DLP Agent Started...")
    start_smart_killer()
    start_git_firewall()  # Khởi động Git Firewall
    
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

    # 3. Git Push Alert Handler (được gọi từ git hook)
    if len(sys.argv) > 1 and sys.argv[1] == "--git-push-alert":
        if len(sys.argv) > 2:
            repo_url = sys.argv[2]
            # Gửi email và exit (không cần single instance check)
            try:
                send_email_git_push(repo_url)
            except Exception as e:
                print(f"Error sending git push alert: {e}", file=sys.stderr)
            sys.exit(0)
        else:
            print("Usage: dlp_agent_mac.py --git-push-alert <repo_url>", file=sys.stderr)
            sys.exit(1)

    # 4. Chạy chính (DLP Agent)
    ensure_single_instance()
    
    if not AZURE_ENDPOINT or not AZURE_KEY or not AZURE_MODEL:
        print("❌ Missing Azure config in .env")
        sys.exit(1)
    
    try:
        main()
    except KeyboardInterrupt:
        pass