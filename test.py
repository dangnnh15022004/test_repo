# -*- coding: utf-8 -*-
import os
import sys
import time
import uuid
import socket
import platform
import hashlib
import subprocess
import threading
import smtplib
import psutil
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from openai import OpenAI
from pynput import keyboard
from pynput.keyboard import Key, Listener
from AppKit import NSWorkspace, NSWorkspaceDidActivateApplicationNotification, NSPasteboard, NSPasteboardTypeString
from Foundation import NSObject, NSURL
from PyObjCTools import AppHelper

# Whitelist - Allowed apps (copy/paste được trong các app này)
ALLOWED_CODE_APPS_MAC = {
    "Code", "Electron", "PyCharm", "IntelliJ IDEA", "CLion",
    "PhpStorm", "WebStorm", "Sublime Text", "Xcode", "Terminal", "iTerm2"
}

# Banned apps - Apps chụp ảnh bị kill
BANNED_APPS_MAC = [
    "Screenshot", "Grab", "Skitch", "Lightshot", "Gyazo",
    "screencapture", "Snippets", "CleanShot X", "Monosnap", "Snip"
]

# Load configuration
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

# Load Azure LLM Config
AZURE_ENDPOINT = os.getenv("AZURE_INFERENCE_ENDPOINT")
AZURE_KEY = os.getenv("AZURE_INFERENCE_KEY")
AZURE_MODEL = os.getenv("AZURE_INFERENCE_MODEL")

# Load Email Config
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")

# Global flag for killer thread
RUN_FLAG = True

print("🔥 Đang khởi động 'DLP Agent (Fast Mode)'...")
print("🎯 Mục tiêu: Xóa clipboard ngay lập tức khi chuyển App (trừ allowed app)")
print("---------------------------------------------------------")

# ==============================
#   KILL BANNED APPS
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
    except Exception: 
        pass

def start_smart_killer():
    """Bắt đầu thread kill banned apps liên tục"""
    def loop_kill():
        while RUN_FLAG:
            kill_banned_windows()
            time.sleep(1.0)
    t = threading.Thread(target=loop_kill)
    t.daemon = True
    t.start()

# ==============================
#   EMAIL ALERTS
# ==============================
def get_system_detail():
    """Lấy thông tin hệ thống"""
    try:
        hostname = socket.gethostname()
        if not hostname: 
            hostname = platform.node()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip_address = s.getsockname()[0]
            s.close()
        except: 
            ip_address = "127.0.0.1"

        user = os.getenv('SUDO_USER') or os.getenv('USER') or os.getlogin()
        local_time = time.strftime("%d/%m/%Y %I:%M:%S %p", time.localtime())
        return {
            "user": user,
            "email_mock": f"{user}@{hostname}",
            "device": hostname,
            "ip": ip_address,
            "time_local": local_time
        }
    except: 
        return {
            "user": "Unknown", 
            "email_mock": "Unknown", 
            "device": "Unknown", 
            "ip": "Unknown", 
            "time_local": "Unknown"
        }

def send_email_alert(content_preview, violated_app="Unknown App"):
    """Gửi email alert khi có violation"""
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECEIVER: 
        return
    
    sys_info = get_system_detail()
    if isinstance(content_preview, str):
        preview = content_preview[:800] + "..." if len(content_preview) > 800 else content_preview
        preview = preview.replace("<", "&lt;").replace(">", "&gt;")
    else: 
        preview = "[Image Content]"

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
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER
    msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html'))

    try:
        server = smtplib.SMTP('smtp.office365.com', 587)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"📧 [EMAIL] Alert sent successfully")
    except Exception as e: 
        print(f"📧 [EMAIL] Error: {e}")

def trigger_email_async(content, app_name="Unknown"):
    """Gửi email async trong thread riêng"""
    threading.Thread(target=send_email_alert, args=(content, app_name)).start()

# ==============================
#   UTILS: FILE READER
# ==============================
def read_file_safe(file_path):
    """Đọc nội dung file an toàn"""
    try:
        if not os.path.exists(file_path): return None
        if os.path.getsize(file_path) > 2 * 1024 * 1024:  # > 2MB
            return None
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            head = f.read(4096)
            if '\0' in head: return None  # Binary file
            f.seek(0)
            return f.read(5000)  # Đọc 5000 ký tự đầu
    except:
        return None

