#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Script để tạo lại git email helper và cập nhật hook"""
import sys
import os

# Thêm thư mục hiện tại vào path để import dlp_agent_mac
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dlp_agent_mac import create_git_email_helper, setup_git_firewall

if __name__ == "__main__":
    print("🔧 Đang tạo lại git email helper và cập nhật hook...")
    try:
        helper_script = create_git_email_helper()
        if helper_script:
            print(f"✅ Đã tạo script helper: {helper_script}")
        else:
            print("❌ Không thể tạo script helper")
            sys.exit(1)
        
        setup_git_firewall()
        print("✅ Đã cập nhật git firewall hook")
        print("\n📝 Bây giờ bạn có thể test git push lại!")
    except Exception as e:
        print(f"❌ Lỗi: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

