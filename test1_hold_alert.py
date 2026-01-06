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
    "last_change_count": 0,
    "browser_allowed": False, # <--- NEW: Cờ đồng bộ trạng thái Domain cho phép
    "code_detected_time": 0,  # Thời gian phát hiện CODE để delay warning
    "warning_shown": False,    # Đã hiện warning chưa
    "warned_hashes": set(),    # Set các hash đã hiện warning để tránh spam
    "warning_threads": set()   # Set các hash đang có thread warning đang chạy
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
    STATE["last_change_count"] = get_pasteboard_change_count()
    STATE["browser_allowed"] = False # Reset cờ
    last_restore_time = 0
    consecutive_allowed_count = 0
    
    while STATE["monitor_active"] and STATE["current_app"] == app_name:
        try:
            # --- LUÔN CẬP NHẬT TRẠNG THÁI DOMAIN ---
            current_url = get_active_browser_url(app_name)
            is_allowed = is_domain_allowed(current_url)
            STATE["browser_allowed"] = is_allowed # [FIX] Đồng bộ trạng thái cho Listener biết

            # 1. Kiểm tra Clipboard mới (luôn check, kể cả khi có hidden_data)
            current_count = get_pasteboard_change_count()
            if current_count != STATE["last_change_count"]:
                # Clipboard đã thay đổi -> có thể user copy mới
                STATE["last_change_count"] = current_count
                d_type, data = get_and_clear_clipboard()
                
                if data:
                    current_hash = get_content_hash(data)
                    # Nếu là Safe Data -> Trả lại
                    if current_hash == STATE["safe_hash"]:
                        restore_clipboard(d_type, data)
                        if STATE["hidden_data"]:
                            STATE["hidden_data"] = None  # Clear old hidden data
                        continue
                    
                    # Data mới -> Check (cập nhật hidden_data nếu có)
                    STATE["source_app"] = app_name
                    STATE["hidden_data"] = data
                    STATE["hidden_type"] = d_type
                    STATE["content_type"] = None
                    threading.Thread(target=async_analysis_universal, args=(data, d_type)).start()
                    continue
            
            # Nếu không có hidden_data và clipboard không đổi -> sleep
            if not STATE["hidden_data"]:
                time.sleep(0.3)
                continue

            # 2. Xử lý dữ liệu đang bị giữ (CODE) - chỉ chạy khi có hidden_data
            if is_allowed:
                # Domain xịn -> Restore liên tục để user có thể paste nhiều lần
                restore_clipboard(STATE["hidden_type"], STATE["hidden_data"])
                last_restore_time = time.time()
                consecutive_allowed_count += 1
                
                # [FIX] Cho phép paste nhiều lần vào Gemini:
                # - Giữ restore clipboard liên tục (không clear hidden_data)
                # - Chỉ clear sau 5 giây restore liên tục để tránh block quá lâu
                # - User copy mới sẽ được phát hiện ở phần 1 và cập nhật hidden_data
                if consecutive_allowed_count > 33:  # ~5 giây restore liên tục (33 * 0.15s)
                    # Đã restore lâu, clear để cho phép copy/paste mới
                    STATE["hidden_data"] = None
                    consecutive_allowed_count = 0
                    print(f"   ✅ [MULTI-PASTE] Cleared state after timeout, ready for new copy")
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