#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy Token 导出器（配合青龙面板使用）
================================================================
复用 signin_88lin.py 的凭据解密机制（新版加密凭据由本机 WorkBuddy
客户端运行时在子进程中解密，仅走内存管道），把解密后的 Bearer Token
保存到本文件同目录 workbuddy_token.txt。

用法：
  python export_token.py

然后把 workbuddy_token.txt 的内容粘贴到青龙环境变量 WB_ACCESS_TOKEN，
配合 workbuddy_checkin.py 定时签到。

注意：
  - Token 约 90 天有效，过期后（青龙日志报 401）重跑本脚本刷新
  - Token 是敏感凭证，勿泄露
  - 若本机定时跑 signin.py（推荐，免维护），则无需导出到青龙
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signin as s  # noqa: E402

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workbuddy_token.txt")


def main():
    try:
        found = s.find_auth_file()
        auth_file = found[0] if isinstance(found, tuple) else found
        print("凭据文件: %s" % auth_file)
        session = s.load_session_retry(auth_file)
        resolved = s.resolve_session(session)
        token = resolved["auth"]["accessToken"]
        uid = (session.get("account") or {}).get("uid", "?")
    except Exception as e:
        print("❌ 获取 Token 失败: %s" % e)
        print("   请先确认 WorkBuddy 桌面端已安装并登录过")
        sys.exit(1)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(token)

    print("✅ Token 已解密并保存: %s" % OUT_FILE)
    print("   用户ID: %s" % uid)
    print("   Token: %s" % token)
    print("")
    print("👉 粘贴到青龙环境变量 WB_ACCESS_TOKEN，配合 workbuddy_checkin.py 使用")
    print("   （约 90 天过期；也可直接在本机定时跑 signin.py，免维护）")


if __name__ == "__main__":
    main()
