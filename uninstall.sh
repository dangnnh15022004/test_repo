#!/bin/bash

# ==============================
#   CONFIG (ĐÃ SỬA LỖI TÊN FILE)
# ==============================
APP_NAME="DlpAgent"
APP_BUNDLE="DlpAgent.app"
# SỬA LỖI: Đặt tên file PLIST chính xác theo kết quả ls -al của bạn
PLIST_NAME="com.dlpagent.agent.plist" 
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"

# File rác
LOCK_FILE="$HOME/.dlp_agent.lock" 
LOG_OUT="/tmp/dlp_agent.out"
LOG_ERR="/tmp/dlp_agent.err"

# Màu sắc (Giữ nguyên)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}    DLP AGENT DEEP CLEAN TOOL (v7)      ${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# ----------------------------------------
# 1. Gỡ bỏ Service (Bây giờ đã tìm thấy file)
# ----------------------------------------
echo -e "${YELLOW}[1/5] Removing Background Service...${NC}"
USER_ID=$(id -u)
PLIST_LABEL="${PLIST_NAME%.*}" # Lấy Label: com.dlpagent.agent

if [ -f "$PLIST_PATH" ]; then
    echo -e "   ℹ️ Found Plist: $PLIST_NAME"
    
    # Gỡ bỏ service khỏi launchd 
    # LƯU Ý: Biến $USER_NAME chưa được định nghĩa, sử dụng $USER_ID an toàn hơn cho launchctl
    launchctl bootout "gui/$USER_ID" "$PLIST_PATH" 2>/dev/null 
    launchctl unload "$PLIST_PATH" 2>/dev/null 

    echo -e "   ✅ Service unloaded from launchd."
    
    # Xóa file vật lý
    rm "$PLIST_PATH"
    echo -e "   ✅ Removed Plist file."
else
    echo -e "   ℹ️ Plist file not found. Service likely not running."
fi

# ----------------------------------------
# 2. Diệt Process 
# ----------------------------------------
echo -e "${YELLOW}[2/5] Killing Running Processes...${NC}"
pkill -f "$APP_NAME" 2>/dev/null
pkill -f "dlp_agent_mac.py" 2>/dev/null
sleep 0.5

# Kiểm tra lần cuối, diệt bằng kill -9 để đảm bảo các tiến trình lỗi bị xóa
if pgrep -f "$APP_NAME" > /dev/null; then
     pkill -9 -f "$APP_NAME"
     echo -e "   ❌ Forced kill needed. Processes stopped."
else
     echo -e "   ✅ Processes stopped."
fi


# ----------------------------------------
# 3. Xóa File Tạm 
# ----------------------------------------
echo -e "${YELLOW}[3/5] Cleaning Temporary Files...${NC}"
FILES_TO_CLEAN=("$LOCK_FILE" "$LOG_OUT" "$LOG_ERR")
for file in "${FILES_TO_CLEAN[@]}"; do
    if [ -f "$file" ]; then
        rm "$file"
        echo -e "   ✅ Deleted: $file"
    fi
done

# ----------------------------------------
# 4. Gỡ bỏ ứng dụng
# ----------------------------------------
echo -e "${YELLOW}[4/5] Removing Application...${NC}"

POSSIBLE_PATHS=(
    "$(pwd)/$APP_NAME"
    "$(pwd)/$APP_BUNDLE"
    "$(pwd)/dist/$APP_BUNDLE"
    "/Applications/$APP_BUNDLE"
    "$HOME/Applications/$APP_BUNDLE"
)

FOUND=false
for path in "${POSSIBLE_PATHS[@]}"; do
    if [ -e "$path" ]; then
        echo -e "   Found: $path"
        
        if [[ "$path" == *"/Applications/"* ]]; then
            echo -e "   🔒 System folder detected. Password required."
            sudo rm -rf "$path"
            
            if [ $? -eq 0 ]; then
                echo -e "   ✅ Removed (Admin mode)."
            else
                echo -e "   ❌ Failed. Password might be incorrect or file locked."
                exit 1
            fi
        else
            rm -rf "$path" 2>/dev/null
            if [ $? -ne 0 ]; then
                 echo -e "   🔒 Permission denied. Retrying with sudo..."
                 sudo rm -rf "$path"
                 echo -e "   ✅ Removed (Admin mode)."
            else
                 echo -e "   ✅ Removed (User mode)."
            fi
        fi
        FOUND=true
    fi
done

if [ "$FOUND" = false ]; then
    echo -e "   ℹ️ App files not found (Clean)."
fi

# ----------------------------------------
# 5. Khởi động lại Dock (Khắc phục lỗi Icon dư thừa)
# ----------------------------------------
echo -e "${YELLOW}[5/5] Resetting Icon Cache...${NC}"
killall Dock 2>/dev/null
echo -e "   ✅ Dock restarted."


# ----------------------------------------
# 6. Hoàn tất
# ----------------------------------------
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}      UNINSTALLATION COMPLETE           ${NC}"
echo -e "${GREEN}========================================${NC}"