# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sys
import time
import hashlib
import platform
import io
import base64
import threading
import socket
import smtplib
import uuid
import atexit
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import winreg 
import ctypes 
import tkinter as tk
from tkinter import font as tkfont
import subprocess

from dotenv import load_dotenv
import pyperclip
import psutil
from openai import OpenAI
from PIL import Image, ImageGrab, ImageTk

# Không dùng keyboard listener nữa - warning được trigger từ delayed_warning sau khi AI check

# ==============================
#   LOAD CONFIG & ENV
# ==============================
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

# ==============================
#   OS DETECTION
# ==============================
SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"
if not IS_WINDOWS: sys.exit(1)

try:
    import win32gui, win32process, win32con, win32clipboard
except ImportError:
    win32gui = None

# ==============================
#   GLOBAL CONFIG
# ==============================
APP_NAME = "DlpAgent"
RUN_FLAG = True
BIG_ICON_FILENAME = "shield_icon.png"
SMALL_ICON_FILENAME = "defend_logo.png"
APP_ICON_FILENAME = "logo.ico"

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
AZURE_ENDPOINT = os.getenv("AZURE_INFERENCE_ENDPOINT")
AZURE_KEY = os.getenv("AZURE_INFERENCE_KEY")
AZURE_MODEL = os.getenv("AZURE_INFERENCE_MODEL")

ALLOWED_APPS = {
    "Code.exe", "devenv.exe", "pycharm64.exe", "idea64.exe", "clion64.exe",
    "wt.exe", "WindowsTerminal.exe", "powershell.exe", "cmd.exe", "sublime_text.exe", 
    "Cursor.exe", "VSCodium.exe", "explorer.exe"  # Cho phép Explorer để copy file mượt hơn
}

BROWSER_APPS = {
    "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe", "CocCocBrowser.exe"
}

BANNED_WINDOW_TITLES = ["Snipping Tool", "Lightshot", "ShareX", "Greenshot", "Snip & Sketch", "Gyazo"]

BANNED_APPS_WINDOWS = [
    "SnippingTool.exe", "Lightshot.exe", "ShareX.exe", "Greenshot.exe", 
    "ScreenSketch.exe", "Gyazo.exe", "screencapture.exe"
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
    "last_clipboard_hash": None,
    "browser_allowed": False,
    "warned_hashes": set(),
    "warning_threads": set()
}

# Git Firewall Configuration
WHITELIST_REPO = ["gitlab.siguna.co", "mycompany.internal"]  # Các repo được phép push
HOOKS_DIR = os.path.join(os.path.expanduser("~"), ".dlp_git_hooks")
HOOK_FILE = os.path.join(HOOKS_DIR, "pre-push")

# ==============================
#   CORE FUNCTIONS
# ==============================
def get_content_hash(data):
    if not data: return None
    if isinstance(data, str):
        return hashlib.md5(data.encode('utf-8')).hexdigest()
    elif isinstance(data, Image.Image):
        b = io.BytesIO()
        data.save(b, "PNG")
        return hashlib.md5(b.getvalue()).hexdigest()
    return None

def get_active_browser_url(app_name):
    """Get active browser URL on Windows - check window title và tab title"""
    try:
        if win32gui:
            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                window_title = win32gui.GetWindowText(hwnd).lower()
                
                # Check các pattern phổ biến trong window title
                # Gemini thường có "Gemini" hoặc "Google AI" trong title
                if "gemini" in window_title or "google ai" in window_title:
                    return "https://gemini.google.com"
                
                # ChatGPT
                if "chatgpt" in window_title or "openai" in window_title:
                    return "https://chatgpt.com"
                
                # Claude
                if "claude" in window_title:
                    return "https://claude.ai"
                
                # GitHub
                if "github" in window_title:
                    return "https://github.com"
                
                # Stack Overflow
                if "stack overflow" in window_title:
                    return "https://stackoverflow.com"
                
                # Copilot/Bing
                if "copilot" in window_title or "bing chat" in window_title:
                    return "https://copilot.microsoft.com"
                
                # Check domain trong title (pattern: domain.com hoặc www.domain.com)
                for domain in ALLOWED_DOMAINS:
                    if domain in window_title:
                        return f"https://{domain}"
    except: pass
    return ""

