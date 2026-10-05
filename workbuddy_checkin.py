#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy（workbuddy.cn）每日自动签到 —— 青龙面板版
================================================================
功能：
  1. 每日签到领积分（Buddy 加油站 / 每日签到，幂等：已签到自动跳过）
  2. 可选领取「今日礼包」（WB_CLAIM_GIFT=1 时开启）
  3. 成长中心全套（WB_GROWTH=1 默认开启，逻辑移植自 88lin/workbuddy-auto-signin）：
     Buddy 旅行领礼物/派出发、任务接单+领奖、断登补登卡、
     连登兑换（7/14/28 天）、开盲盒抽奖、能量开 Buddy 盲盒
  4. 支持多账号（一行变量多个 Token）
  5. 自动解析 Token 有效期，快过期时日志提醒
  6. 支持青龙内置通知（notify.py 优先，DD_BOT 钉钉直发兜底）

接口说明（官方接口，仅发送你自己的 Bearer Token）：
  POST https://copilot.tencent.com/v2/billing/meter/checkin-activity-status  查询签到状态
  POST https://copilot.tencent.com/v2/billing/meter/daily-checkin            执行签到
  POST https://copilot.tencent.com/v2/billing/meter/claim-gift               今日礼包（可选）
  成长中心：https://copilot.tencent.com/v2/activity/growth/*（旅行/任务/补登/兑换/抽奖/盲盒）
  成长中心逻辑移植自 github.com/88lin/workbuddy-auto-signin（MIT）
  鉴权方式：Authorization: Bearer <Token>，与 Cookie 无关

环境变量：
  WB_ACCESS_TOKEN    单账号 Bearer Token（与 WB_ACCESS_TOKENS 二选一）
  WB_ACCESS_TOKENS   多账号，逗号/换行分隔；支持 uid:token 或纯 token
  WB_COOKIE          Cookie 会话模式（网页版，配合 get_token.py 的 workbuddy_cookies.txt）
                     多账号用换行分隔；与 Token 模式可同时配置
  WB_CLAIM_GIFT      可选: 1 开启「今日礼包」领取（默认 0 关闭）
  WB_GROWTH          可选: 0 关闭成长中心（默认 1 开启；Cookie 模式不支持，自动跳过）
  WB_PROXY           可选: 代理地址，如 http://127.0.0.1:7890
  WB_NOTIFY          可选: 填 false 关闭青龙通知，默认开启
                     通知优先走青龙 notify.py；不可用时自动用 DD_BOT_TOKEN/
                     DD_BOT_SECRET（环境变量或 /ql/data/config/config.sh）直发钉钉
  WB_WARN_DAYS       可选: Token 剩余多少天时提醒，默认 7

获取 Token 方法（三选一）：
  方法A（推荐，桌面客户端本地文件）：
    打开本机登录态文件，取 auth.accessToken 字段整串值（通常以 eyJ 开头）：
      Windows: %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
               （或同目录 workbuddy-desktop-ai.info）
      macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
      Linux:   ~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
  方法B（浏览器抓包）：
    Chrome 登录 https://www.workbuddy.cn/app -> F12 -> Network ->
    找任意请求的 Authorization: Bearer xxx，复制 xxx 部分
  方法C（客户端日志）：
    WorkBuddy 运行日志中会以明文打印登录 JWT，搜索 "eyJ" 提取

Token 有效期：通常约 90 天，过期后重新获取并更新环境变量。

青龙面板部署：
  1. 脚本管理 -> 新建文件 workbuddy_checkin.py，粘贴本脚本
  2. 依赖管理 -> Python3 -> 安装 requests（没有也能跑，自动回退标准库 urllib）
  3. 环境变量 -> 新建 WB_ACCESS_TOKEN（或 WB_ACCESS_TOKENS 多账号）
  4. 定时任务 -> 新建任务，命令: task workbuddy_checkin.py
     定时建议: 30 8 * * *（每天 8:30，也可 8/11/13/15/18 点多次补签）
  5. 手动运行一次看日志确认成功

仅供个人学习交流，请勿滥用，遵守 WorkBuddy 服务条款。
"""

import os
import re
import sys
import json
import time
import base64
import math
import random
import hashlib
import hmac
import uuid
import traceback
from datetime import datetime, timezone, date
from urllib.parse import quote_plus

# ---------- HTTP 库：优先 requests，回退标准库 urllib ----------
try:
    import requests as _requests
    HAVE_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAVE_REQUESTS = False

API_BASE = "https://copilot.tencent.com/v2/billing/meter"
STATUS_EP = API_BASE + "/checkin-activity-status"
CHECKIN_EP = API_BASE + "/daily-checkin"
GIFT_EP = API_BASE + "/claim-gift"

# Cookie 模式（网页版同源接口，配合 get_token.py 抓到的 workbuddy_cookies.txt）
WEB_BASE = "https://www.workbuddy.cn/billing/meter"
WEB_STATUS_EP = WEB_BASE + "/checkin-activity-status"
WEB_CHECKIN_EP = WEB_BASE + "/daily-checkin"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- JWT 解析

def jwt_payload(token):
    """无需密钥，仅 base64url 解码 JWT payload。失败返回空 dict。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
    except Exception:
        return {}


def jwt_sub(token):
    return jwt_payload(token).get("sub") or ""


