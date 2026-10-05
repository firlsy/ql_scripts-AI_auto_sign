#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy Token 自动获取器 v3（登录即抓取，窗口右侧实时输出）
================================================================
原理：
  打开一个内嵌浏览器窗口加载 https://www.workbuddy.cn/app，
  你正常登录（手机号验证码/微信均可），程序自动从多个路径抓取凭证：
    1. 劫持页面内 fetch / XMLHttpRequest（被动监听，只看 JSON 响应）：
       - 请求头中的 Authorization: Bearer <token>
       - 响应体 JSON 中的 access_token 字段（登录换 token 接口最常见）
    2. 每 5 秒扫描 localStorage / sessionStorage / document.cookie 中的 JWT
    3. 每 10 秒读取浏览器 HttpOnly Cookie（get_cookies）：
       - Cookie 值本身是 JWT 则直接捕获
       - 否则用 Cookie 会话实测签到接口（网页版 Cookie 鉴权路径）
    4. 抓到后在【窗口右侧悬浮面板】展示 Token/Cookie + 一键复制，
       并写入脚本同目录：
         workbuddy_token.txt   Bearer Token（配 WB_ACCESS_TOKEN 用）
         workbuddy_cookies.txt Cookie 会话（配 WB_COOKIE 用，二选一）
    5. 面板带「深度扫描」按钮与事件日志区，便于排查。

v3 修复（针对 v2 白屏/刷屏问题）：
  - 钩子完全被动化：不再对每个响应做全文 clone 读取，只处理 JSON 且 <300KB 的响应
  - 桥接调用最小化：JS 侧对同一 Token 去重，绝不重复回调 Python
  - 不再每个响应更新面板计数（去掉高频 DOM/桥接风暴）
  - 注入器抗瞬态错误：页面跳转/加载中不再导致保活线程意外退出

使用方法：
  pip install pywebview
  python get_token.py

说明：
  - 若网页版为 Cookie 会话（无 JWT），面板会显示有效 Cookie 并保存，
    签到脚本请用环境变量 WB_COOKIE（v2+ 已支持 Cookie 模式）
  - Token 不经过任何第三方；有效期约 90 天，过期后重跑本程序
