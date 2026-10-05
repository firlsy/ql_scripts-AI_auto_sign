#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度库库AI（kuku.baidu.com）自动签到领积分 —— 青龙面板版
================================================================
功能：
  1. 每日登录签到（+50 积分）
  2. 自动领取「完成一次对话」奖励（+50 积分）
  3. 尝试领取「首次下载登录桌面端」+500 积分（以电脑客户端身份上报，尽力而为）
  4. 扫描任务面板，自动领取所有可领（claimable_point > 0）的任务奖励
  5. 上报 taskscore 体验任务
  6. 查询当前积分余额
  7. 支持多账号、支持青龙内置通知（notify.py）

支持两种模式（自动识别，也可用环境变量 KUKU_MODE 强制指定）：
  - web 模式（推荐）：使用浏览器抓到的 kuku.baidu.com 完整 Cookie
  - client 模式：使用 BDUSS + STOKEN（模拟库库电脑客户端 v1.6.7）

环境变量：
  KUKU_COOKIE         web 模式 Cookie，多账号用换行或 & 分隔
  KUKU_CLIENT_COOKIE  client 模式 Cookie（格式: BDUSS=xxx; STOKEN=yyy），多账号用换行或 & 分隔
  KUKU_MODE           可选: web / client（默认自动识别）
  KUKU_PROXY          可选: 代理地址，如 http://127.0.0.1:7890
  KUKU_NOTIFY         可选: 填 false 关闭青龙通知，默认开启

青龙面板部署：
  1. 脚本管理 -> 新建文件 kuku_checkin.py，粘贴本脚本
  2. 依赖管理 -> Python3 -> 安装 requests（可选安装 curl_cffi 以模拟浏览器 TLS 指纹，成功率更高）
  3. 环境变量 -> 新建 KUKU_COOKIE，值为浏览器抓包的 Cookie
  4. 定时任务 -> 新建任务，命令: task kuku_checkin.py，定时: 30 8 * * *
  5. 手动运行一次看日志确认成功

获取 Cookie 方法（web 模式）：
  1. Chrome 打开 https://kuku.baidu.com 并登录百度账号
  2. F12 打开开发者工具 -> Network（网络）-> 刷新页面
  3. 随便点一个发往 kuku.baidu.com 的请求 -> Headers（标头）-> Request Headers
  4. 复制整行 Cookie: 后面的内容（很长，包含 BDUSS 等字段）
  5. 粘贴到青龙环境变量 KUKU_COOKIE