def is_domain_allowed(url):
    if not url: return False
    for domain in ALLOWED_DOMAINS:
        if domain in url: return True
    return False

def clear_clipboard():
    """Xóa clipboard trên Windows - thread-safe"""
    try:
        with _clipboard_lock:
            if win32clipboard:
                opened = False
                for _ in range(10):  # Retry 10 lần
                    try:
                        win32clipboard.OpenClipboard()
                        opened = True
                        break
                    except:
                        time.sleep(0.05)
                
                if opened:
                    try:
                        win32clipboard.EmptyClipboard()
                    finally:
                        try:
                            win32clipboard.CloseClipboard()
                        except:
                            pass
            else:
                pyperclip.copy("")
    except: 
        try:
            pyperclip.copy("")
        except: pass

# Lock để tránh race condition khi truy cập clipboard
_clipboard_lock = threading.Lock()

def restore_clipboard(data_type, data):
    """Restore clipboard trên Windows - thread-safe với retry logic"""
    if not data: return
    
    # Retry logic cho clipboard operations
    max_retries = 3
    retry_delay = 0.1
    
    for attempt in range(max_retries):
        try:
            with _clipboard_lock:
                if data_type == "text":
                    if win32clipboard:
                        # Thử mở clipboard với timeout
                        opened = False
                        for _ in range(10):  # Retry 10 lần, mỗi lần 0.05s
                            try:
                                win32clipboard.OpenClipboard()
                                opened = True
                                break
                            except:
                                time.sleep(0.05)
                        
                        if opened:
                            try:
                                win32clipboard.EmptyClipboard()
                                win32clipboard.SetClipboardText(data, win32clipboard.CF_UNICODETEXT)
                            finally:
                                try:
                                    win32clipboard.CloseClipboard()
                                except:
                                    pass  # Ignore close error
                        else:
                            # Fallback to pyperclip nếu không mở được
                            pyperclip.copy(data)
                    else:
                        pyperclip.copy(data)
                    print(f"✅ Restored Text to clipboard")
                    return
                    
                elif data_type == "image":
                    # Không restore ảnh để tránh hỗ trợ các app chụp màn hình
                    return
                
                elif data_type == "file":
                    # Restore file object vào clipboard Windows
                    if os.path.exists(data):
                        file_path = os.path.abspath(data)
                        # Cách 1: Dùng PowerShell để copy file vào clipboard (đơn giản và đáng tin cậy)
                        try:
                            # Escape file path cho PowerShell
                            escaped_path = file_path.replace('\\', '\\\\').replace('"', '\\"')
                            ps_script = f'Add-Type -AssemblyName System.Windows.Forms; $file = New-Object System.Collections.Specialized.StringCollection; $file.Add("{escaped_path}"); [System.Windows.Forms.Clipboard]::SetFileDropList($file)'
                            result = subprocess.run(
                                ["powershell", "-Command", ps_script],
                                capture_output=True,
                                timeout=2,
                                check=False
                            )
                            if result.returncode == 0:
                                print(f"✅ Restored File object to clipboard: {file_path}")
                                return
                        except Exception as e1:
                            pass
                        
                        # Cách 2: Fallback - dùng win32clipboard với CF_HDROP
                        if win32clipboard:
                            try:
                                import struct
                                # DROPFILES structure (20 bytes header + file paths)
                                file_path_unicode = file_path.encode('utf-16le')
                                # Structure: pFiles (offset), pt (POINT 8 bytes), fNC, fWide, file paths
                                dropfiles = struct.pack('I', 20)  # pFiles offset
                                dropfiles += struct.pack('I', 0)   # pt.x
                                dropfiles += struct.pack('I', 0)   # pt.y
                                dropfiles += struct.pack('I', 0)   # fNC
                                dropfiles += struct.pack('I', 1)   # fWide (Unicode = 1)
                                dropfiles += file_path_unicode + b'\x00\x00'  # File path + double null terminator
                                
                                opened = False
                                for _ in range(10):
                                    try:
                                        win32clipboard.OpenClipboard()
                                        opened = True
                                        break
                                    except:
                                        time.sleep(0.05)
                                
                                if opened:
                                    try:
                                        win32clipboard.EmptyClipboard()
                                        win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, dropfiles)
                                    finally:
                                        try:
                                            win32clipboard.CloseClipboard()
                                        except:
                                            pass
                                print(f"✅ Restored File object to clipboard (method 2): {file_path}")
                                return
                            except Exception as e2:
                                pass
                        
                        # Cách 3: Fallback cuối - copy file path as text
                        try:
                            pyperclip.copy(file_path)
                            print(f"⚠️ Restored File path as text (fallback): {file_path}")
                            return
                        except: pass
                        
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            # Chỉ print error ở lần thử cuối
            if attempt == max_retries - 1:
                # Fallback to pyperclip cho text
                if data_type == "text":
                    try:
                        pyperclip.copy(data)
                        return
                    except:
                        pass
                # Không print error để tránh spam log
                # print(f"❌ Restore Error (after {max_retries} retries): {e}")