"""

import os
import sys
import json
import time
import base64
import threading
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None
    import urllib.request
    import urllib.error

webview = None  # 延迟导入：仅 main() 需要

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(BASE_DIR, "workbuddy_token.txt")
OUT_COOKIE_FILE = os.path.join(BASE_DIR, "workbuddy_cookies.txt")

APP_URL = "https://www.workbuddy.cn/app"
STATUS_URL = "https://copilot.tencent.com/v2/billing/meter/checkin-activity-status"
WEB_STATUS_URL = "https://www.workbuddy.cn/billing/meter/checkin-activity-status"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- HTTP 测试

def _post(url, headers, body="{}"):
    """返回 (http_code, text)。"""
    if requests:
        r = requests.post(url, headers=headers, data=body, timeout=20)
        return r.status_code, r.text
    req = urllib.request.Request(url, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def test_token(token):
    """Bearer Token 实测（copilot 官方接口），返回 (可用, 描述)。"""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
        "Origin": "https://www.workbuddy.cn",
        "Referer": "https://www.workbuddy.cn/",
        "User-Agent": UA,
    }
    try:
        code, text = _post(STATUS_URL, headers)
        if code == 401:
            return False, "登录态无效(401)"
        try:
            j = json.loads(text)
        except Exception:
            return False, "接口返回异常(HTTP %s)" % code
        if j.get("code") == 0:
            d = j.get("data") or {}
            return True, "有效！今日%s（连续 %s 天）" % (
                "已签到" if d.get("today_checked_in") else "未签到",
                d.get("streak_days", 0))
        return False, "接口返回 code=%s msg=%s" % (j.get("code"), j.get("msg", ""))
    except Exception as e:
        return False, "测试请求失败: %s" % e


def test_cookie_session(cookie_header):
    """Cookie 会话实测（网页版同源接口），返回 (可用, 描述)。"""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": cookie_header,
        "Origin": "https://www.workbuddy.cn",
        "Referer": "https://www.workbuddy.cn/app",
        "User-Agent": UA,
    }
    try:
        code, text = _post(WEB_STATUS_URL, headers)
        if code in (401, 403):
            return False, "Cookie 会话无效(HTTP %s)" % code
        try:
            j = json.loads(text)
        except Exception:
            return False, "接口返回异常(HTTP %s): %s" % (code, text[:80])
        if j.get("code") == 0 or j.get("success") is True:
            d = j.get("data") or {}
            return True, "有效！今日%s（连续 %s 天）" % (
                "已签到" if d.get("today_checked_in") else "未签到",
                d.get("streak_days", 0))
        return False, "接口返回 code=%s msg=%s" % (j.get("code"), j.get("msg", ""))
    except Exception as e:
        return False, "测试请求失败: %s" % e


# ---------------------------------------------------------------- JWT 解析

def jwt_payload(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
    except Exception:
        return {}


def jwt_validity(token):
    payload = jwt_payload(token)
    sub = payload.get("sub", "?")
    exp = payload.get("exp")
    if exp:
        remain = (float(exp) - datetime.now(timezone.utc).timestamp()) / 86400.0
        exp_str = datetime.fromtimestamp(float(exp)).strftime("%Y-%m-%d %H:%M")
        return sub, "有效期至 %s（剩约 %.0f 天）" % (exp_str, remain)
    return sub, "无 exp 字段，有效期未知"


# ---------------------------------------------------------------- 页面注入 JS

INJECT_JS = r"""
(function () {
  if (window.__wbTokenHook) return;
  window.__wbTokenHook = true;
  try {  // 整体保护：注入逻辑绝不影响宿主页面
    var JWT = /eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/;
    var reqCount = 0;
    var reported = {};          // 已上报过的候选（JS 侧去重，杜绝桥接风暴）
    var PANEL_MAX_LOG = 6;

    function bridge() { return window.pywebview && window.pywebview.api; }

    // ---------------- 右侧悬浮面板（纯 CSSOM 样式，不受 CSP 限制） ----------------
    function el(tag, text) { var e = document.createElement(tag); if (text != null) e.textContent = text; return e; }
    function st(e, props) { for (var k in props) e.style.setProperty(k, props[k]); return e; }

    function panelLog(msg, color) {
      var box = document.getElementById('wb-log');
      if (!box) return;
      var line = el('div', '[' + new Date().toTimeString().slice(0, 8) + '] ' + msg);
      line.style.color = color || '#9fb3c8';
      box.insertBefore(line, box.firstChild);
      while (box.children.length > PANEL_MAX_LOG) box.removeChild(box.lastChild);
    }

    function ensurePanel() {
      if (document.getElementById('wb-token-panel')) return;
      if (!document.body) return;
      var p = el('div'); p.id = 'wb-token-panel';
      st(p, {position:'fixed', top:'12px', right:'12px', zIndex:2147483647, width:'350px',
             maxHeight:'85vh', overflow:'auto', background:'#161b26', color:'#e8eaf0',
             borderRadius:'12px', boxShadow:'0 8px 30px rgba(0,0,0,.4)',
             font:'13px/1.5 "Segoe UI","Microsoft YaHei",sans-serif', padding:'14px 16px',
             border:'1px solid #2c3644'});

      var head = st(el('div'), {display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:'8px'});
      var title = el('b', '🔍 Token 捕获器'); st(title, {fontSize:'14px'});
      var minBtn = el('span', '— 收起'); st(minBtn, {cursor:'pointer', opacity:'.7', fontSize:'12px', userSelect:'none'});
      head.appendChild(title); head.appendChild(minBtn);

      var status = el('div', '钩子已注入，等待登录/请求…'); status.id = 'wb-status';
      st(status, {color:'#9fb3c8'});
      var meta = el('div', ''); meta.id = 'wb-meta';
      st(meta, {color:'#7a8aa0', fontSize:'11px', marginTop:'2px'});

      var box = el('div'); box.id = 'wb-token-box'; box.style.display = 'none';
      st(box, {marginTop:'10px'});
      var ta = el('textarea'); ta.id = 'wb-token'; ta.readOnly = true;
      st(ta, {width:'100%', boxSizing:'border-box', height:'90px', background:'#0e1218',
              color:'#7ee787', border:'1px solid #2c3644', borderRadius:'8px', padding:'6px',
              font:'11px/1.4 Consolas,monospace', resize:'vertical', wordBreak:'break-all'});
      var copyBtn = el('button', '📋 复制内容');
      st(copyBtn, {marginTop:'6px', width:'100%', padding:'7px', border:'0', borderRadius:'8px',
                   background:'#2f81f7', color:'#fff', cursor:'pointer', fontSize:'13px'});
      var valid = el('div'); valid.id = 'wb-valid';
      st(valid, {marginTop:'6px', color:'#9fb3c8', fontSize:'12px', whiteSpace:'pre-wrap', wordBreak:'break-all'});
      box.appendChild(ta); box.appendChild(copyBtn); box.appendChild(valid);

      var scanBtn = el('button', '🔎 深度扫描本地存储');
      st(scanBtn, {marginTop:'8px', width:'100%', padding:'6px', border:'0', borderRadius:'8px',
                   background:'#2c3644', color:'#cdd9e5', cursor:'pointer', fontSize:'12px'});
      var scanOut = el('div'); scanOut.id = 'wb-scan-out';
      st(scanOut, {marginTop:'6px', color:'#9fb3c8', fontSize:'11px', whiteSpace:'pre-wrap',
                   maxHeight:'150px', overflow:'auto', display:'none',
                   background:'#0e1218', borderRadius:'8px', padding:'6px'});
      var logBox = el('div'); logBox.id = 'wb-log';
      st(logBox, {marginTop:'8px', fontSize:'11px', lineHeight:'1.6', borderTop:'1px solid #2c3644', paddingTop:'6px'});

      p.appendChild(head); p.appendChild(status); p.appendChild(meta);
      p.appendChild(box); p.appendChild(scanBtn); p.appendChild(scanOut); p.appendChild(logBox);
      document.body.appendChild(p);

      var collapsed = false;
      minBtn.onclick = function () {
        collapsed = !collapsed;
        [status, meta, box, scanBtn, scanOut, logBox].forEach(function (x) { x.style.display = collapsed ? 'none' : ''; });
        if (!collapsed) box.style.display = box.dataset.open === '1' ? 'block' : 'none';
        minBtn.textContent = collapsed ? '— 展开' : '— 收起';
      };
      copyBtn.onclick = function () {
        ta.select();
        var done = false;
        try { done = document.execCommand('copy'); } catch (e) {}
        if (!done && navigator.clipboard) { navigator.clipboard.writeText(ta.value); done = true; }
        copyBtn.textContent = done ? '✅ 已复制' : '❌ 复制失败';
        setTimeout(function () { copyBtn.textContent = '📋 复制内容'; }, 1500);
      };
      scanBtn.onclick = function () {
        scanOut.style.display = 'block';
        scanOut.textContent = '扫描中…';
        if (window.__wbDeepScan) window.__wbDeepScan();
      };
      panelLog('钩子注入成功', '#3fb950');
    }

    window.__wbPanel = {
      show: ensurePanel,
      setStatus: function (t, color) {
        ensurePanel(); var e = document.getElementById('wb-status');
        if (e) { e.textContent = t; e.style.color = color || '#9fb3c8'; }
      },
      setToken: function (content, mode, validInfo, outPath) {
        ensurePanel();
        var box = document.getElementById('wb-token-box');
        box.style.display = 'block'; box.dataset.open = '1';
        document.getElementById('wb-token').value = content;
        document.getElementById('wb-valid').textContent = (validInfo || '') + (outPath ? ('\n已保存: ' + outPath) : '');
        var s = document.getElementById('wb-status');
        s.textContent = mode === 'cookie' ? '✅ 已捕获有效 Cookie 会话！' : '✅ 已捕获有效 Token！';
        s.style.color = '#3fb950';
        panelLog(mode === 'cookie' ? 'Cookie 会话验证通过' : 'Token 验证通过', '#3fb950');
      }
    };

    // ---------------- 上报（JS 侧强去重） ----------------
    function report(tok, src) {
      if (!tok || reported[tok]) return;
      reported[tok] = Date.now();
      ensurePanel();
      window.__wbPanel.setStatus('发现候选凭证（' + (src || '?') + '），验证中…', '#d29922');
      panelLog('上报候选: ' + (src || '?') + ' 长度' + tok.length);
      try { if (bridge()) window.pywebview.api.capture_token(String(tok), String(src || '')); } catch (e) {}
    }

    // ---------------- 响应体扫描（只看小体积 JSON，绝不碰流式响应） ----------------
    function scanText(t, src) {
      if (!t || t.length > 300000) return;
      try {
        var j = JSON.parse(t);
        var at = j.access_token || (j.data && j.data.access_token) ||
                 (j.auth && (j.auth.access_token || j.auth.accessToken)) ||
                 (j.result && j.result.access_token);
        if (at) { report(String(at), src + ':access_token字段'); return; }
      } catch (e) {}
      var m = t.match(JWT);
      if (m) report(m[0], src);
    }

    function isSkippableUrl(u) {
      u = String(u || '');
      return /^about:|^blob:|^data:|^chrome|^devtools|127\.0\.0\.1|localhost/i.test(u);
    }

    // ---------------- fetch 劫持（被动） ----------------
    var of = window.fetch;
    if (of) {
      window.fetch = function (input, init) {
        var url = (input && input.url) || String(input || '');
        try {
          var h = (init && init.headers) || (input && input.headers);
          if (h && !isSkippableUrl(url)) {
            var auth = null;
            if (typeof Headers !== 'undefined' && h instanceof Headers) {
              auth = h.get('Authorization') || h.get('authorization');
            } else if (Array.isArray(h)) {
              for (var i = 0; i < h.length; i++) if (String(h[i][0]).toLowerCase() === 'authorization') auth = h[i][1];
            } else {
              auth = h['Authorization'] || h['authorization'];
            }
            if (auth && /^Bearer\s+/i.test(auth)) report(auth.replace(/^Bearer\s+/i, ''), 'fetch请求头');
          }
        } catch (e) {}
        var p = of.apply(this, arguments);
        if (!isSkippableUrl(url)) {
          try {
            p.then(function (resp) {
              try {
                var ct = '';
                try { ct = resp.headers.get('content-type') || ''; } catch (e) {}
                if (ct.indexOf('json') >= 0 && ct.indexOf('event-stream') < 0) {
                  resp.clone().text().then(function (t) { scanText(t, 'fetch响应'); }).catch(function () {});
                }
              } catch (e) {}
            }).catch(function () {});
          } catch (e) {}
        }
        return p;
      };
    }

    // ---------------- XHR 劫持（被动） ----------------
    var osh = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
      try { if (/^authorization$/i.test(String(k)) && /^Bearer\s+/i.test(String(v))) report(String(v).replace(/^Bearer\s+/i, ''), 'xhr请求头'); } catch (e) {}
      return osh.apply(this, arguments);
    };
    var oOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, u) { this.__wbUrl = u; return oOpen.apply(this, arguments); };
    var oSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function () {
      var xhr = this;
      xhr.addEventListener('load', function () {
        try {
          if (isSkippableUrl(xhr.__wbUrl)) return;
          var ct = xhr.getResponseHeader('Content-Type') || '';
          if (ct.indexOf('json') < 0) return;
          scanText(xhr.responseText, 'xhr响应');
        } catch (e) {}
      });
      return oSend.apply(this, arguments);
    };

    // ---------------- 本地存储定时扫描 ----------------
    function scanStores() {
      try {
        [window.localStorage, window.sessionStorage].forEach(function (store) {
          for (var i = 0; i < store.length; i++) {
            var k = store.key(i), v = String(store.getItem(k));
            var m = v.match(JWT);
            if (m) report(m[0], 'storage:' + k);
          }
        });
        var cm = String(document.cookie || '').match(JWT);
        if (cm) report(cm[0], 'document.cookie');
      } catch (e) {}
    }
    setInterval(scanStores, 5000); scanStores();
    setInterval(ensurePanel, 3000); ensurePanel();

    // ---------------- 深度扫描 ----------------
    window.__wbDeepScan = async function () {
      var out = [];
      function dumpStore(name, store) {
        out.push('== ' + name + ' (' + store.length + '键) ==');
        for (var i = 0; i < store.length; i++) {
          var k = store.key(i), v = String(store.getItem(k));
          out.push(k + ' | ' + v.length + '字符' + (JWT.test(v) ? ' [含JWT!]' : ''));
          if (v.length <= 150) out.push('   ' + v.slice(0, 150));
        }
      }
      try { dumpStore('localStorage', localStorage); } catch (e) { out.push('localStorage不可用: ' + e); }
      try { dumpStore('sessionStorage', sessionStorage); } catch (e) { out.push('sessionStorage不可用: ' + e); }
      out.push('== document.cookie ==');
      try { out.push(String(document.cookie || '(空)').replace(/;\s*/g, '\n')); } catch (e) {}
      try {
        var dbs = await indexedDB.databases();
        out.push('== IndexedDB: ' + dbs.length + ' 个库 ==');
        for (var d of dbs) {
          out.push(' ' + d.name + (d.version ? (' (v' + d.version + ')') : ''));
          try {
            var db = await new Promise(function (res) {
              var r = indexedDB.open(d.name);
              r.onsuccess = function () { res(r.result); };
              r.onerror = function () { res(null); };
            });
            if (db) { out.push('   stores: ' + Array.from(db.objectStoreNames).join(', ')); db.close(); }
          } catch (e) {}
        }
      } catch (e) { out.push('IndexedDB不可枚举: ' + e); }
      var text = out.join('\n');
      var e2 = document.getElementById('wb-scan-out');
      if (e2) { e2.textContent = text; e2.style.display = 'block'; }
      try { if (bridge()) window.pywebview.api.deep_scan_report(text.slice(0, 8000)); } catch (err) {}
      scanStores();
    };
  } catch (e) {
    try { window.console.error('wb-hook-init-failed', e); } catch (e2) {}
  }
})();
"""


# ---------------------------------------------------------------- JS 桥接 API

class TokenApi:
    """暴露给页面 JS 的接口（pywebview js_api）。"""

    def __init__(self):
        self.window = None
        self.token = None            # 已验证有效的 Bearer Token
        self.cookie_header = None    # 已验证有效的 Cookie 会话
        self.candidates = set()      # 已见过的候选（去重）
        self.rejected = set()        # 实测无效的候选
        self.lock = threading.Lock()
        self.done = threading.Event()
        self._warned_no_getcookies = False

    # ---- 面板更新（仅在关键节点调用，避免高频桥接） ----
    def _panel(self, js):
        try:
            if self.window:
                self.window.evaluate_js(js)
        except Exception:
            pass

    def _show_token(self, content, mode, info, path):
        self._panel("window.__wbPanel && window.__wbPanel.setToken(%s, %s, %s, %s)"
                    % (json.dumps(content), json.dumps(mode), json.dumps(info), json.dumps(path)))

    def _reject_panel(self, token_head, desc):
        self._panel("window.__wbPanel && window.__wbPanel.setStatus(%s, %s)"
                    % (json.dumps("候选 %s… 无效(%s)，继续监听" % (token_head, desc)),
                       json.dumps("#f85149")))

    # ---- JS 调用：捕获候选凭证 ----
    def capture_token(self, token, source=""):
        token = (token or "").strip()
        if not token:
            return "empty"
        with self.lock:
            if token in self.candidates or token in self.rejected or self.done.is_set():
                return "dup"
            self.candidates.add(token)
        payload = jwt_payload(token)
        if not payload or ("exp" not in payload and "sub" not in payload):
            log("[捕获] 非标准 JWT（来源 %s，长度 %d），已忽略" % (source or "?", len(token)))
            return "invalid"
        threading.Thread(target=self._verify_candidate, args=(token, source), daemon=True).start()
        return "verifying"

    def _verify_candidate(self, token, source):
        sub, validity = jwt_validity(token)
        ok, desc = test_token(token)
        if ok:
            with self.lock:
                if self.done.is_set():
                    return
                self.token = token
                self.done.set()
            log("")
            log("=" * 52)
            log("✅ 已捕获有效 Token！（来源: %s）" % (source or "?"))
            log("   用户ID: %s" % sub)
            log("   %s" % validity)
            log("   接口实测: %s" % desc)
            log("   Token: %s" % token)
            saved = ""
            try:
                with open(OUT_FILE, "w", encoding="utf-8") as f:
                    f.write(token)
                saved = OUT_FILE
                log("   已保存到: %s" % OUT_FILE)
            except Exception as e:
                log("   ⚠️ 保存文件失败: %s" % e)
            log("")
            log("👉 将 Token 粘贴到青龙环境变量 WB_ACCESS_TOKEN 即可")
            log("=" * 52)
            self._show_token(token, "jwt", "用户 %s | %s | %s" % (sub, validity, desc), saved)
        else:
            with self.lock:
                self.rejected.add(token)
            log("[候选] 来源 %s 的 Token 实测无效: %s，继续等待…" % (source or "?", desc))
            self._reject_panel(token[:12], desc.split("(")[0].strip())

    # ---- JS 调用：深度扫描结果 ----
    def deep_scan_report(self, text):
        log("\n----- 深度扫描结果 -----\n%s\n-----------------------" % text)


# ---------------------------------------------------------------- Cookie 轮询

def cookie_poller(window, api):
    """定时读取 HttpOnly Cookie；若是 JWT 直接捕获，否则实测 Cookie 会话。"""
    has_get_cookies = hasattr(window, "get_cookies")
    if not has_get_cookies and not api._warned_no_getcookies:
        api._warned_no_getcookies = True
        log("[提示] 当前 pywebview 版本不支持 get_cookies，仅扫描 JS 可见 Cookie")
    tested_headers = {}
    last_round = 0.0

    while not api.done.is_set():
        time.sleep(10)
        if api.done.is_set():
            break
        try:
            # 窗口关闭时 evaluate_js 会抛异常，借此退出线程
            window.evaluate_js("1")
        except Exception:
            break

        cookies = []
        if has_get_cookies:
            try:
                cookies = window.get_cookies() or []
            except Exception:
                cookies = []

        # 1) Cookie 值本身是 JWT
        for c in cookies:
            v = (getattr(c, "value", None) or "")
            if v.startswith("eyJ"):
                api.capture_token(v, "HttpOnlyCookie:" + getattr(c, "name", "?"))
        if api.done.is_set():
            break

        # 2) Cookie 会话实测（网页版同源接口），限频
        if time.time() - last_round < 60:
            continue
        header_parts = []
        for c in cookies:
            dom = (getattr(c, "domain", "") or "").lstrip(".")
            name = getattr(c, "name", "")
            value = getattr(c, "value", "") or ""
            if name and value and dom.endswith("workbuddy.cn"):
                header_parts.append("%s=%s" % (name, value))
        if not header_parts:
            continue
        header = "; ".join(header_parts)
        last_round = time.time()
        if header in tested_headers and time.time() - tested_headers[header] < 300:
            continue
        tested_headers[header] = time.time()
        ok, desc = test_cookie_session(header)
        if ok:
            with api.lock:
                if api.done.is_set():
                    return
                api.cookie_header = header
                api.done.set()
            log("")
            log("=" * 52)
            log("✅ 网页版为 Cookie 会话，已验证有效！")
            log("   接口实测: %s" % desc)
            log("   Cookie: %s" % (header[:120] + "..." if len(header) > 120 else header))
            saved = ""
            try:
                with open(OUT_COOKIE_FILE, "w", encoding="utf-8") as f:
                    f.write(header)
                saved = OUT_COOKIE_FILE
                log("   已保存到: %s" % OUT_COOKIE_FILE)
            except Exception as e:
                log("   ⚠️ 保存文件失败: %s" % e)
            log("")
            log("👉 将 Cookie 粘贴到青龙环境变量 WB_COOKIE 即可（签到脚本支持 Cookie 模式）")
            log("=" * 52)
            api._show_token(header, "cookie", "Cookie 会话 | %s" % desc, saved)
        else:
            log("[Cookie] 会话实测暂无效: %s（登录完成前属正常现象）" % desc)


# ---------------------------------------------------------------- 注入保活

def inject_keeper(window, api):
    """SPA 跳转/刷新后重新注入钩子与面板。容忍瞬态错误，不死线程。"""
    failures = 0
    while True:
        time.sleep(4)
        try:
            r = window.evaluate_js("!!window.__wbTokenHook")
            failures = 0
            if r is not True:
                window.evaluate_js(INJECT_JS)
        except Exception:
            failures += 1
            if failures > 30:   # 约2分钟连续失败才认定窗口已关闭
                break


# ---------------------------------------------------------------- 主流程

def main():
    global webview
    try:
        import webview as _webview
        webview = _webview
    except ImportError:
        print("缺少依赖：请先执行  pip install pywebview")
        sys.exit(1)

    api = TokenApi()
    log("=" * 52)
    log(" WorkBuddy Token 自动获取器 v3")
    log("=" * 52)
    log("1. 即将打开登录窗口，右侧有「Token 捕获器」面板")
    log("   （面板出现 = 钩子注入成功；看不到面板请反馈）")
    log("2. 请正常登录（手机号验证码/微信均可）")
    log("3. 抓到凭证后面板实时显示并支持一键复制，同时保存:")
    log("   %s" % OUT_FILE)
    log("   %s（Cookie 会话时）" % OUT_COOKIE_FILE)
    log("=" * 52)
    log("")

    window = webview.create_window(
        "WorkBuddy 登录（Token 自动获取）",
        APP_URL,
        js_api=api,
        width=1180,
        height=840,
    )
    api.window = window

    def on_loaded():
        try:
            window.evaluate_js(INJECT_JS)
        except Exception:
            pass

    window.events.loaded += on_loaded
    threading.Thread(target=inject_keeper, args=(window, api), daemon=True).start()
    threading.Thread(target=cookie_poller, args=(window, api), daemon=True).start()

    webview.start()

    log("登录窗口已关闭。")
    if api.token:
        log("Token 已保存: %s" % OUT_FILE)
    elif api.cookie_header:
        log("Cookie 已保存: %s" % OUT_COOKIE_FILE)
    else:
        log("❌ 未捕获到有效凭证。")
        log("   若窗口内能看到右侧面板，请点「深度扫描」并把结果发出来；")
        log("   若看不到面板，说明注入未生效，请连同终端日志一起反馈。")
    try:
        input("按回车键退出...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()