def jwt_expire_days(token):
    """返回 Token 剩余有效天数，解析失败返回 None。"""
    exp = jwt_payload(token).get("exp")
    if not exp:
        return None
    try:
        remain = float(exp) - datetime.now(timezone.utc).timestamp()
        return remain / 86400.0
    except Exception:
        return None


# ---------------------------------------------------------------- 账号解析

def split_accounts(raw):
    """逗号 / 换行 / & 分隔的多账号字符串 -> [(uid, token), ...]"""
    raw = raw.strip().strip('"').strip("'")
    items = [a.strip() for a in re.split(r"[\n,]+", raw) if a.strip()]
    if len(items) <= 1 and "&" in raw:
        items = [a.strip() for a in raw.split("&") if a.strip()]
    accounts = []
    for item in items:
        # 支持 uid:token 形式（纯 token 以 eyJ 开头，不含冒号）
        if ":" in item and not item.startswith("eyJ"):
            uid, tok = item.split(":", 1)
            accounts.append((uid.strip(), tok.strip()))
        else:
            accounts.append(("", item))
    # 补全 uid
    fixed = []
    for uid, tok in accounts:
        if not uid:
            uid = jwt_sub(tok)
        fixed.append((uid, tok))
    return fixed


# ---------------------------------------------------------------- HTTP

def http(method, url, token, uid, body=None, proxy=None):
    headers = {
        "Accept": "application/json",
        "User-Agent": UA,
        "Origin": "https://www.workbuddy.cn",
        "Referer": "https://www.workbuddy.cn/",
        "Authorization": "Bearer " + token,
    }
    if uid:
        headers["X-User-Id"] = str(uid)
    proxies = {"http": proxy, "https": proxy} if proxy else None

    if HAVE_REQUESTS:
        if method == "POST":
            headers["Content-Type"] = "application/json"
            r = _requests.post(url, headers=headers, data=body or "{}",
                               timeout=30, proxies=proxies)
        else:
            r = _requests.get(url, headers=headers, timeout=30, proxies=proxies)
        return r.status_code, r.text

    # 标准库回退
    if method == "POST":
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=(body or "{}").encode("utf-8"),
                                     headers=headers, method="POST")
    else:
        req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def parse_json(code, text):
    try:
        return json.loads(text)
    except Exception:
        return None


# ---------------------------------------------------------------- 签到逻辑

def checkin_one(idx, uid, token, proxy=None, claim_gift=False):
    """对单个账号执行签到，返回 (成功: bool, 摘要: str)。"""
    tag = ("账号%d" % idx) + ("(%s…)" % uid[:8] if uid else "")

    # Token 有效期检查
    days = jwt_expire_days(token)
    if days is not None:
        if days <= 0:
            log("[%s] ⚠️ Token 已过期，请重新获取！" % tag)
            return False, "%s: Token 已过期" % tag
        warn_days = int(os.environ.get("WB_WARN_DAYS", "7") or 7)
        if days <= warn_days:
            log("[%s] ⚠️ Token 仅剩约 %.1f 天有效期，请尽快更新！" % (tag, days))

    # 1) 查询签到状态（官方接口只接受 POST，GET 会 404）
    code, text = http("POST", STATUS_EP, token, uid, proxy=proxy)
    if code == 401:
        return False, "%s: 登录态失效(401)，请在 WorkBuddy 重新登录后更新 Token" % tag
    st = parse_json(code, text)
    if not st:
        return False, "%s: 状态接口返回异常(%s): %s" % (tag, code, text[:120])
    if st.get("code") != 0:
        return False, "%s: 状态查询失败 code=%s msg=%s" % (
            tag, st.get("code"), st.get("msg", ""))

    data = st.get("data") or {}
    if data.get("today_checked_in"):
        streak = data.get("streak_days", 0)
        log("[%s] 今日已签到，连续 %s 天（跳过）" % (tag, streak))
        summary = "%s: 今日已签到（连续 %s 天）" % (tag, streak)
    else:
        # 2) 执行签到
        code, text = http("POST", CHECKIN_EP, token, uid, proxy=proxy)
        if code == 401:
            return False, "%s: 登录态失效(401)，请在 WorkBuddy 重新登录后更新 Token" % tag
        ck = parse_json(code, text)
        if not ck:
            return False, "%s: 签到接口返回异常(%s): %s" % (tag, code, text[:120])
        c = ck.get("code")
        if c == 0:
            d = ck.get("data") or {}
            credit = d.get("credit", 0)
            streak = d.get("streak_days", 0)
            log("[%s] ✅ 签到成功 +%s 积分（连续 %s 天）" % (tag, credit, streak))
            summary = "%s: 签到成功 +%s 积分（连续 %s 天）" % (tag, credit, streak)
        elif c == 10001 or "已签" in (ck.get("msg") or ""):
            log("[%s] 今日已签到（接口确认）" % tag)
            summary = "%s: 今日已签到" % tag
        else:
            return False, "%s: 签到失败 code=%s msg=%s" % (tag, c, ck.get("msg", ""))

    # 3) 可选：今日礼包
    if claim_gift:
        try:
            code, text = http("POST", GIFT_EP, token, uid, proxy=proxy)
            g = parse_json(code, text)
            if g and g.get("code") == 0:
                gd = g.get("data") or {}
                log("[%s] 🎁 今日礼包领取成功 +%s" % (tag, gd.get("credit", gd)))
                summary += " | 礼包 +%s" % gd.get("credit", "")
            else:
                msg = (g or {}).get("msg", "") if g else "HTTP %s" % code
                log("[%s] 🎁 今日礼包: %s" % (tag, msg or "不可用"))
        except Exception as e:
            log("[%s] 🎁 今日礼包异常（不影响签到）: %s" % (tag, e))

    return True, summary