def get_clipboard_hash():
    """Get hash of current clipboard content"""
    try:
        # Thử lấy file list trước (Windows clipboard có thể có file)
        img = ImageGrab.grabclipboard()
        if isinstance(img, list):
            for path in img:
                if os.path.isfile(path):
                    return get_content_hash(path)  # Hash file path
        
        # Thử lấy text
        content = pyperclip.paste()
        if content:
            return get_content_hash(content)
        
        # Thử lấy image
        if isinstance(img, Image.Image):
            return get_content_hash(img)
    except: pass
    return None

def get_and_clear_clipboard():
    """Lấy dữ liệu và xóa clipboard - lưu file path, không đọc nội dung"""
    try:
        # Thử lấy file list trước (Windows clipboard có thể có file)
        img = ImageGrab.grabclipboard()
        if isinstance(img, list):
            for path in img:
                if os.path.isfile(path):
                    # Lưu file path, không đọc nội dung
                    clear_clipboard()
                    return "file", path
        
        # Thử lấy image
        if isinstance(img, Image.Image):
            clear_clipboard()
            return "image", img
        
        # Thử lấy text
        content = pyperclip.paste()
        if content and content.strip():
            data = content.strip()
            clear_clipboard()
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
#   SYSTEM FUNCTIONS
# ==============================
def ensure_single_instance():
    kernel32 = ctypes.windll.kernel32
    mutex_name = "Global\\DlpAgent_Fixed_Mutex_999" 
    mutex = kernel32.CreateMutexW(None, False, mutex_name)
    if kernel32.GetLastError() == 183: sys.exit(0)
    return mutex 

def kill_banned_windows():
    def kill_by_window_title():
        if not win32gui: return
        def callback(hwnd, extra):
            try:
                if win32gui.IsWindowVisible(hwnd):
                    window_text = win32gui.GetWindowText(hwnd)
                    for banned in BANNED_WINDOW_TITLES:
                        if banned.lower() in window_text.lower():
                            _, pid = win32process.GetWindowThreadProcessId(hwnd)
                            try: psutil.Process(pid).kill()
                            except: pass
            except: pass
        try: win32gui.EnumWindows(callback, None)
        except: pass

    def kill_by_process_name():
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                name = proc.info['name'] or ""
                for banned in BANNED_APPS_WINDOWS:
                    if banned.lower() == name.lower():
                        proc.kill()
                        break
            except: pass

    while RUN_FLAG:
        try:
            kill_by_window_title()
            kill_by_process_name()
        except: pass
        time.sleep(0.5)

def start_smart_killer():
    t = threading.Thread(target=kill_banned_windows); t.daemon = True; t.start()

def hide_console_window():
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd != 0: ctypes.windll.user32.ShowWindow(hwnd, 0)
    except: pass

def add_to_startup():
    try:
        exe_path = sys.executable
        path = f'"{exe_path}" "{os.path.abspath(__file__)}"' if not getattr(sys, 'frozen', False) else f'"{exe_path}"'
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            try:
                if winreg.QueryValueEx(key, APP_NAME)[0] == path: return
            except FileNotFoundError: pass
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, path)
    except: pass

def remove_from_startup():
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.DeleteValue(key, APP_NAME)
    except: pass

def get_active_app_name() -> str | None:
    if win32gui:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd: return None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return psutil.Process(pid).name()
        except: return None
    return None