# ==============================
#   UTILS: NATIVE CLIPBOARD (FAST - từ test1.py)
# ==============================
def get_and_clear_clipboard():
    """
    Lấy dữ liệu và XÓA NGAY LẬP TỨC.
    Sử dụng NSURL để xử lý đường dẫn file chính xác.
    Trả về (data_type, data) - type là "text" hoặc "file"
    """
    try:
        pb = NSPasteboard.generalPasteboard()
        types = pb.types()
        
        data_type = "text"
        content = None

        # 1. Ưu tiên check FILE trước
        if "public.file-url" in types:
            url_str = pb.stringForType_("public.file-url")
            if url_str:
                ns_url = NSURL.URLWithString_(url_str)
                if ns_url and ns_url.isFileURL():
                    data_type = "file"
                    content = ns_url.path()  # Lấy đường dẫn sạch
        
        # 2. Nếu không phải file, lấy TEXT
        elif NSPasteboardTypeString in types and data_type != "file":
            data_type = "text"
            content = pb.stringForType_(NSPasteboardTypeString)
        
        # 3. Xóa ngay lập tức nếu có dữ liệu
        if content:
            pb.clearContents()
            
        return data_type, content
    except Exception as e:
        print(f"Error Clipboard: {e}")
        return None, None

def restore_clipboard(data_type, data):
    """Khôi phục dữ liệu đúng định dạng"""
    if not data: return
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        
        if data_type == "text":
            pb.setString_forType_(data, NSPasteboardTypeString)
        elif data_type == "file":
            # Restore File chuẩn Native
            url_obj = NSURL.fileURLWithPath_(data)
            if url_obj:
                pb.writeObjects_([url_obj])
            else:
                pb.setString_forType_(data, NSPasteboardTypeString)
    except Exception as e:
        print(f"Error Restore: {e}")

# ==============================
#   AI LLM CLASSIFICATION
# ==============================
def hash_data(data):
    """Hash content để track thay đổi"""
    if isinstance(data, str):
        return hashlib.md5(data.encode('utf-8')).hexdigest()
    return hashlib.md5(str(data).encode('utf-8')).hexdigest()

def call_llm(client, model, content):
    """Gọi Azure LLM để phân loại content là CODE hay TEXT"""
    sys_p = "Analyze input. If contains programming code (Python, Java, HTML, etc) output '[CONCLUSION: CODE]'. Else '[CONCLUSION: TEXT]'."
    if isinstance(content, str):
        msgs = [{"role":"system","content":sys_p}, {"role":"user","content":content[:3000]}]
        try:
            res = client.chat.completions.create(model=model, messages=msgs, max_tokens=20, temperature=0)
            out = res.choices[0].message.content or ""
            result = "CODE" if "CODE" in out else "TEXT"
            print(f"🤖 [LLM] Result: {result}")
            return result
        except Exception as e:
            print(f"🤖 [LLM] Error: {e}")
            return "CODE"  # Default to CODE nếu lỗi
    return "TEXT"

