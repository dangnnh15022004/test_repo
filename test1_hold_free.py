# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sys
import threading
import subprocess
import time
import urllib.parse
import hashlib
import uuid
import socket
import platform
import smtplib
import psutil
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from openai import OpenAI

try:
    from AppKit import NSWorkspace, NSWorkspaceDidActivateApplicationNotification, NSPasteboard, NSPasteboardTypeString
    from Foundation import NSObject, NSURL
    from PyObjCTools import AppHelper
    from pynput import keyboard
except ImportError:
    print("❌ Thiếu thư viện! Chạy: pip install pyobjc-framework-Cocoa openai python-dotenv pynput")
    sys.exit(1)

# ==============================
#   CONFIG
# ==============================
load_dotenv()
AZURE_ENDPOINT = os.getenv("AZURE_INFERENCE_ENDPOINT")
AZURE_KEY = os.getenv("AZURE_INFERENCE_KEY")
AZURE_MODEL = os.getenv("AZURE_INFERENCE_MODEL", "gpt-35-turbo")

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

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
RUN_FLAG = True

# ALLOWED_DOMAINS: Domain được phép copy/paste tự do như ALLOWED_APPS (không cần check, không alert)
ALLOWED_DOMAINS = [
    "github.com", "gitlab.com",
    "chatgpt.com", "openai.com",
    "gemini.google.com",
    "copilot.microsoft.com", "bing.com",
    "claude.ai", "poe.com", "chatpro.ai", "stackoverflow.com"
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
    "last_change_count": 0,
    "browser_allowed": False # Cờ đồng bộ trạng thái Domain cho phép
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
    """Check nếu domain trong ALLOWED_DOMAINS (copy/paste tự do như IDE)"""
    if not url: return False
    for domain in ALLOWED_DOMAINS:
        if domain in url: return True
    return False

def is_allowed_source(app_name, url=None):
    """Check nếu app hoặc URL là allowed source (IDE hoặc domain trong ALLOWED_DOMAINS) - copy/paste tự do"""
    # Check app name
    if app_name in ALLOWED_APPS:
        return True
    # Check URL nếu có (cho browser)
    if url and is_domain_allowed(url):
        return True
    return False

def clear_clipboard():
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()

def restore_clipboard(data_type, data):
    if not data: return
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents() 
        if data_type == "text":
            pb.setString_forType_(data, NSPasteboardTypeString)
        elif data_type == "file":
            url_obj = NSURL.fileURLWithPath_(data)
            if url_obj: pb.writeObjects_([url_obj])
            else: pb.setString_forType_(data, NSPasteboardTypeString)
    except: pass

def get_pasteboard_change_count():
    return NSPasteboard.generalPasteboard().changeCount()

def get_and_clear_clipboard():
    try:
        pb = NSPasteboard.generalPasteboard()
        types = pb.types()
        data_type = "text"
        content = None

        if "public.file-url" in types:
            url_str = pb.stringForType_("public.file-url")
            if url_str:
                ns_url = NSURL.URLWithString_(url_str)
                if ns_url and ns_url.isFileURL():
                    data_type = "file"
                    content = ns_url.path()
        elif NSPasteboardTypeString in types:
            data_type = "text"
            content = pb.stringForType_(NSPasteboardTypeString)
        
        if content: pb.clearContents()
        return data_type, content
    except: return None, None

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
#   KILLER & EMAIL
# ==============================
def kill_banned_windows():
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] in BANNED_APPS_MAC:
                proc.kill()
    except: pass

def start_smart_killer():
    t = threading.Thread(target=lambda: [kill_banned_windows(), time.sleep(1)] and True)
    t.daemon = True
    t.start()

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

def show_alert(app_name, source_app="Unknown", skip_debounce=False):
    try:
        if not skip_debounce:
            current_time = time.time()
            if (STATE["last_alert_app"] == app_name and current_time - STATE["last_alert_time"] < 2.0): return
            STATE["last_alert_time"] = current_time
            STATE["last_alert_app"] = app_name
        
        safe_msg = f"Copying from {source_app} to {app_name} is restricted."
        cmd = f'''display alert "Policy Violation" message "{safe_msg}" as critical buttons {{"OK"}} default button "OK" giving up after 5'''
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
            STATE["last_change_count"] = get_pasteboard_change_count()
            return

        # Check AI
        verdict = call_azure_llm(content)
        STATE["content_type"] = verdict
        
        if verdict == "TEXT":
            # print(f"   ✅ AI: TEXT (Safe) -> Auto Restore")
            STATE["hidden_data"] = None
            STATE["safe_hash"] = get_content_hash(data)
            restore_clipboard(d_type, data)
            time.sleep(0.1)
            STATE["last_change_count"] = get_pasteboard_change_count()
        else:
            print(f"   🤖 AI: CODE -> Blocked & Held")
            
    finally:
        STATE["llm_checking"] = False