# ---------------------------------------------------------------- Cookie 模式

def cookie_http(url, cookie, proxy=None):
    """网页版同源接口 POST（Cookie 鉴权），返回 (http_code, dict或None)。"""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": cookie,
        "Origin": "https://www.workbuddy.cn",
        "Referer": "https://www.workbuddy.cn/app",
        "User-Agent": UA,
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if HAVE_REQUESTS:
        r = _requests.post(url, headers=headers, data="{}", timeout=30, proxies=proxies)
        code, text = r.status_code, r.text
    else:
        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code, text = r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            code, text = e.code, e.read().decode("utf-8", "ignore")
    try:
        return code, json.loads(text)
    except Exception:
        return code, None


def cookie_run(idx, cookie, proxy=None):
    """Cookie 会话签到（网页版接口），返回 (成功: bool, 摘要: str)。"""
    tag = "Cookie账号%d" % idx
    code, j = cookie_http(WEB_STATUS_EP, cookie, proxy)
    if code in (401, 403):
        return False, "%s: Cookie 会话失效(HTTP %s)，请重跑 get_token.py 获取" % (tag, code)
    if not j:
        return False, "%s: 状态接口返回异常(HTTP %s)" % (tag, code)
    if j.get("code") != 0 and j.get("success") is not True:
        return False, "%s: 状态查询失败 code=%s msg=%s" % (
            tag, j.get("code"), j.get("msg") or j.get("message", ""))

    data = j.get("data") or {}
    if data.get("today_checked_in"):
        log("[%s] 今日已签到，连续 %s 天（跳过）" % (tag, data.get("streak_days", 0)))
        summary = "%s: 今日已签到（连续 %s 天）" % (tag, data.get("streak_days", 0))
    else:
        code, j = cookie_http(WEB_CHECKIN_EP, cookie, proxy)
        if code in (401, 403):
            return False, "%s: Cookie 会话失效(HTTP %s)" % (tag, code)
        if not j:
            return False, "%s: 签到接口返回异常(HTTP %s)" % (tag, code)
        c = j.get("code")
        msg = j.get("msg") or j.get("message", "") or ""
        if c == 0 or j.get("success") is True:
            d = j.get("data") or {}
            credit = d.get("credit", 0)
            streak = d.get("streak_days", 0)
            log("[%s] ✅ 签到成功 +%s 积分（连续 %s 天）" % (tag, credit, streak))
            summary = "%s: 签到成功 +%s 积分（连续 %s 天）" % (tag, credit, streak)
        elif c == 10001 or "已签" in msg:
            log("[%s] 今日已签到（接口确认）" % tag)
            summary = "%s: 今日已签到" % tag
        else:
            return False, "%s: 签到失败 code=%s msg=%s" % (tag, c, msg)
    return True, summary


# ---------------------------------------------------------------- 成长中心
# 逻辑移植自 88lin/workbuddy-auto-signin 的 run_growth（MIT），
# 接口族：{API_BASE}/v2/activity/growth/*，Bearer 鉴权，与签到同一 Token。

GROWTH_BASE = "https://copilot.tencent.com/v2/activity/growth"
MAKEUP_MAX_PER_RUN = 1   # 每轮最多消耗一张补登卡，剩余留给下一轮

# 连登兑换各档奖励的官方文案（2026-09 活动版本），优先用服务端实发明细
_REDEEM_REWARDS = {
    "7d":  "+2 能量 +1 补登卡 +1 次抽奖",
    "14d": "+50 积分 +3 能量 +1 补登卡 +1 次抽奖",
    "28d": "+150 积分 +5 能量 +1 补登卡 +1 次抽奖",
}
# (档位标识, /redeem/summary 的状态字段前缀, 展示名, 天数)
_REDEEM_TIERS = (
    ("7d",  "starter",   "入门", 7),
    ("14d", "advanced",  "进阶", 14),
    ("28d", "legendary", "巅峰", 28),
)