class TrapdoorHandler(NSObject):
    """
    Class này lắng nghe sự kiện từ hệ điều hành MacOS.
    Nó chạy song song với hệ thống, không bị delay bởi vòng lặp Python.
    """
    def init(self):
        # Không cần gọi super().init() cho NSObject trong PyObjC
        self.source_app = None
        self.saved_content = None
        self.saved_content_type = None  # "text" hoặc "file"
        self.current_app = None  # Track app hiện tại
        self.content_hash = None  # Track hash của content
        self.content_type = None  # CODE, TEXT, hoặc None (chưa check)
        
        # Khởi tạo LLM client nếu có config
        self.llm_client = None
        self.llm_cache = {}  # Cache kết quả LLM theo hash
        if AZURE_ENDPOINT and AZURE_KEY and AZURE_MODEL:
            try:
                self.llm_client = OpenAI(base_url=AZURE_ENDPOINT, api_key=AZURE_KEY)
                print(f"🤖 [LLM] Azure LLM initialized")
            except Exception as e:
                print(f"🤖 [LLM] Failed to initialize: {e}")
        
        return self
    
    def handleAppActivation_(self, notification):
        try:
            # 1. Lấy tên App vừa được Active
            user_info = notification.userInfo()
            active_app = user_info['NSWorkspaceApplicationKey'].localizedName()
            self.current_app = active_app  # Update current app
            
            # 2. Nếu vào App Allowed -> Restore nếu đang giữ
            if active_app in ALLOWED_CODE_APPS_MAC:
                if self.saved_content:
                    restore_clipboard(self.saved_content_type, self.saved_content)
                    print(f"✅ [RESTORE] {active_app} -> Allowed")
                    self.saved_content = None
                    self.saved_content_type = None
                return

            # 3. Vào App Lạ -> CƯỚP VÀ XÓA NGAY (sử dụng Native API - nhanh)
            d_type, data = get_and_clear_clipboard()
            
            if not data:
                # Nếu clipboard rỗng, có thể do mình đã xóa trước đó, check kho dự trữ
                if self.saved_content:
                    d_type = self.saved_content_type
                    data = self.saved_content
                else:
                    return  # Không có gì để xử lý

            # 4. Lưu kho
            self.source_app = active_app
            self.saved_content = data
            self.saved_content_type = d_type
            
            # Tính hash từ content (nếu file thì đọc content để hash)
            if d_type == "file":
                file_content = read_file_safe(data)
                if file_content:
                    curr_hash = hash_data(file_content)
                else:
                    # File binary/large -> dùng hash của path
                    curr_hash = hash_data(data)
            else:
                curr_hash = hash_data(data)
            
            if self.content_hash != curr_hash:
                # Copy mới
                self.content_hash = curr_hash
                self.content_type = None  # Reset, chưa check LLM
                display_info = os.path.basename(data) if d_type == "file" else "Text"
                print(f"📋 [COPY] App: {active_app} | Type: {d_type.upper()} ({display_info})")
            else:
                # Content giống saved_content → Chuyển app
                display_info = os.path.basename(data) if d_type == "file" else f"'{data[:30]}...'"
                print(f"⚡ [BLOCKED] Chuyển sang: {active_app} | Source: {self.source_app} | {display_info}")
            
            # 5. Chạy AI ở luồng khác
            threading.Thread(target=self.async_analysis, args=(active_app, d_type, data, curr_hash)).start()

        except Exception as e:
            print(f"Lỗi: {e}")
    
    def async_analysis(self, app_name, d_type, data, content_hash):
        """Hàm chạy ngầm: Check AI và quyết định có trả lại dữ liệu không"""
        content_to_check = None
        
        # A. Nếu là FILE -> Đọc nội dung bên trong
        if d_type == "file":
            content_to_check = read_file_safe(data)
            if content_to_check is None:
                # File ảnh/nhị phân hoặc quá lớn -> An toàn -> Trả lại
                if self.current_app == app_name:
                    restore_clipboard("file", data)
                    print(f"   ✅ [SAFE] File Binary/Ảnh -> Auto Restore")
                return
        else:
            # Nếu là TEXT
            content_to_check = data

        # B. Check AI (async - không block)
        if not self.llm_client or not AZURE_MODEL:
            self.content_type = "CODE"
            return
        
        try:
            # Check cache trước
            if content_hash in self.llm_cache:
                verdict = self.llm_cache[content_hash]
                print(f"   🤖 [LLM] Cache hit: {verdict}")
            else:
                # Gọi LLM
                verdict = call_llm(self.llm_client, AZURE_MODEL, content_to_check)
                self.llm_cache[content_hash] = verdict
            
            self.content_type = verdict
            print(f"   🤖 AI: {verdict}")

            # C. KHÔNG restore TEXT khi chuyển app - chỉ restore khi paste (Cmd+V)
            # Việc restore sẽ được xử lý trong on_paste_attempt()
        except Exception as e:
            print(f"   🤖 [LLM] Error: {e}")
            self.content_type = "CODE"
    
    def check_llm_sync(self, content, content_hash):
        """Gọi LLM sync để check content type (blocking)"""
        if not self.llm_client or not AZURE_MODEL:
            # Không có LLM config, default to CODE
            self.content_type = "CODE"
            return "CODE"
        
        try:
            # Check cache trước
            if content_hash in self.llm_cache:
                result = self.llm_cache[content_hash]
                print(f"🤖 [LLM] Cache hit: {result}")
            else:
                # Gọi LLM
                result = call_llm(self.llm_client, AZURE_MODEL, content)
                self.llm_cache[content_hash] = result
            
            self.content_type = result
            return result
        except Exception as e:
            print(f"🤖 [LLM] Sync check error: {e}")
            self.content_type = "CODE"  # Default to CODE nếu lỗi
            return "CODE"
    
    def on_paste_attempt(self):
        """Được gọi khi người dùng nhấn Cmd+V"""
        try:
            # Lấy app hiện tại
            current_app = self.current_app
            if not current_app:
                # Fallback: lấy app hiện tại từ system
                try:
                    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
                    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
                    current_app = result.stdout.strip()
                except:
                    return
            
            # Chỉ alert nếu:
            # 1. App hiện tại KHÔNG phải allowed app
            # 2. Có saved_content (đã copy trước đó)
            if current_app not in ALLOWED_CODE_APPS_MAC and self.saved_content:
                # Nếu chưa có kết quả LLM, đợi LLM trả lời xong
                if self.content_type is None:
                    print(f"⏳ [PASTE ATTEMPT] App: {current_app} | Waiting for LLM result...")
                    # Lấy content để check LLM
                    if self.saved_content_type == "file":
                        # Đọc nội dung file để check LLM
                        file_content = read_file_safe(self.saved_content)
                        if file_content:
                            result = self.check_llm_sync(file_content, self.content_hash)
                        else:
                            # File không đọc được → Default to CODE
                            result = "CODE"
                            self.content_type = "CODE"
                    else:
                        # Text content
                        result = self.check_llm_sync(self.saved_content, self.content_hash)
                    
                    # Nếu là TEXT, restore clipboard và không alert
                    if result == "TEXT":
                        restore_clipboard(self.saved_content_type, self.saved_content)
                        print(f"✅ [PASTE ALLOWED] App: {current_app} | LLM classified as TEXT, clipboard restored")
                        return  # Không alert
                
                # Chỉ alert nếu là CODE
                if self.content_type == "CODE":
                    print(f"🚨 [PASTE ATTEMPT] App: {current_app} | Source: {self.source_app} | Type: CODE")
                    if self.saved_content_type == "file":
                        print(f"   📁 File: {os.path.basename(self.saved_content)}")
                    self.show_alert(current_app)
                    # Gửi email alert (gửi file content nếu có thể đọc được)
                    if self.saved_content_type == "file":
                        alert_content = read_file_safe(self.saved_content) or f"[File: {os.path.basename(self.saved_content)}]"
                        trigger_email_async(alert_content, app_name=current_app)
                    else:
                        trigger_email_async(self.saved_content, app_name=current_app)
                elif self.content_type == "TEXT":
                    print(f"✅ [PASTE ALLOWED] App: {current_app} | LLM classified as TEXT")
                else:
                    # Fallback: nếu vẫn không có kết quả hoặc None -> Default to CODE và alert
                    print(f"⚠️ [PASTE ATTEMPT] App: {current_app} | Type: {self.content_type} (None/Unknown), defaulting to CODE and alerting")
                    self.content_type = "CODE"  # Set để đảm bảo
                    self.show_alert(current_app)
                    # Gửi email alert
                    if self.saved_content_type == "file":
                        alert_content = read_file_safe(self.saved_content) or f"[File: {os.path.basename(self.saved_content)}]"
                        trigger_email_async(alert_content, app_name=current_app)
                    else:
                        trigger_email_async(self.saved_content, app_name=current_app)
        except Exception as e:
            print(f"Lỗi on_paste_attempt: {e}")
    
    def show_alert(self, app_name):
        """Hiện alert khi paste ra ngoài allowed app"""
        try:
            safe_msg = f"Copying from {self.source_app} to {app_name} is restricted."
            cmd = f'''display alert "Policy Violation" message "{safe_msg}" as critical buttons {{"OK"}} default button "OK" giving up after 10'''
            subprocess.Popen(["osascript", "-e", cmd])
        except:
            pass

