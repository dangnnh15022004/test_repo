import os
import subprocess
import stat
import time
import threading
import atexit
import signal
import sys

# --- Cấu hình ---
WHITELIST_REPO = ["gitlab.siguna.co", "mycompany.internal"]
HOME_DIR = os.path.expanduser("~")
HOOKS_DIR = os.path.join(HOME_DIR, ".dlp_git_hooks")
HOOK_FILE = os.path.join(HOOKS_DIR, "pre-push")

PRE_PUSH_SCRIPT = f"""#!/bin/bash
# DLP Agent Git Firewall
remote="$1"
url="$2"
if [ -z "$url" ]; then
    url=$(git config --get remote."$remote".url)
fi

# Whitelist (Python inject vào đây)
ALLOWED_IPS=({' '.join(WHITELIST_REPO)})

for domain in "${{ALLOWED_IPS[@]}}"; do
    if [[ "$url" == *"$domain"* ]]; then
        exit 0 # Allowed
    fi
done

echo "🚫 [DLP] BLOCKED: Push to $url is not allowed."
exit 1
"""

def setup_git_firewall():
    """Cài đặt Git Firewall"""
    try:
        # 1. Tạo thư mục và file hook
        if not os.path.exists(HOOKS_DIR):
            os.makedirs(HOOKS_DIR)
        
        with open(HOOK_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(PRE_PUSH_SCRIPT)
        
        # 2. Cấp quyền thực thi
        st = os.stat(HOOK_FILE)
        os.chmod(HOOK_FILE, st.st_mode | stat.S_IEXEC)
        
        # 3. Cấu hình Git Global
        subprocess.run(["git", "config", "--global", "core.hooksPath", HOOKS_DIR], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        print("✅ Git Firewall is ACTIVE (Integrated Mode)")
    except Exception as e:
        print(f"❌ Git Firewall Setup Error: {e}")

def cleanup_git_firewall():
    """Hàm này sẽ chạy khi chương trình tắt để trả lại config cũ"""
    print("\n🧹 Đang dọn dẹp Git Firewall...")
    try:
        # Gỡ bỏ cấu hình core.hooksPath
        subprocess.run(["git", "config", "--global", "--unset", "core.hooksPath"], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # (Tùy chọn) Xóa thư mục hook nếu muốn sạch sẽ hoàn toàn
        # import shutil
        # if os.path.exists(HOOKS_DIR): shutil.rmtree(HOOKS_DIR)
        
        print("🔓 Đã gỡ bỏ chặn Git Push. Git hoạt động bình thường.")
    except Exception as e:
        print(f"❌ Cleanup Error: {e}")

def monitor_git_config():
    """Loop check để đảm bảo user không tắt firewall khi app đang chạy"""
    while True:
        try:
            result = subprocess.run(["git", "config", "--global", "core.hooksPath"], 
                                    capture_output=True, text=True)
            current_path = result.stdout.strip()
            
            if current_path != HOOKS_DIR:
                # print("⚠️ Git config modified! Re-enforcing firewall...")
                subprocess.run(["git", "config", "--global", "core.hooksPath", HOOKS_DIR],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
        time.sleep(5)

# --- Xử lý sự kiện tắt chương trình ---
def handle_exit(signum, frame):
    """Bắt sự kiện Ctrl+C hoặc Kill"""
    sys.exit(0) # Gọi sys.exit sẽ kích hoạt atexit

# Đăng ký hàm dọn dẹp sẽ chạy khi script kết thúc
atexit.register(cleanup_git_firewall)

# Đăng ký bắt tín hiệu Ctrl+C (SIGINT) và Kill (SIGTERM)
signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)

# --- Main Demo ---
if __name__ == "__main__":
    # 1. Setup ngay khi chạy
    setup_git_firewall()
    
    # 2. Chạy luồng bảo vệ
    try:
        t = threading.Thread(target=monitor_git_config, daemon=True)
        t.start()
    except Exception as e:
        print(f"❌ Thread Error: {e}")
    
    print("🚀 DLP Agent Running... (Git Push Blocked)")
    print("💡 Bấm Ctrl+C để tắt chương trình và tự động gỡ bỏ chặn.")
    
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        pass # Cho phép atexit xử lý cleanup