# ==============================
#   WATCHDOG (BROWSER)
# ==============================
def browser_watchdog_loop(app_name):
    print(f"👀 Bắt đầu giám sát {app_name}...")
    STATE["monitor_active"] = True
    STATE["last_change_count"] = get_pasteboard_change_count()
    STATE["browser_allowed"] = False # Reset cờ
    
    while STATE["monitor_active"] and STATE["current_app"] == app_name:
        try:
            # --- LUÔN CẬP NHẬT TRẠNG THÁI DOMAIN ---
            current_url = get_active_browser_url(app_name)
            is_domain_allowed_now = is_domain_allowed(current_url)  # ALLOWED_DOMAINS - copy/paste tự do như IDE
            STATE["browser_allowed"] = is_domain_allowed_now # Đồng bộ trạng thái cho Listener biết

            # Nếu là domain trong ALLOWED_DOMAINS -> xử lý như IDE (copy/paste tự do)
            if is_domain_allowed_now:
                if STATE["hidden_data"]:
                    restore_clipboard(STATE["hidden_type"], STATE["hidden_data"])
                    STATE["hidden_data"] = None
                    print(f"✅ [RESTORE] {app_name} - Allowed domain")
                # Tiếp tục giám sát clipboard mới
                current_count = get_pasteboard_change_count()
                if current_count != STATE["last_change_count"]:
                    STATE["last_change_count"] = current_count
                    d_type, data = get_and_clear_clipboard()
                    if data:
                        restore_clipboard(d_type, data)  # Restore ngay cho domain allowed
                time.sleep(0.3)
                continue

            # 1. Kiểm tra Clipboard mới
            if not STATE["hidden_data"]:
                current_count = get_pasteboard_change_count()
                if current_count == STATE["last_change_count"]:
                    time.sleep(0.3)
                    continue
                
                STATE["last_change_count"] = current_count
                d_type, data = get_and_clear_clipboard()
                
                if data:
                    current_hash = get_content_hash(data)
                    # Nếu là Safe Data -> Trả lại
                    if current_hash == STATE["safe_hash"]:
                        restore_clipboard(d_type, data)
                        continue
                    
                    # Data mới -> Check
                    STATE["source_app"] = app_name
                    STATE["hidden_data"] = data
                    STATE["hidden_type"] = d_type
                    STATE["content_type"] = None
                    
                    threading.Thread(target=async_analysis_universal, args=(data, d_type)).start()
                continue

            # 2. Xử lý dữ liệu đang bị giữ (CODE)
            # Domain không allowed -> Xóa
            clear_clipboard()

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
    # Check nếu là allowed source (IDE hoặc domain trong ALLOWED_DOMAINS)
    current_url = None
    if app_name in BROWSER_APPS:
        current_url = get_active_browser_url(app_name)
    
    if is_allowed_source(app_name, current_url):
        if STATE["hidden_data"]:
            restore_clipboard(STATE["hidden_type"], STATE["hidden_data"])
            print(f"✅ [RESTORE] {app_name}" + (f" ({current_url})" if current_url else ""))
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
    else:
            return

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
    """Xử lý Alert khi nhấn Cmd+V"""
    try:
        app_name = STATE["current_app"]
        
        # Check nếu là allowed source (IDE hoặc domain trong ALLOWED_DOMAINS)
        current_url = None
        if app_name in BROWSER_APPS:
            current_url = get_active_browser_url(app_name)
        
        if is_allowed_source(app_name, current_url):
            # IDE hoặc domain trong ALLOWED_DOMAINS -> không alert, không chặn
            return

        # Domain không allowed -> chỉ alert nếu là CODE
        if STATE["content_type"] == "CODE":
             source_app = STATE.get("source_app", "Unknown")
             print(f"🚫 [PASTE ALERT] Triggered in {app_name}")
             show_alert(app_name, source_app)
             
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
    start_keyboard_listener()
    
    handler = TrapdoorHandler.new()
    ws = NSWorkspace.sharedWorkspace()
    ws.notificationCenter().addObserver_selector_name_object_(
        handler, "handleAppActivation:", NSWorkspaceDidActivateApplicationNotification, None
    )
    
    try: AppHelper.runConsoleEventLoop()
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()