# ==============================
#   GIT FIREWALL (DLP - Prevent Push to External Repos)
# ==============================
def setup_git_firewall():
    """Cài đặt Git Firewall để ngăn push lên repo ngoài"""
    try:
        # 1. Tạo thư mục và file hook
        if not os.path.exists(HOOKS_DIR):
            os.makedirs(HOOKS_DIR)
        
        # 2. XÁC ĐỊNH CÁCH GỌI LỆNH
        if getattr(sys, 'frozen', False):
            # Nếu là executable đã đóng gói (.exe)
            agent_script = sys.executable
            # Gọi trực tiếp binary
            run_cmd = f'"{agent_script}"'
        else:
            # Nếu là script Python thông thường
            agent_script = os.path.abspath(__file__)
            # Phải gọi bằng python
            run_cmd = f'python "{agent_script}"'
        
        # Escape đường dẫn cho PowerShell
        agent_script_escaped = agent_script.replace('"', '`"')
        
        # 3. Tạo pre-push script với PowerShell (Windows)
        whitelist_str = ', '.join([f'"{repo}"' for repo in WHITELIST_REPO])
        whitelist_display = ', '.join(WHITELIST_REPO)
        
        pre_push_script = f'''# DLP Agent Git Firewall (Windows PowerShell)
param(
    [string]$remote = $args[0],
    [string]$url = $args[1]
)

if ([string]::IsNullOrEmpty($url)) {{
    $url = git config --get "remote.$remote.url"
}}

# Whitelist repos
$allowedRepos = @({whitelist_str})

$isAllowed = $false
foreach ($domain in $allowedRepos) {{
    if ($url -like "*$domain*") {{
        $isAllowed = $true
        break
    }}
}}

if (-not $isAllowed) {{
    Write-Host "🚫 [DLP] BLOCKED: Push to $url is not allowed." -ForegroundColor Red
    Write-Host "💡 Allowed repos: {whitelist_display}" -ForegroundColor Yellow
    
    # Gửi email cảnh báo
    if (Test-Path "{agent_script_escaped}") {{
        Start-Process -FilePath "{run_cmd}" -ArgumentList "--git-push-alert", "`"$url`"" -WindowStyle Hidden
    }}
    
    exit 1
}}

exit 0
'''
        
        hook_file_ps1 = os.path.join(HOOKS_DIR, "pre-push.ps1")
        with open(hook_file_ps1, "w", encoding="utf-8") as f:
            f.write(pre_push_script)
        
        # 4. Tạo batch wrapper để Git có thể gọi PowerShell script (cho git.exe thuần Windows)
        pre_push_bat = os.path.join(HOOKS_DIR, "pre-push.bat")
        bat_content = f'''@echo off
powershell.exe -ExecutionPolicy Bypass -File "%~dp0pre-push.ps1" %1 %2
exit %errorlevel%
'''
        with open(pre_push_bat, "w", encoding="utf-8") as f:
            f.write(bat_content)

        # 4b. Tạo shell pre-push cho Git Bash / WSL (gọi ngược lại Python như bạn đề xuất)
        if getattr(sys, 'frozen', False):
            exe = sys.executable.replace('\\', '/')
            shell_run_cmd = f'"{exe}"'
        else:
            exe = sys.executable.replace('\\', '/')
            script_path = os.path.abspath(__file__).replace('\\', '/')
            shell_run_cmd = f'"{exe}" "{script_path}"'

        hook_file_sh = os.path.join(HOOKS_DIR, "pre-push")
        pre_push_sh_content = f"""#!/bin/sh
# DLP Agent Git Firewall (Windows, shell-based)
remote=\"$1\"
url=\"$2\"

if [ -z \"$url\" ]; then
    url=$(git config --get remote.\"$remote\".url)
fi

{shell_run_cmd} --check-git-push \"$url\"
exit $?
"""
        with open(hook_file_sh, "w", encoding="utf-8", newline="\\n") as f:
            f.write(pre_push_sh_content)
        
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