def dig(obj, key):
    """在可能被 data/result 包裹的响应里找字段，兼容信封结构。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if k in obj and isinstance(obj[k], dict):
                r = dig(obj[k], key)
                if r is not None:
                    return r
    return None


def as_int(v, default=0):
    """把可能为字符串的数字安全地转成 int（OverflowError 一并兜住）。"""
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def fmt_credit(v):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return v


def _first_int(body, key, fallback=0):
    """优先取响应里的实际发放数值，挖不到才用回落值。"""
    if isinstance(body, dict):
        v = dig(body, key)
        if v is not None:
            return as_int(v)
    return as_int(fallback)


def _fmt_eta(arrive_at, server_now):
    """服务端时间戳 -> "还有多久回来"；任一缺失/非法返回空串，绝不让展示信息炸掉整轮。"""
    try:
        left = float(arrive_at) - float(server_now)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(left):
        return ""
    if left <= 0:
        return "，已到达待领取"
    minutes = int(round(left / 60.0))
    if minutes < 60:
        return "，约 %d 分钟后回" % max(1, minutes)
    return "，约 %.1f 小时后回" % (left / 3600.0)


def _client_token(prefix="u"):
    """活动接口（抽奖/兑换/开盲盒）要求的防重放 token，缺了 /lottery/draw 会 400。"""
    return "%s-%s" % (prefix, uuid.uuid4())


def _redeem_reward_desc(body, tier):
    """兑换成功的奖励描述：优先拼服务端实发的 *_granted 字段。"""
    bits = []
    credit = as_int(dig(body, "credit_granted"), 0)
    energy = as_int(dig(body, "energy_granted"), 0)
    cards = as_int(dig(body, "cards_granted"), 0)
    chances = as_int(dig(body, "chances_granted"), 0)
    if credit:
        bits.append("+%s 积分" % fmt_credit(credit))
    if energy:
        bits.append("+%s 能量" % fmt_credit(energy))
    if cards:
        bits.append("+%s 补登卡" % fmt_credit(cards))
    if chances:
        bits.append("+%s 次抽奖" % fmt_credit(chances))
    if bits:
        return "（%s）" % " ".join(bits)
    return "（%s）" % _REDEEM_REWARDS.get(tier, "奖励已到账")


def _is_no_chance(msg):
    """抽奖"没有次数"是常态不是故障（次数来自连登兑换）。"""
    m = str(msg or "").lower()
    if not m:
        return False
    if "insufficient" in m or "not enough" in m:
        return "chance" in m or "balance" in m
    return "no chance" in m


def _is_unknown_tier(code, body):
    """连登兑换 tier 标识不被服务端认识 -> 退回天数写法重试一次（参数校验阶段拒绝，重试不会重复领）。"""
    if code != 400:
        return False
    m = str(dig(body, "msg") or "").lower()
    return "tier" in m and ("unknown" in m or "unsupported" in m or "invalid" in m)


def _is_tier_locked(code, body):
    """未解锁档位：403 + 天数不足——业务常态，不算失败（须先于 401/403 中止判断）。"""
    if code != 403:
        return False
    m = str(dig(body, "msg") or "")
    return "天数不足" in m or "不足" in m


def _api_success(code, body):
    business_code = body.get("code") if isinstance(body, dict) else None
    return 200 <= code < 300 and (
        business_code is None or (type(business_code) is int and business_code == 0))


def _makeup_candidates(streak_body, heatmap_body):
    """从日历选断登日期：当月、今天之前、活动上线后、score==0。

    makeup_dates 是已补登历史，不能当待办；使用 heatmap.today 的服务端日期避免
    本机时区算错；日历结构异常时抛错整段停止，不靠猜测消耗补登卡。
    """
    def parse_date(value):
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError("补登日期格式无效")
        return date.fromisoformat(value)

    today_obj = dig(heatmap_body, "today")
    if not isinstance(today_obj, dict):
        raise ValueError("活跃日历缺少服务端日期")
    today = parse_date(today_obj.get("date"))
    launch = parse_date(dig(streak_body, "launch_date"))
    start = max(today.replace(day=1), launch, date(2026, 6, 17))
    streak = dig(streak_body, "streak")
    if not isinstance(streak, dict):
        raise ValueError("连登状态格式无效")
    made_up = streak.get("makeup_dates", [])
    if not isinstance(made_up, list):
        raise ValueError("已补登记录格式无效")
    made_up = {parse_date(value) for value in made_up}
    cells = dig(heatmap_body, "cells")
    if not isinstance(cells, list):
        raise ValueError("活跃日历缺少日期列表")
    scores = {}
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("活跃日历记录格式无效")
        day = parse_date(cell.get("date"))
        score = cell.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or score < 0:
            raise ValueError("活跃日历分数无效")
        if day in scores and scores[day] != score:
            raise ValueError("活跃日历同一日期的记录冲突")
        scores[day] = score
    return sorted((day.isoformat() for day, score in scores.items()
                   if start <= day < today and score == 0 and day not in made_up),
                  reverse=True)


def growth_req(method, url, token, uid, proxy=None, body=None):
    """成长中心请求：返回 (http_code, dict或None)。"""
    payload = json.dumps(body) if body is not None else "{}"
    code, text = http(method, url, token, uid, body=payload, proxy=proxy)
    return code, parse_json(code, text)


def run_growth(idx, uid, token, proxy=None):
    """成长中心自动化（对齐 88lin run_growth）：
    领旅行礼物 → 派 Buddy → 任务接单/领奖 → 补登 → 连登兑换 → 开盲盒 → 能量开 Buddy → 展示。
    各子步骤单独 try，一段失败不影响其余；401/403 中止本轮。
    返回 (hard_fail: bool, summary: str)。
    """
    tag = ("账号%d" % idx) + ("(%s…)" % uid[:8] if uid else "")
    parts = []
    credits_gained = 0
    failures = 0
    hard_failures = 0
    successes = 0

    def req(method, path, body=None):
        return growth_req(method, GROWTH_BASE + path, token, uid, proxy, body)

    def auth_abort(code):
        if code == 401:
            parts.append("登录态失效(401)，请更新 Token")
            return True
        if code == 403:
            parts.append("权限拒绝(403)")
            return True
        return False

    def note_http(code, body, label, required=False):
        """前置查询非 2xx 的统一记录；返回 True 表示跳过后续处理。
        required（补登判断所必需）或硬失败(5xx/网络) 计入失败。"""
        nonlocal failures, hard_failures
        if 200 <= code < 300 and (not required or _api_success(code, body)):
            return False
        detail = str(body.get("error") or body.get("msg") or "") if isinstance(body, dict) else ""
        reason = "HTTP %s" % code
        parts.append("%s失败：%s" % (label, "%s（%s）" % (reason, detail)
                                     if detail and detail != reason else (detail or reason)))
        if required or code >= 500:
            failures += 1
            hard_failures += 1
        return True

    # --- 1. Buddy 旅行：领礼物 + 派出发 ---
    try:
        scode, sbody = req("GET", "/buddy/travel/status")
        if auth_abort(scode):
            return True, "%s 成长中心: %s" % (tag, "；".join(parts))
        travel = dig(sbody, "state") if (200 <= scode < 300) else None
        daily_limit = bool(dig(sbody, "daily_limit_reached")) if (200 <= scode < 300) else False
        note_http(scode, sbody, "查旅行状态")
        claimed_travel = False
        if travel == "arrived":
            record_id = dig(sbody, "record_id")
            ccode, cbody = req("POST", "/buddy/travel/claim", {"record_id": record_id})
            if auth_abort(ccode):
                return True, "%s 成长中心: %s" % (tag, "；".join(parts))
            if 200 <= ccode < 300 and dig(cbody, "reward_credit") is not None:
                got = as_int(dig(cbody, "reward_credit"))
                credits_gained += got
                parts.append("领旅行礼物 +%s 积分" % fmt_credit(got))
                successes += 1
                claimed_travel = True
            else:
                msg = (dig(cbody, "msg") or "") if isinstance(cbody, dict) else ""
                parts.append("领旅行礼物失败：%s" % (msg or "HTTP %s" % ccode))
                failures += 1
                if ccode >= 500:
                    hard_failures += 1
            if claimed_travel:
                travel = "idle"   # 只有领取成功后才允许派出发，避免覆盖未领奖励
        if travel == "idle" and daily_limit:
            parts.append("今日旅行名额已用完")
        elif travel == "idle":
            ccode, cbody = req("GET", "/buddy/travel/config")
            if auth_abort(ccode):
                return True, "%s 成长中心: %s" % (tag, "；".join(parts))
            locs = dig(cbody, "locations") if (200 <= ccode < 300) else None
            if locs and isinstance(locs[0], dict):
                loc = locs[0]
                dcode, dbody = req("POST", "/buddy/travel/depart", {"location_id": loc.get("id")})
                if auth_abort(dcode):
                    return True, "%s 成长中心: %s" % (tag, "；".join(parts))
                if 200 <= dcode < 300:
                    loc_name = (dig(dbody, "location") or {}).get("name", "?")
                    dur = dig(dbody, "duration_hours") or (dig(dbody, "location") or {}).get("duration_hours", "?")
                    parts.append("派 Buddy 去%s（%s 小时后回）" % (loc_name, dur))
                    successes += 1
                else:
                    msg = dig(dbody, "msg") or ""
                    parts.append("派 Buddy 失败：%s" % (msg or "HTTP %s" % dcode))
                    failures += 1
                    if dcode >= 500:
                        hard_failures += 1
        elif travel == "traveling":
            loc_name = (dig(sbody, "location") or {}).get("name", "?")
            parts.append("Buddy 旅行中（%s%s）" % (
                loc_name, _fmt_eta(dig(sbody, "arrive_at"), dig(sbody, "server_now"))))
    except Exception as e:
        parts.append("旅行模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 2. 任务接单 + 领奖（放抽奖前：任务送的抽奖机会/能量马上能用上）---
    try:
        tcode, tbody = req("GET", "/tasks")
        if auth_abort(tcode):
            return True, "%s 成长中心: %s" % (tag, "；".join(parts))
        if not note_http(tcode, tbody, "查任务列表"):
            tasks = dig(tbody, "tasks") or []
            titles = {t.get("task_code"): t.get("title", t.get("task_code")) for t in tasks}
            pending = [t.get("task_code") for t in tasks
                       if t.get("task_code") and not t.get("locked")
                       and t.get("accept_status") == "not_accepted"]
            for i in range(0, len(pending), 20):
                batch = pending[i:i + 20]
                acode, abody = req("POST", "/tasks/accept", {"task_codes": batch})
                if auth_abort(acode):
                    return True, "%s 成长中心: %s" % (tag, "；".join(parts))
                results = dig(abody, "results")
                if not isinstance(results, list):
                    results = [{"task_code": c,
                                "status": "ok" if 200 <= acode < 300 else "error",
                                "message": dig(abody, "msg")} for c in batch]
                for r in results:
                    title = titles.get(r.get("task_code"), r.get("task_code"))
                    if r.get("status") == "error":
                        parts.append("领取任务「%s」失败：%s" % (
                            title, r.get("message") or "HTTP %s" % acode))
                        failures += 1
                        if acode >= 500:
                            hard_failures += 1
                    else:
                        parts.append("领取任务「%s」" % title)
                        successes += 1
            for t in tasks:
                if t.get("locked") or t.get("accept_status") != "completed":
                    continue
                code = t.get("task_code")
                title = titles.get(code, code)
                try:
                    ccode, cbody = req("POST", "/tasks/%s/claim" % code, {})
                    if auth_abort(ccode):
                        return True, "%s 成长中心: %s" % (tag, "；".join(parts))
                    if 200 <= ccode < 300 and not dig(cbody, "already_claimed"):
                        rc = _first_int(cbody, "credit", t.get("reward_credit"))
                        re_ = _first_int(cbody, "energy", t.get("reward_energy"))
                        credits_gained += rc
                        parts.append("领任务奖「%s」+积分%s+能量%s" % (title, rc, re_))
                        successes += 1
                    elif 200 <= ccode < 300:
                        parts.append("任务奖「%s」已领过" % title)
                    else:
                        parts.append("领任务奖「%s」失败：%s" % (
                            title, dig(cbody, "msg") or "HTTP %s" % ccode))
                        failures += 1
                        if ccode >= 500:
                            hard_failures += 1
                except Exception as e:
                    parts.append("领任务奖「%s」异常（%s: %s）" % (code, type(e).__name__, e))
                    failures += 1
                    hard_failures += 1
    except Exception as e:
        parts.append("任务模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 3. 补登卡：断登自动补一张（放连登兑换前，兑换才能拿到最新解锁状态）---
    streak_body = None
    streak_stale = False
    try:
        mcode, mbody = req("GET", "/streak")
        if auth_abort(mcode):
            return True, "%s 成长中心: %s" % (tag, "；".join(parts))
        if not note_http(mcode, mbody, "查连登状态", required=True):
            streak_body = mbody
            cards_obj = dig(mbody, "makeup_cards")
            cards = as_int(cards_obj.get("balance")) if isinstance(cards_obj, dict) \
                else as_int(cards_obj)
            dates = []
            if cards > 0:
                hcode, hbody = req("GET", "/heatmap")
                if auth_abort(hcode):
                    return True, "%s 成长中心: %s" % (tag, "；".join(parts))
                if not note_http(hcode, hbody, "查补登日历", required=True):
                    dates = _makeup_candidates(mbody, hbody)
            if dates:
                made_up_n = 0
                for d in dates[:min(cards, MAKEUP_MAX_PER_RUN)]:
                    ucode, ubody = req("POST", "/makeup-cards/use", {"target_date": d})
                    if auth_abort(ucode):
                        return True, "%s 成长中心: %s" % (tag, "；".join(parts))
                    if _api_success(ucode, ubody):
                        cards -= 1
                        made_up_n += 1
                        streak_stale = True
                        left_obj = dig(ubody, "makeup_cards")
                        left_cards = as_int(left_obj.get("balance"), cards) \
                            if isinstance(left_obj, dict) else as_int(left_obj, cards)
                        parts.append("补登 %s（剩 %s 张卡）" % (d, left_cards))
                        cards = max(0, left_cards)
                        successes += 1
                    else:
                        msg = dig(ubody, "msg") or ""
                        if ucode == 400 and str(msg).strip().lower() == "date is not broken, no makeup needed":
                            parts.append("%s 已活跃或已补登，无需再次补登" % d)
                            break
                        parts.append("补登 %s 失败：%s" % (d, msg or "HTTP %s" % ucode))
                        failures += 1
                        hard_failures += 1
                        break
                if made_up_n and len(dates) > made_up_n and cards > 0:
                    parts.append("另有 %s 天可补、剩 %s 张卡，下轮继续" % (
                        len(dates) - made_up_n, cards))
    except Exception as e:
        parts.append("补登模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 4. 连登奖励兑换（入门/进阶/巅峰三档）---
    try:
        rcode, rbody = req("GET", "/redeem/summary")
        if auth_abort(rcode):
            return True, "%s 成长中心: %s" % (tag, "；".join(parts))
        if not note_http(rcode, rbody, "查连登兑换"):
            for tier, status_key, label, days in _REDEEM_TIERS:
                status = dig(rbody, status_key + "_status")
                if not status or status in ("claimed", "locked"):
                    continue
                c2code, c2body = req("POST", "/redeem",
                                     {"tier": tier, "client_token": _client_token()})
                if _is_unknown_tier(c2code, c2body):
                    c2code, c2body = req("POST", "/redeem",
                                         {"tier": days, "client_token": _client_token()})
                # 403「天数不足」是业务常态，必须先于中止判断
                if _is_tier_locked(c2code, c2body):
                    parts.append("连登兑换「%s」未解锁（连登天数不足）" % label)
                    continue
                if auth_abort(c2code):
                    return True, "%s 成长中心: %s" % (tag, "；".join(parts))
                if 200 <= c2code < 300:
                    credits_gained += as_int(dig(c2body, "credit_granted"))
                    parts.append("连登兑换「%s」%s" % (label, _redeem_reward_desc(c2body, tier)))
                    successes += 1
                else:
                    msg = dig(c2body, "msg") or ""
                    parts.append("连登兑换「%s」失败：%s" % (label, msg or "HTTP %s" % c2code))
                    failures += 1
                    if c2code >= 500:
                        hard_failures += 1
    except Exception as e:
        parts.append("连登兑换模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 5. 盲盒/抽奖（draw 必须带 client_token，缺了会 400）---
    try:
        lcode, lbody = req("GET", "/lottery/chances")
        if auth_abort(lcode):
            return True, "%s 成长中心: %s" % (tag, "；".join(parts))
        chances = 0 if note_http(lcode, lbody, "查抽奖机会") else as_int(dig(lbody, "balance"))
        if chances > 0:
            dcode, dbody = req("POST", "/lottery/draw", {"client_token": _client_token()})
            if auth_abort(dcode):
                return True, "%s 成长中心: %s" % (tag, "；".join(parts))
            if 200 <= dcode < 300:
                prize = dig(dbody, "prize_name") or dig(dbody, "prize") or "未知"
                if not isinstance(prize, str):
                    prize = str(prize)
                if dig(dbody, "need_address") or dig(dbody, "require_address"):
                    prize += "（实物奖，需到成长中心填写收件信息）"
                parts.append("开盲盒获得：%s" % prize)
                successes += 1
                if chances > 1:
                    parts.append("还剩 %s 次抽奖机会，下轮继续" % (chances - 1))
            else:
                msg = dig(dbody, "msg") or ""
                if _is_no_chance(msg):
                    parts.append("开盲盒：%s" % (msg or "无抽奖机会"))
                else:
                    parts.append("开盲盒失败：%s" % (msg or "HTTP %s" % dcode))
                    failures += 1
                    if dcode >= 500:
                        hard_failures += 1
    except Exception as e:
        parts.append("盲盒模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 6. Buddy 盲盒（能量攒够就开）---
    try:
        qcode, qbody = req("GET", "/buddy/quota")
        if auth_abort(qcode):
            return True, "%s 成长中心: %s" % (tag, "；".join(parts))
        if not note_http(qcode, qbody, "查 Buddy 能量"):
            affordable = as_int(dig(qbody, "affordable"))
            max_open = as_int(dig(qbody, "max_open_count"), 1) or 1
            if affordable > 0:
                count = min(affordable, max_open)
                ocode, obody = req("POST", "/buddy/open",
                                   {"count": count, "client_token": _client_token()})
                if auth_abort(ocode):
                    return True, "%s 成长中心: %s" % (tag, "；".join(parts))
                if 200 <= ocode < 300:
                    name = dig(obody, "buddy") or dig(obody, "name") or dig(obody, "buddies")
                    if not isinstance(name, str):
                        name = "新 Buddy"
                    parts.append("开 Buddy 盲盒 ×%s（%s）" % (count, name))
                    successes += 1
                else:
                    msg = dig(obody, "msg") or ""
                    parts.append("开 Buddy 盲盒失败：%s" % (msg or "HTTP %s" % ocode))
                    failures += 1
                    if ocode >= 500:
                        hard_failures += 1
    except Exception as e:
        parts.append("Buddy 盲盒模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 7. 能量 & 连签状态（纯展示值，失败不计）---
    energy = None
    streak_days = None
    try:
        ecode, ebody = req("GET", "/energy")
        if 200 <= ecode < 300:
            energy = dig(ebody, "balance")
    except Exception:
        pass
    try:
        if streak_body is not None and not streak_stale:
            streak_obj = dig(streak_body, "streak") or {}
            streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
        else:
            scode2, sbody2 = req("GET", "/streak")
            if 200 <= scode2 < 300:
                streak_obj = dig(sbody2, "streak") or {}
                streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
    except Exception:
        pass

    tail = []
    if energy is not None:
        tail.append("能量 %s" % energy)
    if streak_days is not None:
        tail.append("连签 %s 天" % streak_days)
    if credits_gained:
        tail.append("本次 +共 %s 积分" % credits_gained)

    if parts:
        report = "；".join(parts)
    elif failures:
        report = "成长中心各步骤均失败"
    else:
        report = "成长中心无可领取项"
    if tail:
        report += "（%s）" % "，".join(tail)

    return hard_failures > 0, "%s 成长中心: %s" % (tag, report)


# ---------------------------------------------------------------- 通知推送

def _load_ql_config():
    """读取青龙 config.sh 中的 key=value 配置，供 notify.py 不可用时兜底。"""
    cfg = {}
    for p in ("/ql/data/config/config.sh", "/ql/config/config.sh"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.replace("export", "").strip()
                    v = v.strip().strip('"').strip("'")
                    if k and v and k not in cfg:
                        cfg[k] = v
        except Exception:
            pass
    return cfg


def _dingtalk_send(token, secret, title, content):
    """不依赖 notify.py，直发钉钉机器人（支持加签）。"""
    ts = str(round(time.time() * 1000))
    url = "https://oapi.dingtalk.com/robot/send?access_token=" + token
    if secret:
        sign = base64.b64encode(hmac.new(
            secret.encode(), (ts + "\n" + secret).encode(), hashlib.sha256).digest()).decode()
        url += "&timestamp=%s&sign=%s" % (ts, quote_plus(sign))
    payload = json.dumps({"msgtype": "text",
                          "text": {"content": "%s\n%s" % (title, content)}})
    if HAVE_REQUESTS:
        resp = _requests.post(url, data=payload,
                              headers={"Content-Type": "application/json"}, timeout=15)
        code, text = resp.status_code, resp.text
    else:
        req = urllib.request.Request(url, data=payload.encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                code, text = r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            code, text = e.code, e.read().decode("utf-8", "ignore")
    try:
        j = json.loads(text)
        if j.get("errcode") == 0:
            return True
        log("钉钉机器人返回异常: %s" % text[:200])
    except Exception:
        log("钉钉机器人响应无法解析: %s" % text[:200])
    return False


def push_notify(title, content):
    """推送通知：优先青龙 notify.py（自动搜索常见路径，兼容脚本在子目录），
    失败则用 DD_BOT_TOKEN/DD_BOT_SECRET（环境变量或 config.sh）直发钉钉。"""
    # 方式 1：青龙 notify.py
    for d in ("/ql/data/scripts", "/ql/scripts", "/ql/data", os.getcwd(),
              os.path.dirname(os.path.abspath(__file__))):
        if d and os.path.isfile(os.path.join(d, "notify.py")) and d not in sys.path:
            sys.path.insert(0, d)
    try:
        from notify import send  # noqa
        send(title, content)
        return True
    except Exception as e:
        log("notify.py 不可用(%s)，尝试钉钉直发兜底..." % e)

    # 方式 2：钉钉机器人直发
    cfg = _load_ql_config()
    token = os.environ.get("DD_BOT_TOKEN") or cfg.get("DD_BOT_TOKEN")
    secret = os.environ.get("DD_BOT_SECRET") or cfg.get("DD_BOT_SECRET")
    if token:
        try:
            return _dingtalk_send(token, secret, title, content)
        except Exception as e:
            log("钉钉直发失败: %s" % e)
    else:
        log("未找到 DD_BOT_TOKEN（环境变量或 config.sh），无法兜底推送")
    return False


# ---------------------------------------------------------------- 主流程

def main():
    proxy = os.environ.get("WB_PROXY") or None
    claim_gift = os.environ.get("WB_CLAIM_GIFT", "0").lower() in ("1", "true", "yes")
    growth_enabled = os.environ.get("WB_GROWTH", "1").lower() not in ("0", "false", "no", "off")

    raw = (os.environ.get("WB_ACCESS_TOKENS", "")
           or os.environ.get("WB_ACCESS_TOKEN", "")).strip()
    cookie_raw = os.environ.get("WB_COOKIE", "").strip()
    if not raw and not cookie_raw:
        log("❌ 未配置环境变量 WB_ACCESS_TOKEN / WB_ACCESS_TOKENS（Token 模式）")
        log("   或 WB_COOKIE（Cookie 模式，网页版会话）")
        log("获取方法：运行 get_token.py 登录后自动抓取，或见脚本头部注释")
        log("  Windows 客户端文件: %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info")
        log("  取 auth.accessToken 字段值；或浏览器登录 workbuddy.cn 后 F12 抓 Authorization")
        sys.exit(1)

    summary_lines = []
    ok, fail = 0, 0

    accounts = split_accounts(raw) if raw else []
    if accounts:
        log("========== WorkBuddy 自动签到开始（Token 模式） ==========")
        log("运行环境: %s | 账号数: %d" % (
            "requests" if HAVE_REQUESTS else "urllib(标准库)", len(accounts)))
        for idx, (uid, token) in enumerate(accounts, 1):
            try:
                success, msg = checkin_one(idx, uid, token, proxy, claim_gift)
            except Exception as e:
                success, msg = False, "异常: %s" % e
                if os.environ.get("WB_DEBUG"):
                    traceback.print_exc()
            if success:
                ok += 1
            else:
                fail += 1
                log("❌ " + msg)
            summary_lines.append(msg)
            # 成长中心（签到成功才跑；401/403 等登录态问题签到阶段已暴露）
            if growth_enabled and success:
                try:
                    ghard, gmsg = run_growth(idx, uid, token, proxy)
                except Exception as e:
                    ghard, gmsg = True, "账号%d 成长中心异常: %s" % (idx, e)
                    if os.environ.get("WB_DEBUG"):
                        traceback.print_exc()
                log("[%s]" % ("⚠️" if ghard else "🌱") + gmsg)
                summary_lines.append(gmsg)
                if ghard:
                    fail += 1   # 硬失败(5xx/登录态失效)让任务标红提醒；业务常态不计失败
            if idx < len(accounts):
                time.sleep(random.uniform(2, 5))

    if cookie_raw:
        cookies = [c.strip() for c in re.split(r"[\n]+", cookie_raw) if c.strip()]
        if len(cookies) <= 1 and "&" in cookie_raw:
            cookies = [c.strip() for c in cookie_raw.split("&") if c.strip()]
        log("========== WorkBuddy 自动签到开始（Cookie 模式） ==========")
        log("运行环境: %s | Cookie 账号数: %d" % (
            "requests" if HAVE_REQUESTS else "urllib(标准库)", len(cookies)))
        for idx, ck in enumerate(cookies, 1):
            try:
                success, msg = cookie_run(idx, ck, proxy)
            except Exception as e:
                success, msg = False, "异常: %s" % e
                if os.environ.get("WB_DEBUG"):
                    traceback.print_exc()
            if success:
                ok += 1
            else:
                fail += 1
                log("❌ " + msg)
            summary_lines.append(msg)
            if idx < len(cookies):
                time.sleep(random.uniform(2, 5))

    log("\n========== 签到结束：成功 %d / 失败 %d ==========" % (ok, fail))
    summary = "WorkBuddy 签到（成功%d/失败%d）\n" % (ok, fail) + "\n".join(summary_lines)

    # 青龙内置通知（notify.py 优先，钉钉直发兜底）
    if os.environ.get("WB_NOTIFY", "true").lower() != "false":
        if push_notify("【青龙】WorkBuddy 签到", summary):
            log("已通过青龙通知推送结果")
        else:
            log("通知推送失败：notify.py 不可用且缺少 DD_BOT_TOKEN 配置")

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
