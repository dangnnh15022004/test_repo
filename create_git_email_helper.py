#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Script để tạo git email helper"""
import os
import stat

HOME_DIR = os.path.expanduser("~")
HOOKS_DIR = os.path.join(HOME_DIR, ".dlp_git_hooks")
helper_script = os.path.join(HOOKS_DIR, "dlp_git_email.py")

# Tìm BASE_DIR - thử nhiều vị trí
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, ".env")
if not os.path.exists(env_path):
    # Thử thư mục cha
    BASE_DIR = os.path.dirname(BASE_DIR)
    env_path = os.path.join(BASE_DIR, ".env")

print(f"📁 BASE_DIR: {BASE_DIR}")
print(f"📁 HOOKS_DIR: {HOOKS_DIR}")
print(f"📁 Helper script sẽ được tạo tại: {helper_script}")

# Tạo thư mục nếu chưa có
os.makedirs(HOOKS_DIR, exist_ok=True)

helper_code = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os
import smtplib
import uuid
import socket
import platform
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Load env từ file .env - tìm trong nhiều vị trí
BASE_DIR = "{BASE_DIR}"
env_paths = [
    os.path.join(BASE_DIR, ".env"),
    os.path.expanduser("~/.dlp_agent.env"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
]

env_loaded = False
for env_path in env_paths:
    if os.path.exists(env_path):
        try:
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and "=" in line and not line.startswith("#"):
                        key, value = line.split("=", 1)
                        os.environ[key.strip()] = value.strip().strip('\\"\\'')
            env_loaded = True
            print(f"[DLP Git Email] Loaded .env from: {{env_path}}", file=sys.stderr)
            break
        except Exception as e:
            print(f"[DLP Git Email] Error loading {{env_path}}: {{e}}", file=sys.stderr)

if not env_loaded:
    print("[DLP Git Email] Warning: No .env file found", file=sys.stderr)

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")

def get_system_detail():
    try:
        hostname = socket.gethostname() or platform.node() or "Unknown"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip_address = s.getsockname()[0]
            s.close()
        except:
            ip_address = "127.0.0.1"
        try:
            user = os.getenv('SUDO_USER') or os.getenv('USER') or os.getlogin()
        except:
            user = "Unknown"
        local_time = time.strftime("%d/%m/%Y %I:%M:%S %p", time.localtime())
        return {{
            "user": user,
            "email_mock": f"{{user}}@{{hostname}}",
            "device": hostname,
            "ip": ip_address,
            "time_local": local_time
        }}
    except:
        return {{
            "user": "Unknown",
            "email_mock": "Unknown",
            "device": "Unknown",
            "ip": "Unknown",
            "time_local": "Unknown"
        }}

def send_email_git_push(repo_url):
    try:
        print(f"[DLP Git Email] Starting email send for repo: {{repo_url}}", file=sys.stderr)
        
        if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECEIVER:
            print(f"[DLP Git Email] Missing email config: SENDER={{EMAIL_SENDER}}, RECEIVER={{EMAIL_RECEIVER}}", file=sys.stderr)
            return
        
        sys_info = get_system_detail()
        alert_id = str(uuid.uuid4())
        subject = "Medium-severity alert: DLP policy matched for git push in a device"
        
        html_body = """
        <html><body style="font-family: 'Segoe UI', sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
            <div style="background-color: #fff; padding: 40px; border-radius: 8px; border-top: 6px solid #d83b01; max-width: 750px; margin: auto; box-shadow: 0 2px 10px rgba(0,0,0,0.05);">
                <h2 style="color: #212529; margin-top: 0;">A medium-severity alert has been triggered</h2>
                <p style="font-size: 15px; color: #666;">DLP policy matched for git push to external repository on a managed device (macOS).</p>
                <div style="background-color: #faf9f8; padding: 15px; border-left: 4px solid #a4262c; margin: 20px 0;">
                    <strong style="color: #a4262c;">Severity: Medium</strong>
                </div>
                <table style="width: 100%; font-size: 14px; line-height: 1.8; border-collapse: collapse;">
                    <tr><td style="width: 220px; font-weight: bold; color: #444;">Time of occurrence:</td><td>{{time_local}}</td></tr>
                    <tr><td style="font-weight: bold; color: #444;">Activity:</td><td>DlpRuleMatch (Git Push)</td></tr>
                    <tr><td style="font-weight: bold; color: #444;">User:</td><td style="color: #0078d4;">{{email_mock}}</td></tr>
                    <tr><td style="font-weight: bold; color: #444;">Policy:</td><td>DLP_Block_SourceCode</td></tr>
                    <tr><td style="font-weight: bold; color: #444;">Alert ID:</td><td style="color: #666; font-family: monospace;">{{alert_id}}</td></tr>
                    <tr><td style="font-weight: bold; color: #444;">Repository URL:</td><td style="color: #d83b01; font-weight: bold; font-family: monospace;">{{repo_url}}</td></tr>
                    <tr><td style="font-weight: bold; color: #444;">Device:</td><td>{{device}}</td></tr>
                    <tr><td style="font-weight: bold; color: #444;">IP:</td><td>{{ip}}</td></tr>
                    <tr><td style="font-weight: bold; color: #444;">Status:</td><td style="color: #a4262c; font-weight: bold;">BLOCK</td></tr>
                </table>
                <hr style="border: 0; border-top: 1px solid #e1dfdd; margin: 25px 0;">
                <h3 style="font-size: 16px;">Details:</h3>
                <div style="background-color: #f3f2f1; padding: 15px; border: 1px solid #e1dfdd; font-family: Consolas, monospace; font-size: 13px; color: #d13438;">
                    Attempted to push code to external repository outside whitelist.
                </div>
            </div>
        </body></html>
        """.format(
            time_local=sys_info['time_local'],
            email_mock=sys_info['email_mock'],
            alert_id=alert_id,
            repo_url=repo_url,
            device=sys_info['device'],
            ip=sys_info['ip']
        )
        
        msg = MIMEMultipart()
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))
        
        print(f"[DLP Git Email] Connecting to SMTP server...", file=sys.stderr)
        server = smtplib.SMTP("smtp.office365.com", 587)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("📧 [EMAIL] Git Push alert sent successfully", file=sys.stderr)
    except Exception as e:
        import traceback
        print(f"[DLP Git Email] Error: {{e}}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        repo_url = sys.argv[1]
        send_email_git_push(repo_url)
    else:
        print("Usage: python3 dlp_git_email.py <repo_url>", file=sys.stderr)
'''

try:
    with open(helper_script, "w", encoding="utf-8") as f:
        f.write(helper_code)
    
    # Cấp quyền thực thi
    st = os.stat(helper_script)
    os.chmod(helper_script, st.st_mode | stat.S_IEXEC)
    
    print(f"✅ Đã tạo script helper: {helper_script}")
    
    # Cập nhật hook pre-push
    hook_file = os.path.join(HOOKS_DIR, "pre-push")
    if os.path.exists(hook_file):
        with open(hook_file, "r") as f:
            hook_content = f.read()
        
        # Escape đường dẫn cho bash
        helper_path_escaped = helper_script.replace('"', '\\"')
        
        # Tìm và thay thế phần gọi script helper
        old_pattern = 'if [ -n "" ] && [ -f "" ]; then'
        new_pattern = f'if [ -n "{helper_path_escaped}" ] && [ -f "{helper_path_escaped}" ]; then'
        
        if old_pattern in hook_content:
            hook_content = hook_content.replace(old_pattern, new_pattern)
            hook_content = hook_content.replace('python3 "" "$url" &', f'python3 "{helper_path_escaped}" "$url" > /tmp/dlp_git_email.log 2>&1 &')
            
            with open(hook_file, "w") as f:
                f.write(hook_content)
            
            # Cấp quyền thực thi
            st = os.stat(hook_file)
            os.chmod(hook_file, st.st_mode | stat.S_IEXEC)
            
            print(f"✅ Đã cập nhật hook: {hook_file}")
        else:
            print(f"⚠️ Không tìm thấy pattern cần thay thế trong hook. Có thể hook đã được cập nhật.")
            print(f"   Hãy đảm bảo hook có dòng: python3 \"{helper_path_escaped}\" \"$url\" &")
    else:
        print(f"⚠️ Hook file không tồn tại: {hook_file}")
        print(f"   Bạn cần restart DLP Agent để tạo hook mới.")
    
    print("\n✅ Hoàn tất! Bây giờ bạn có thể test git push lại.")
    
except Exception as e:
    print(f"❌ Lỗi: {e}")
    import traceback
    traceback.print_exc()