# ==============================
#   EMAIL ALERT LOGIC (MICROSOFT PURVIEW STYLE) - GIỮ NGUYÊN
# ==============================
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
            user = os.getenv('USERNAME') or os.getenv('USER') or os.getlogin()
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
    <html>
    <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; background-color: #f8f9fa; margin: 0; padding: 20px;">
        <div style="background-color: #ffffff; padding: 40px; border-radius: 8px; border-top: 6px solid #d83b01; max-width: 750px; margin: auto; box-shadow: 0 2px 10px rgba(0,0,0,0.05);">
            
            <h2 style="color: #212529; margin-top: 0;">A medium-severity alert has been triggered</h2>
            <p style="font-size: 15px; color: #666;">DLP policy matched for clipboard content on a managed device (Windows).</p>
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

            <br>


        </div>
        <div style="text-align: center; margin-top: 25px; font-size: 12px; color: #888;">
            Generated by DLP Agent • Internal Security System • Microsoft 365 Compatible
        </div>
    </body>
    </html>
    """

    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER
    msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html'))

    try:
        server = smtplib.SMTP('smtp.office365.com', 587)
        server.ehlo(); server.starttls(); server.ehlo()
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
            <p style="font-size: 15px; color: #666;">DLP policy matched for file copy on a managed device (Windows).</p>
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
            <p style="font-size: 15px; color: #666;">DLP policy matched for git push to external repository on a managed device (Windows).</p>
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

# ==============================
#   GUI ALERT (WINDOWS) - GIỮ NGUYÊN
# ==============================
def _run_popup_gui(title, message, big_icon_path, small_icon_path):
    try:
        root = tk.Tk(); root.overrideredirect(True); root.attributes('-topmost', True)
        if getattr(sys, 'frozen', False): c_dir = sys._MEIPASS if hasattr(sys, '_MEIPASS') else BASE_DIR
        else: c_dir = BASE_DIR
        app_icon = os.path.join(c_dir, APP_ICON_FILENAME)
        if os.path.exists(app_icon): 
            try: root.iconbitmap(app_icon)
            except: pass

        w, h = 380, 300
        x = root.winfo_screenwidth() - w - 20; y = root.winfo_screenheight() - h - 60
        root.geometry(f"{w}x{h}+{x}+{y}")

        f_top = tk.Frame(root, bg="#162036", height=150); f_top.pack(fill="x", side="top"); f_top.pack_propagate(False)
        if os.path.exists(big_icon_path):
            try:
                img = ImageTk.PhotoImage(Image.open(big_icon_path).resize((110, 110), Image.Resampling.LANCZOS))
                tk.Label(f_top, image=img, bg="#162036", bd=0).place(relx=0.5, rely=0.5, anchor="center"); root.img1 = img
            except: pass
        tk.Label(f_top, text="✕", bg="#162036", fg="#aaa", font=("Arial", 12), cursor="hand2").place(relx=0.95, rely=0.1, anchor="ne")
        
        f_bot = tk.Frame(root, bg="#1f1f1f"); f_bot.pack(fill="both", expand=True, side="bottom")
        f_h_s = tk.Frame(f_bot, bg="#1f1f1f"); f_h_s.pack(anchor="w", padx=20, pady=(12,0))
        if os.path.exists(small_icon_path):
            try:
                s_img = ImageTk.PhotoImage(Image.open(small_icon_path).resize((18, 18), Image.Resampling.LANCZOS))
                tk.Label(f_h_s, image=s_img, bg="#1f1f1f", bd=0).pack(side="left", padx=(0,5)); root.img2 = s_img
            except: pass
        else: tk.Label(f_h_s, text="🛡️", bg="#1f1f1f", fg="#fff").pack(side="left", padx=(0,5))
        
        tk.Label(f_h_s, text="Data Loss Prevention", bg="#1f1f1f", fg="#fff").pack(side="left")
        tk.Label(f_bot, text=title, bg="#1f1f1f", fg="#fff", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=20, pady=(2,0))
        tk.Label(f_bot, text=message, bg="#1f1f1f", fg="#ccc", wraplength=340, justify="left").pack(anchor="w", padx=20, pady=(4,15))
        btn = tk.Label(f_bot, text="Dismiss", bg="#3a3a3a", fg="#fff", font=("Segoe UI", 10, "bold"), cursor="hand2")
        btn.pack(fill="x", padx=20, pady=(0,20), ipady=12)
        btn.bind("<Button-1>", lambda e: root.destroy())
        root.after(8000, root.destroy); root.mainloop()
    except: pass

def show_alert(app_name, source_app="Unknown"):
    """Alert DLP cố định, không hiển thị From/To."""
    try:
        current_dir = sys._MEIPASS if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS') else BASE_DIR
        big_icon = os.path.join(current_dir, BIG_ICON_FILENAME)
        small_icon = os.path.join(current_dir, SMALL_ICON_FILENAME)
        
        title = "Policy Violation"
        message = "Copying Source Code to external apps is restricted."
        
        t = threading.Thread(target=_run_popup_gui, args=(title, message, big_icon, small_icon))
        t.daemon = True; t.start()
    except: pass

# ==============================
#   AI ENGINE
# ==============================
llm_cache = {}
def call_azure_llm(content):
    if not content or not AZURE_KEY: return "TEXT"
    content_hash = hashlib.md5(str(content).encode('utf-8')).hexdigest()
    if content_hash in llm_cache: return llm_cache[content_hash]

    try:
        client = OpenAI(base_url=AZURE_ENDPOINT, api_key=AZURE_KEY)
        system_prompt = "You are a DLP Agent. Input can be file content or text. If it contains source code (Python, JS, Keys, SQL), return 'CODE'. Otherwise return 'TEXT'."
        
        # Xử lý image nếu cần
        if isinstance(content, Image.Image):
            def image_to_data_url(img):
                with io.BytesIO() as buf:
                    img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                    if img.mode != "RGB": img = img.convert("RGB")
                    img.save(buf, format="PNG")
                    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": "Check this image"},
                    {"type": "image_url", "image_url": {"url": image_to_data_url(content)}}
                ]}
            ]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": str(content)[:3000]}
            ]
        
        response = client.chat.completions.create(
            model=AZURE_MODEL,
            messages=messages,
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
        # Nếu là file, đọc nội dung để check AI (nhưng restore thì restore file path)
        if d_type == "file":
            content = read_file_safe(data)
            
            # Nếu là File An toàn (Binary/Ảnh/File Safe - không đọc được)
            if content is None:
                STATE["content_type"] = "TEXT"
                STATE["hidden_data"] = None 
                STATE["safe_hash"] = get_content_hash(data)  # Hash file path
                restore_clipboard(d_type, data)  # Restore file object
                time.sleep(0.1)
                STATE["last_clipboard_hash"] = get_clipboard_hash()
                return
            
            # Có nội dung, check AI
            verdict = call_azure_llm(content)
        else:
            # Text hoặc Image
            content = data
            verdict = call_azure_llm(content)
        
        STATE["content_type"] = verdict
        
        if verdict == "TEXT":
            STATE["hidden_data"] = None
            STATE["safe_hash"] = get_content_hash(data)
            restore_clipboard(d_type, data)  # Restore đúng type (file/text/image)
            time.sleep(0.1)
            STATE["last_clipboard_hash"] = get_clipboard_hash()
        else:
            # CODE detected
            data_hash = get_content_hash(data)
            print(f"   🤖 AI: CODE -> Detected (Warning will show after delay)")
            
            # Chỉ trigger warning nếu:
            # 1. Chưa có thread warning đang chạy cho hash này
            # 2. Chưa hiện warning cho hash này
            should_warn = False
            
            if data_hash not in STATE["warning_threads"]:
                if data_hash not in STATE["warned_hashes"]:
                    should_warn = True
            
            if should_warn:
                STATE["warning_threads"].add(data_hash)
                # Trigger warning sau delay ngắn (chạy ngầm, không chặn paste)
                threading.Thread(target=delayed_warning, args=(STATE["current_app"], STATE["source_app"], data_hash), daemon=True).start()
            
    finally:
        STATE["llm_checking"] = False

def delayed_warning(app_name, source_app, data_hash):
    """Hiện cảnh báo sau khi AI xác định là CODE (không phụ thuộc Ctrl+V).
    Áp dụng cho cả browser (chatbot domain) và app ngoài whitelist."""
    try:
        time.sleep(0.1)  # Delay ngắn để đảm bảo AI check hoàn tất
        
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
                    
                    # Không restore ngay - đợi AI check xong, browser watchdog sẽ xử lý
                    continue
            
            # 2. Xử lý dữ liệu đang bị giữ (CODE) - Logic đơn giản như Mac version
            if is_allowed:
                # Domain xịn -> Restore liên tục
                if STATE["hidden_data"]:
                    restore_clipboard(STATE["hidden_type"], STATE["hidden_data"])
                    consecutive_allowed_count += 1
                    
                    if consecutive_allowed_count > 33:  # ~5 giây
                        STATE["hidden_data"] = None
                        consecutive_allowed_count = 0
                        print(f"   ✅ [MULTI-PASTE] Cleared state after timeout")
            else:
                # Domain lởm -> Xóa clipboard (giống Mac version - đơn giản)
                if STATE["hidden_data"]:
                    clear_clipboard()
                consecutive_allowed_count = 0
            
            # Nếu không có hidden_data -> sleep
            if not STATE["hidden_data"]:
                time.sleep(0.3)
                continue

            time.sleep(0.15)
        except: pass
    print(f"💤 Dừng giám sát {app_name}")

# ==============================
#   MAIN HANDLER
# ==============================
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

    # App KHÔNG được phép: xóa clipboard ngay để chặn paste tức thì
    clear_clipboard()

    if get_content_hash(data) == STATE["safe_hash"]:
        restore_clipboard(d_type, data)
        return

    STATE["hidden_data"] = data
    STATE["hidden_type"] = d_type
    STATE["content_type"] = None
    print(f"🔒 [BLOCK] {app_name}. Checking...")
    threading.Thread(target=async_analysis_universal, args=(data, d_type)).start()

# Không dùng keyboard listener nữa - warning được trigger từ delayed_warning sau khi AI check xong

# ==============================
#   MAIN LOOP
# ==============================
def main_loop():
    # Thiết lập icon
    current_dir = sys._MEIPASS if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS') else BASE_DIR
    
    if getattr(sys, 'frozen', False):
        time.sleep(0.5); hide_console_window()

    start_smart_killer()
    start_git_firewall()  # Khởi động Git Firewall
    
    last_app = None
    while RUN_FLAG:
        try:
            current_app = get_active_app_name()
            if current_app:
                current_app_normalized = current_app.lower()
                
                # Normalize app names để match
                matched_app = None
                for allowed in ALLOWED_APPS:
                    if allowed.lower() == current_app_normalized:
                        matched_app = allowed
                        break
                
                for browser in BROWSER_APPS:
                    if browser.lower() == current_app_normalized:
                        matched_app = browser
                        break
                
                if matched_app:
                    current_app = matched_app
                else:
                    current_app = current_app_normalized
                
                STATE["current_app"] = current_app
                
                if current_app != last_app:
                    handle_switch(current_app)
                    last_app = current_app
            
            time.sleep(0.2)
        except Exception as e:
            time.sleep(1)

if __name__ == "__main__":
    # 1. Cài đặt Startup
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        add_to_startup()
        sys.exit(0)

    # 2. Gỡ bỏ Startup
    if len(sys.argv) > 1 and sys.argv[1] == "--remove":
        remove_from_startup()
        try: 
            subprocess.run(f"taskkill /F /IM {APP_NAME}.exe", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
        sys.exit(0)

    # 3. Git Push Check Handler (được gọi từ git hook - trả về exit code cho Git)
    if len(sys.argv) > 1 and sys.argv[1] == "--check-git-push":
        url = sys.argv[2] if len(sys.argv) > 2 else ""
        # Cho phép nếu URL thuộc whitelist
        allowed = False
        for domain in WHITELIST_REPO:
            if domain and domain in url:
                allowed = True
                break
        if allowed:
            sys.exit(0)
        # Repo ngoài whitelist → chặn và gửi email
        try:
            print(f"🚫 [DLP] BLOCKED: Push to {url} is not allowed.")
            print(f"💡 Allowed repos: {', '.join(WHITELIST_REPO)}")
            send_email_git_push(url)
        except Exception as e:
            print(f"Error sending git push alert: {e}", file=sys.stderr)
        sys.exit(1)

    # 4. Git Push Alert Handler cũ (giữ lại cho tương thích nếu có nơi khác gọi)
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
            print("Usage: dlp_agent.py --git-push-alert <repo_url>", file=sys.stderr)
            sys.exit(1)

    # 5. Chạy chính (DLP Agent)
    _mutex = ensure_single_instance()
    
    if not AZURE_ENDPOINT or not AZURE_KEY or not AZURE_MODEL:
        print("❌ Missing Azure config in .env")
        sys.exit(1)
    
    try:
        main_loop()
    except KeyboardInterrupt:
        pass