仅供个人学习交流，请勿滥用，遵守库库服务条款。
"""

import os
import re
import sys
import json
import time
import random
import traceback
from urllib.parse import urlencode

# ---------- HTTP 库：优先 curl_cffi（Chrome TLS 指纹），回退 requests ----------
HAS_CFFI = False
try:
    from curl_cffi import requests as _cffi_requests
    HAS_CFFI = True
except ImportError:
    try:
        import requests as _requests
    except ImportError:
        print("缺少依赖：请在青龙「依赖管理 -> Python3」安装 requests（推荐同时安装 curl_cffi）")
        sys.exit(1)

TARGET = "https://kuku.baidu.com"
APP_ID = "123971023"

# web 模式参数（来自 kuku2api 项目实测）
WEB_QUERY = "clienttype=400&app_id=%s&web=1&channel=chunlei&version=1.4.4" % APP_ID
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

# client 模式参数（来自库库电脑客户端 v1.6.7 实测）
CLIENT_QUERY = "app_id=%s&channel=chunlei&clienttype=401&version=1.6.7" % APP_ID
CLIENT_UA = "genflow;1.6.7;PC;PC-Windows;10.0.19045;GenFlowPro"

TASK_NAMES = {
    "LOGIN": "每日登录",
    "CHAT": "完成一次对话",
    "SELF_DOWNLOAD": "首次下载登录桌面端(送500)",
    "INVITE_VISIT": "邀请新用户登录网页版",
    "INVITE_DOWNLOAD": "邀请新用户登录电脑版",
}


def client_cookie_from_raw(raw):
    """从任意 Cookie 串中提取 BDUSS/STOKEN，拼出客户端最小三件套。"""
    parts = {}
    for seg in raw.split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip()] = v.strip()
    if not parts.get("BDUSS"):
        return None
    return "BDUSS=%s; STOKEN=%s; gfprotpl=genflowpro" % (
        parts["BDUSS"], parts.get("STOKEN", ""))


def try_desktop_bonus(h):
    """尽力而为地领取两项一次性奖励：
    1. 首次下载登录桌面端（SELF_DOWNLOAD，+500 积分）
       —— 以电脑客户端身份(clienttype=401)上报；若服务端要求真实的
          桌面端登录事件则会拒绝，此时需手动安装登录一次客户端，
          之后由任务面板补领（rewardClaim）自动拿到 500 积分。
    2. taskscore 体验任务（/activity/taskcomplete，errno=-8 也算成功）
    全部幂等，失败不影响主签到流程。
    """
    results = []
    # 1. 桌面端首登奖励
    try:
        r = h.request(
            "POST",
            "%s/api/genflowpro/freepoint/taskComplete?%s" % (TARGET, CLIENT_QUERY),
            data=urlencode({"task_type": "SELF_DOWNLOAD", "auto_claim": "true"}),
            extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.get("errno") == 0:
            dd = r.get("data", {}) or {}
            status = str(dd.get("complete_status", "")).upper()
            point = int(dd.get("reward_point", 0) or 0)
            if status == "SUCCESS" and point > 0:
                results.append((TASK_NAMES["SELF_DOWNLOAD"], "领取成功", point))
            else:
                results.append((TASK_NAMES["SELF_DOWNLOAD"],
                                "已领过或状态: %s" % (status or dd), 0))
        else:
            results.append((TASK_NAMES["SELF_DOWNLOAD"],
                            "服务端未认可(通常需真实桌面端登录一次): %s"
                            % (r.get("show_msg") or r.get("errmsg")), 0))
        time.sleep(random.uniform(1.5, 3.0))
    except Exception as e:
        results.append((TASK_NAMES["SELF_DOWNLOAD"], "异常: %s" % e, 0))

    # 2. taskscore 体系体验任务（errno=-8 是「领取成功」的历史遗留语义）
    try:
        r = h.request(
            "POST",
            "%s/api/genflowpro/activity/taskcomplete?%s" % (TARGET, CLIENT_QUERY),
            data=urlencode({"task_from": "activity_kuku"}),
            extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.get("errno") in (0, -8):
            results.append(("体验任务(taskscore)", "上报成功", 0))
        else:
            results.append(("体验任务(taskscore)",
                            "跳过: %s" % (r.get("show_msg") or r.get("errmsg")), 0))
        time.sleep(random.uniform(1.0, 2.0))
    except Exception as e:
        results.append(("体验任务(taskscore)", "异常: %s" % e, 0))
    return results


def log(msg):
    print(msg, flush=True)


class Http:
    """统一请求封装，自动带 Cookie / UA / Referer / Origin。"""

    def __init__(self, cookie, ua, proxy=None):
        self.cookie = cookie
        self.ua = ua
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def request(self, method, url, data=None, extra_headers=None):
        headers = {
            "Cookie": self.cookie,
            "Referer": TARGET + "/",
            "Origin": TARGET,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": self.ua,
        }
        if extra_headers:
            headers.update(extra_headers)
        if HAS_CFFI:
            # curl_cffi 模拟 Chrome 指纹，绕过 TLS 指纹检测
            r = _cffi_requests.request(
                method, url, headers=headers, data=data,
                timeout=30, proxies=self.proxies, impersonate="chrome",
            )
        else:
            r = _requests.request(
                method, url, headers=headers, data=data,
                timeout=30, proxies=self.proxies,
            )
        return r.json()


def judge_task_result(task_type, dd, panel_map):
    """判定 taskComplete 的真实结果与积分。

    实测坑点：服务端返回 complete_status=SUCCESS 时 reward_point 经常为 0
    （积分实际已到账但响应里不带数额）。此时用任务面板里的 single_reward_point
    兜底——仅当领取前面板任务状态不是 FINISHED（即本次为首次完成）时才计入。
    """
    name = TASK_NAMES.get(task_type, task_type)
    status = str(dd.get("complete_status", "")).upper()
    point = int(dd.get("reward_point", 0) or 0)
    pre = panel_map.get(task_type) or {}
    pre_status = str(pre.get("task_status", "")).upper()
    if status == "SUCCESS":
        if point <= 0 and pre_status != "FINISHED":
            point = int(pre.get("single_reward_point", 0) or 0)
        if point > 0:
            return (name, "领取成功", point)
        return (name, "已领过" if pre_status == "FINISHED" else "成功", 0)
    if status in ("BADGE_ALREADY_RECEIVED", "FINISHED", "DONE"):
        return (name, "今日已领过", 0)
    return (name, "状态: %s" % (status or dd), point)


# ---------------------------------------------------------------- web 模式

def web_run(cookie, proxy=None):
    """网页版流程：userreport 换 token -> homenew 看面板 -> taskComplete 领奖。"""
    h = Http(cookie, WEB_UA, proxy)
    result = {"tasks": [], "gained": 0, "points": None, "ok": False}

    # 1. 换取 bdstoken / uinfo / uk
    r = h.request("GET", "%s/api/genflowpro/common/userreport?%s" % (TARGET, WEB_QUERY))
    if r.get("errno") != 0:
        raise RuntimeError("Cookie 已失效或请求被拒（userreport errno=%s %s）"
                           % (r.get("errno"), r.get("show_msg") or r.get("errmsg")))
    d = r["data"]
    token_query = WEB_QUERY + "&bdstoken=%s&uinfo=%s&uk=%s" % (
        d.get("bdstoken", ""), d.get("uinfo", ""), d.get("uk", ""))
    result["ok"] = True

    # 2. 任务面板
    panel_tasks = []
    panel_map = {}  # task_type -> 任务详情（用于积分兜底）
    try:
        r = h.request("GET", "%s/api/genflowpro/freepoint/homenew?%s" % (TARGET, token_query))
        if r.get("errno") == 0:
            for act in r.get("data", {}).get("activities", []):
                for tab in act.get("tabs", []):
                    for tk in tab.get("tasks", []):
                        panel_tasks.append(tk)
                        if tk.get("task_type"):
                            panel_map[tk["task_type"]] = tk
    except Exception as e:
        log("    任务面板获取失败（不影响签到）: %s" % e)

    # 3. 上报 LOGIN / CHAT 并领取
    for task_type, auto_claim in (("LOGIN", False), ("CHAT", True)):
        try:
            form = {"task_type": task_type}
            if auto_claim:
                form["auto_claim"] = "true"
            r = h.request(
                "POST",
                "%s/api/genflowpro/freepoint/taskComplete?%s" % (TARGET, token_query),
                data=urlencode(form),
                extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.get("errno") != 0:
                result["tasks"].append((TASK_NAMES.get(task_type, task_type),
                                        "失败: %s" % (r.get("show_msg") or r.get("errmsg")), 0))
                continue
            name, status_text, point = judge_task_result(task_type, r.get("data", {}) or {}, panel_map)
            result["gained"] += point
            result["tasks"].append((name, status_text, point))
            time.sleep(random.uniform(1.5, 3.5))
        except Exception as e:
            result["tasks"].append((TASK_NAMES.get(task_type, task_type), "异常: %s" % e, 0))

    # 4. 扫描面板，领取所有可领奖励（幂等，已领过会被服务端拒绝，无副作用）
    for tk in panel_tasks:
        try:
            claimable = int(tk.get("claimable_point", 0) or 0)
            task_key = tk.get("task_key")
            if claimable <= 0 or not task_key:
                continue
            form = {
                "activity_key": tk.get("activity_key") or "genflow_free_points",
                "period_id": tk.get("period_no") or tk.get("period_id") or 4,
                "task_key": task_key,
            }
            r = h.request(
                "POST",
                "%s/api/genflowpro/freepoint/rewardClaim?%s" % (TARGET, token_query),
                data=urlencode(form),
                extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            dd = r.get("data", {}) or {}
            point = int(dd.get("claimed_point", 0) or 0)
            if r.get("errno") == 0 and point > 0:
                result["gained"] += point
                result["tasks"].append((tk.get("task_name") or task_key, "面板补领成功", point))
            time.sleep(random.uniform(1.0, 2.5))
        except Exception:
            pass

    # 5. 尝试「首次下载登录桌面端 +500」与 taskscore 体验任务
    #    （走电脑客户端通道 clienttype=401，尽力而为，失败不影响主流程）
    ccookie = client_cookie_from_raw(cookie)
    if ccookie:
        for name, status_text, point in try_desktop_bonus(Http(ccookie, CLIENT_UA, proxy)):
            result["tasks"].append((name, status_text, point))
            result["gained"] += point

    # 6. 查询积分余额（必须放在所有领取动作之后，否则读到的是到账前的旧余额；
    #    best effort，该接口不能带 app_id）
    try:
        q = "&".join(p for p in token_query.split("&") if not p.startswith("app_id="))
        r = h.request("GET", "%s/bizapi/gfpro/getgfvipremain?%s" % (TARGET, q))
        for item in (r.get("data", {}) or {}).get("list", []) or []:
            if item.get("assetName") == "token":
                result["points"] = item.get("totalPoint")
    except Exception:
        pass

    return result


# ---------------------------------------------------------------- client 模式

def client_run(raw_cookie, proxy=None):
    """电脑客户端流程：BDUSS+STOKEN，无需换 token，直接调 freepoint 接口。"""
    cookie = client_cookie_from_raw(raw_cookie)
    if not cookie:
        raise RuntimeError("client 模式 Cookie 中未找到 BDUSS")

    h = Http(cookie, CLIENT_UA, proxy)
    result = {"tasks": [], "gained": 0, "points": None, "ok": False}

    # 1. 校验登录态
    r = h.request("GET", "%s/api/genflowpro/settings/profile?%s" % (TARGET, CLIENT_QUERY))
    if r.get("errno") != 0:
        raise RuntimeError("BDUSS/STOKEN 已失效（profile errno=%s）" % r.get("errno"))
    result["ok"] = True
    uinfo = r.get("data", {}) or {}
    log("    账号: %s" % (uinfo.get("nickname") or uinfo.get("uname") or "未知"))

    # 2. 任务面板
    panel = {}
    activities = []
    panel_map = {}  # task_type -> 任务详情（用于积分兜底）
    try:
        r = h.request("GET", "%s/api/genflowpro/freepoint/homenew?%s" % (TARGET, CLIENT_QUERY))
        # errno 151004 / -6 = 无活动，视为空面板
        if r.get("errno") == 0:
            panel = r.get("data", {}) or {}
            activities = panel.get("activities", [])
            for act in activities:
                for tab in act.get("tabs", []):
                    for tk in tab.get("tasks", []):
                        if tk.get("task_type"):
                            panel_map[tk["task_type"]] = tk
    except Exception as e:
        log("    任务面板获取失败（不影响签到）: %s" % e)

    # 3. 上报 LOGIN / CHAT
    for task_type, auto_claim in (("LOGIN", False), ("CHAT", True)):
        try:
            form = {"task_type": task_type}
            if auto_claim:
                form["auto_claim"] = "true"
            r = h.request(
                "POST",
                "%s/api/genflowpro/freepoint/taskComplete?%s" % (TARGET, CLIENT_QUERY),
                data=urlencode(form),
                extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.get("errno") != 0:
                result["tasks"].append((TASK_NAMES.get(task_type, task_type),
                                        "失败: %s" % (r.get("show_msg") or r.get("errmsg")), 0))
                continue
            name, status_text, point = judge_task_result(task_type, r.get("data", {}) or {}, panel_map)
            result["gained"] += point
            result["tasks"].append((name, status_text, point))
            time.sleep(random.uniform(1.5, 3.5))
        except Exception as e:
            result["tasks"].append((TASK_NAMES.get(task_type, task_type), "异常: %s" % e, 0))

    # 3.5 首次下载登录桌面端(+500) 与 taskscore 体验任务（尽力而为）
    for name, status_text, point in try_desktop_bonus(h):
        result["tasks"].append((name, status_text, point))
        result["gained"] += point

    # 4. rewardClaim 逐项补领
    for act in activities:
        akey = act.get("activity_key") or "genflow_free_points"
        pid = act.get("period_no") or act.get("period_id") or 4
        for tab in act.get("tabs", []):
            for tk in tab.get("tasks", []):
                try:
                    claimable = int(tk.get("claimable_point", 0) or 0)
                    tkey = tk.get("task_key")
                    if claimable <= 0 or not tkey:
                        continue
                    form = {"activity_key": akey, "period_id": pid, "task_key": tkey}
                    r = h.request(
                        "POST",
                        "%s/api/genflowpro/freepoint/rewardClaim?%s" % (TARGET, CLIENT_QUERY),
                        data=urlencode(form),
                        extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    dd = r.get("data", {}) or {}
                    point = int(dd.get("claimed_point", 0) or 0)
                    if r.get("errno") == 0 and point > 0:
                        result["gained"] += point
                        result["tasks"].append((tk.get("task_name") or tkey, "面板补领成功", point))
                    time.sleep(random.uniform(1.0, 2.5))
                except Exception:
                    pass

    # 5. 查询积分余额（不能带 app_id）
    try:
        q = "&".join(p for p in CLIENT_QUERY.split("&") if not p.startswith("app_id="))
        r = h.request("GET", "%s/bizapi/gfpro/getgfvipremain?%s" % (TARGET, q))
        for item in (r.get("data", {}) or {}).get("list", []) or []:
            if item.get("assetName") == "token":
                result["points"] = item.get("totalPoint")
    except Exception:
        pass

    return result


# ---------------------------------------------------------------- 主流程

def split_accounts(raw):
    raw = raw.strip().strip('"').strip("'")
    accounts = [a.strip() for a in re.split(r"[\n]+", raw) if a.strip()]
    if len(accounts) <= 1 and "&" in raw:
        accounts = [a.strip() for a in raw.split("&") if a.strip()]
    return accounts


def main():
    proxy = os.environ.get("KUKU_PROXY") or None
    mode = (os.environ.get("KUKU_MODE") or "auto").lower()

    web_cookies = split_accounts(os.environ.get("KUKU_COOKIE", "")) \
        if os.environ.get("KUKU_COOKIE") else []
    client_cookies = split_accounts(os.environ.get("KUKU_CLIENT_COOKIE", "")) \
        if os.environ.get("KUKU_CLIENT_COOKIE") else []

    if mode == "web":
        client_cookies = []
    elif mode == "client":
        web_cookies = []

    if not web_cookies and not client_cookies:
        log("未配置环境变量 KUKU_COOKIE（web 模式）或 KUKU_CLIENT_COOKIE（client 模式）")
        log("获取方法见脚本头部注释：浏览器登录 kuku.baidu.com 后 F12 抓 Cookie")
        sys.exit(1)

    log("========== 百度库库AI 自动签到开始 ==========")
    log("运行环境: %s | 账号数: web=%d, client=%d" % (
        "curl_cffi(Chrome指纹)" if HAS_CFFI else "requests", len(web_cookies), len(client_cookies)))

    summary_lines = []
    all_ok = True
    idx = 0

    def run_account(label, cookie):
        nonlocal idx, all_ok
        idx += 1
        log("\n----- 账号 %d (%s) -----" % (idx, label))
        try:
            if label == "web":
                res = web_run(cookie, proxy)
            else:
                res = client_run(cookie, proxy)
        except Exception as e:
            log("    签到失败: %s" % e)
            if os.environ.get("KUKU_DEBUG"):
                traceback.print_exc()
            summary_lines.append("账号%d(%s): 失败 - %s" % (idx, label, e))
            all_ok = False
            return
        line = "账号%d: " % idx
        for name, status, point in res["tasks"]:
            log("    [%s] %s%s" % (name, status, (" +%s积分" % point) if point else ""))
        line += "%s" % ("、".join("%s%s" % (n, ("+%s" % p) if p else "")
                                  for n, _, p in res["tasks"]) or "无任务结果")
        line += " | 本次获得: %s 积分" % res["gained"]
        if res["points"] is not None:
            line += " | 当前余额: %s 积分" % res["points"]
        log("    " + line)
        summary_lines.append(line)

    for c in web_cookies:
        run_account("web", c)
        if idx < len(web_cookies) + len(client_cookies):
            time.sleep(random.uniform(5, 12))

    for c in client_cookies:
        run_account("client", c)
        if idx < len(web_cookies) + len(client_cookies):
            time.sleep(random.uniform(5, 12))

    log("\n========== 签到结束 ==========")
    summary = "百度库库AI签到\n" + "\n".join(summary_lines)

    # 青龙内置通知
    if (os.environ.get("KUKU_NOTIFY", "true").lower() != "false"):
        try:
            from notify import send
            send("百度库库AI签到", summary)
            log("已通过青龙通知推送结果")
        except Exception:
            log("未启用通知或 notify.py 不存在（可在青龙配置文件中配置推送渠道）")

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