def start_keyboard_listener(handler):
    """Bắt đầu lắng nghe phím Cmd+V"""
    def on_hotkey():
        handler.on_paste_attempt()
    
    # Tạo hotkey cho Cmd+V
    hotkey = keyboard.GlobalHotKeys({
        '<cmd>+v': on_hotkey
    })
    hotkey.start()
    return hotkey

def main():
    # Bắt đầu smart killer để kill banned apps
    start_smart_killer()
    
    # Thiết lập Listener
    handler = TrapdoorHandler.alloc().init()
    workspace = NSWorkspace.sharedWorkspace()
    notification_center = workspace.notificationCenter()
    
    # Đăng ký nhận thông báo khi App thay đổi
    notification_center.addObserver_selector_name_object_(
        handler,
        "handleAppActivation:",
        NSWorkspaceDidActivateApplicationNotification,
        None
    )
    
    # Bắt đầu keyboard listener cho Cmd+V
    keyboard_listener = start_keyboard_listener(handler)
    
    print(f"✅ Allowed apps: {', '.join(sorted(ALLOWED_CODE_APPS_MAC))}")
    print(f"🚫 Banned apps: {', '.join(BANNED_APPS_MAC)}")
    print("👀 Đang theo dõi...")
    print("   - Copy/paste trong allowed app: ✅ Cho phép")
    print("   - Copy/paste ra ngoài: 🚫 Block + Alert (khi nhấn Cmd+V) + Email")
    print("   - Banned apps: 🚫 Tự động kill")
    print("---------------------------------------------------------")
    
    # Chạy vòng lặp sự kiện của MacOS
    AppHelper.runConsoleEventLoop()

if __name__ == "__main__":
    main()