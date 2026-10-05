#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星辰超级智能体（TeleAgent / agent.teleai.com.cn）每日积分签到 · 青龙面板脚本
====================================================================

⚠️ 重要说明：
    该平台的"积分福利社-每日领取 100 积分"接口位于登录态之后，需要先在
    浏览器 / 客户端抓到实际的领取请求后，将路径填入下方 CLAIM_PATHS
    （或环境变量 TELEAGENT_CLAIM_PATHS）才能完成签到。
    本脚本已内置：
      1) 令牌有效性校验 + 积分余额查询（/user/resource/summary）
      2) 活动横幅配置拉取（/notifications/campaigns，便于排查活动接口）
      3) 按候选列表逐个尝试领取接口

青龙面板使用步骤
--------------------------------------------------------------------
1. 环境变量 → 新建：
   名称：TELEAGENT_TOKEN
   值：网页版登录后的 console_token（见 README「令牌获取」）
       多账号用换行分隔

2. （可选）如果抓包确认了领取接口，可新建环境变量覆盖默认列表：
   名称：TELEAGENT_CLAIM_PATHS
   值：逗号分隔的接口路径，例如
       /console/api/points/daily/claim,/superCowork/sapi/v1/points/sign

3. 定时任务 → 新建命令：
   task teleagent_checkin.py
   定时规则（示例，每天 08:30）：
   30 8 * * *

4. 依赖：脚本仅用 Python 标准库，无需安装第三方包。
   如需青龙消息推送，请使用 ql 仓库自带的 notify.py（自动检测）。
"""

import json
import os
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
API_BASE = os.getenv("TELEAGENT_API_BASE", "https://agent.teleai.com.cn").rstrip("/")

# 多账号：换行分隔；也兼容 & 分隔
RAW_TOKENS = os.getenv("TELEAGENT_TOKEN", "") or ""
TOKENS = [t.strip() for t in RAW_TOKENS.replace("&", "\n").split("\n") if t.strip()]

# 候选领取接口：按顺序尝试，直到某一个返回业务成功
DEFAULT_CANDIDATES = [
    "/console/api/points/daily/claim",
    "/console/api/points/sign",
    "/console/api/user/points/claim",
    "/console/api/welfare/daily/claim",
    "/superCowork/sapi/v1/points/daily/claim",
    "/superCowork/sapi/v1/points/sign",
]
CLAIM_PATHS = [
    p.strip()
    for p in os.getenv("TELEAGENT_CLAIM_PATHS", ",".join(DEFAULT_CANDIDATES)).split(",")
    if p.strip()
]

TIMEOUT = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ---------------------------------------------------------------------------
# HTTP 工具
# ---------------------------------------------------------------------------


def request(method, url, token, body=None, extra_headers=None):
    """发送请求，返回 (http_status, parsed_json_or_text)。"""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://agent.teleai.com.cn",
        "Referer": "https://agent.teleai.com.cn/",
        "Authorization": f"Bearer {token}",
        # 平台登录态下部分请求会带 Auth-Token，一并附上以最大程度兼容
        "Auth-Token": f"Bearer {token}",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:  # 网络异常
        return None, f"网络异常: {e}"

    try:
        return status, json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return status, text[:300]


# ---------------------------------------------------------------------------
# 业务步骤
# ---------------------------------------------------------------------------


def token_ok(token):
    """校验令牌：访问用户资源摘要接口，未登录时网关返回 code=4001。"""
    status, data = request("GET", f"{API_BASE}/console/api/user/resource/summary", token)
    if isinstance(data, dict):
        code = str(data.get("code", ""))
        if code == "4001":
            return False, data
        return True, data
    # 非 JSON（如返回了 HTML）也视为未通过
    return status == 200 and isinstance(data, str) is False, data


def fetch_campaigns(token):
    """拉取活动/运营位配置，便于确认福利社活动接口地址。"""
    status, data = request("POST", f"{API_BASE}/console/api/notifications/campaigns",
                           token, body={})
    return status, data


def try_claim(token, path):
    """尝试调用领取接口。返回 (成功与否, 说明)。"""
    status, data = request("POST", f"{API_BASE}{path}", token, body={})
    summary = json.dumps(data, ensure_ascii=False)[:300] if isinstance(data, dict) else str(data)[:300]

    if isinstance(data, dict):
        code = str(data.get("code", ""))
        if code == "4001":
            return False, f"令牌失效(4001): {summary}"
        # 常见成功形态：code==200/0/"0"、success==True
        if code in ("200", "0") or data.get("success") is True:
            return True, summary
        # 404/4005 等视为接口不存在，继续下一个候选
        return False, f"HTTP {status} {summary}"
    return False, f"HTTP {status} {summary}"


# ---------------------------------------------------------------------------
# 消息推送（青龙 notify.py）
# ---------------------------------------------------------------------------


def notify(title, content):
    try:
        sys.path.append(os.getcwd())
        from notify import send  # type: ignore
        send(title, content)
    except Exception:
        pass  # 无 notify 环境时静默跳过


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_account(index, token):
    lines = [f"账号 #{index + 1}"]

    ok, info = token_ok(token)
    if not ok:
        lines.append(f"  ❌ 令牌无效或已过期：{json.dumps(info, ensure_ascii=False)[:200]}")
        return "\n".join(lines), False

    lines.append("  ✅ 令牌有效")
    if isinstance(info, dict) and info.get("data"):
        lines.append(f"  📊 资源概览：{json.dumps(info['data'], ensure_ascii=False)[:300]}")

    # 先看活动配置（便于排查真实领取接口）
    c_status, c_data = fetch_campaigns(token)
    if isinstance(c_data, dict) and str(c_data.get("code")) != "4001":
        lines.append(f"  📣 活动配置 HTTP {c_status}：{json.dumps(c_data, ensure_ascii=False)[:300]}")

    # 尝试领取
    success = False
    for path in CLAIM_PATHS:
        ok, msg = try_claim(token, path)
        lines.append(f"  🎯 {path} → {'✅' if ok else '⏭'} {msg}")
        if ok:
            success = True
            break

    if not success:
        lines.append("  ⚠️ 所有候选接口均未领取成功。")
        lines.append("      请按 README「抓包定位领取接口」一节，把真实接口路径填入")
        lines.append("      环境变量 TELEAGENT_CLAIM_PATHS 后重试。")

    # 领取后再次查询余额做对比
    ok2, info2 = token_ok(token)
    if ok2 and isinstance(info2, dict) and info2.get("data"):
        lines.append(f"  📊 最新概览：{json.dumps(info2['data'], ensure_ascii=False)[:300]}")

    return "\n".join(lines), success


def main():
    if not TOKENS:
        print("❌ 未配置环境变量 TELEAGENT_TOKEN（网页版 console_token，多账号换行分隔）")
        sys.exit(1)

    all_lines = []
    any_success = True
    for i, token in enumerate(TOKENS):
        text, success = run_account(i, token)
        print(text)
        all_lines.append(text)
        if not success:
            any_success = False

    title = "星辰超级智能体签到" + ("成功" if any_success else "存在问题")
    notify(title, "\n\n".join(all_lines))


if __name__ == "__main__":
    main()
