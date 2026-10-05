# workbuddy-自动签到脚本
ql_scripts-AI_auto_sign
AI Agent platform automatic sign-in / daily task script



## 目录：

- [百度库库AI 自动签到（青龙面板）](#百度库库AI 自动签到)
- [WorkBuddy 自动签到（本机免维护 + 青龙面板两种方案）](WorkBuddy 自动签到（本机免维护 + 青龙面板两种方案）)



<a id="百度库库AI 自动签到"></a>

## 百度库库AI 自动签到（青龙面板）

自动完成 kuku.baidu.com（库库AI）的每日积分任务：

| 任务        | 奖励      | 自动化           |
| --------- | ------- | ------------- |
| 每日登录      | +50 积分  | ✅ 全自动         |
| 完成一次对话    | +50 积分  | ✅ 全自动         |
| 首次下载登录桌面端 | +500 积分 | ⚡ 尽力而为（见下方说明） |
| 任务面板可领奖励  | 不定      | ✅ 自动扫描补领      |
| 邀请新用户     | +50~300 | ❌ 需真人，脚本不处理   |

积分可白嫖库库内置的 DeepSeek、GLM、文心、Kimi、MiniMax 等模型额度。

> 脚本参考了 kuku2api（网页版接口）与库库电脑客户端 v1.6.7 的真实接口契约实现，  
> 仅操作你自己的账号会话，不绕过任何认证。请勿滥用，遵守库库服务条款。



---

#### 一、获取 Cookie

###### 方式 A：web 模式（推荐，成功率最高）

1. Chrome 打开 <https://kuku.baidu.com> 并登录百度账号
2. 按 `F12` 打开开发者工具 → **Network（网络）** → 刷新页面
3. 点任意一个发往 `kuku.baidu.com` 的请求 → **Headers（标头）** → Request Headers
4. 复制 `Cookie:` 后面的整串内容（包含 `BDUSS`、`STOKEN` 等，很长）

###### 方式 B：client 模式

只需 Cookie 中的两个字段：

```
BDUSS=你的BDUSS值; STOKEN=你的STOKEN值
```

（同样从 F12 → Application → Cookies → kuku.baidu.com 中找 BDUSS 和 STOKEN）

> ⚠️ Cookie 是登录凭证，等同密码，不要泄露给任何人。

---

#### 二、青龙面板部署

1. **脚本管理** → 新建文件 `kuku_checkin.py`，把 `kuku_checkin.py` 的内容粘贴进去
2. **依赖管理 → Python3** → 安装依赖：
   ```
   requests
   curl_cffi
   ```
   （`curl_cffi` 可选但强烈建议安装——百度接口对 TLS 指纹有检测，装了成功率更高）
3. **环境变量** → 新建变量：
   - 名称：`KUKU_COOKIE`，值：方式 A 抓到的完整 Cookie
   - （可选）名称：`KUKU_CLIENT_COOKIE`，值：方式 B 的 `BDUSS=xxx; STOKEN=yyy`
   - 多账号：多个值之间用 **换行** 或 `&` 分隔写在同一个变量里
4. **定时任务** → 新建任务：
   - 命令：`task kuku_checkin.py`
   - 定时规则：`30 8 * * *`（每天 8:30，可自行调整）
5. 手动点一次「运行」，查看日志确认签到成功

#### 三、可选环境变量

| 变量            | 说明                                | 默认         |
| ------------- | --------------------------------- | ---------- |
| `KUKU_MODE`   | `web` / `client` / `auto`         | auto（自动识别） |
| `KUKU_PROXY`  | HTTP 代理，如 `http://127.0.0.1:7890` | 无          |
| `KUKU_NOTIFY` | `false` 关闭青龙通知推送                  | true       |
| `KUKU_DEBUG`  | 填任意值输出完整异常栈                       | 无          |

通知推送为**双保险机制**：

1. **优先走青龙 `notify.py`**：脚本会自动搜索 `/ql/data/scripts` 等常见路径（脚本放在子目录也能找到），找到就按青龙「系统设置 → 通知设置」里配置的渠道发送
2. **notify.py 不可用时自动兜底**：直接读取 `DD_BOT_TOKEN` / `DD_BOT_SECRET`（优先环境变量，其次青龙 `config.sh`）直发钉钉机器人，支持加签。日志会明确显示走了哪条路

> 注意：如果你的钉钉机器人设置了「自定义关键词」安全校验，脚本推送的标题固定以
> `【青龙】` 开头，请确保关键词包含"青龙"；或改用「加签」安全方式
> （脚本已支持自动加签，把 SECRET 填到 `DD_BOT_SECRET` 即可）。

#### 四、关于「首次下载登录桌面端 送500积分」

这是**一次性**新用户任务（服务端任务类型 `SELF_DOWNLOAD`）。脚本会以电脑客户端身份  
（`clienttype=401` + 客户端 UA）自动尝试上报领取，结果分三种情况：

1. **直接领取成功**：服务端认可，日志显示「领取成功 +500」
2. **服务端要求真实的桌面端登录事件**：日志显示「服务端未认可」——此时需要你手动  
   [下载库库桌面端](https://kuku.baidu.com) 安装并登录一次（首次登录事件由服务端记录），  
   之后**无需任何操作**：脚本每次运行都会扫描任务面板，一旦该任务变为可领取状态  
   （`claimable_point > 0`），会通过 `rewardClaim` 自动把 500 积分补领到手
3. **已领过**：日志显示「已领过」，无副作用

> 建议：反正 500 积分是白送的，最稳妥的做法是装一次桌面端登录一下，  
> 剩下的交给脚本自动补领。

#### 五、常见问题

- **提示 Cookie 已失效 / userreport errno 非 0**：Cookie 过期了，重新抓一次更新环境变量。库库 Cookie 一般能维持数周到数月，避免频繁在别处登出账号可延长有效期。
- **requests 报 403 / 连接被重置**：安装 `curl_cffi` 依赖（脚本会自动优先使用它模拟 Chrome TLS 指纹）。
- **重复运行会重复加分吗**：不会。接口幂等，已领过的任务返回「已领过」，reward 为 0。
- **errno=151004**：表示当前没有进行中的积分活动（活动可能已结束），属正常现象。

#### 六、免责声明

本项目仅供学习交流，未与百度有任何关联。使用本项目产生的一切后果由使用者自行承担。



<a id="WorkBuddy 自动签到（本机免维护 + 青龙面板两种方案）"></a>

## WorkBuddy 自动签到（本机免维护 + 青龙面板两种方案）

WorkBuddy（workbuddy.cn）每日签到自动领积分。核心方案来自开源项目
[88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin)（MIT，已安全审计）：
直接读取本机 WorkBuddy 桌面端登录凭据，**新版加密格式由本机客户端运行时在子进程中解密**，
无需抓包、无需手动复制 Token、Token 永不过期（客户端自动刷新）。

#### 已验证（2026-10-05 本机实测）

- `signin.py status` → 解密成功，返回真实签到状态（Buddy加油站，连续3天/300积分）
- `signin.py auto` → 成功领取 100 积分（连续4天），并领到任务奖 +300 积分 +8 能量
- `export_token.py` → Token 解密导出成功，配 `workbuddy_checkin.py` 跑通端到端

#### 方案 A：本机定时任务（推荐，零维护）

凭据由客户端自动刷新，脚本每次实时解密，**永远不存在 Token 过期问题**。

```powershell
## 在本目录执行（需已装 Python 3），自动创建两个计划任务：
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
```

创建的任务：
- `WorkBuddyAutoSignin`：每天 00:05 签到 + 成长中心，静默写 `signin.log`
- `WorkBuddyGrowthPoll`：每 4 小时补签 + 成长中心（01/05/09/13/17/21 点）

手动运行：
```bash
python signin.py auto      ## 签到 + 成长中心（幂等，重复跑不会多领）
python signin.py status    ## 只查签到状态
python signin.py doctor    ## 离线检查凭据与解密能力
```

#### 方案 B：青龙面板

1. 本机运行 `python export_token.py` → 解密 Token 保存到 `workbuddy_token.txt`
2. 把文件内容粘贴到青龙环境变量 `WB_ACCESS_TOKEN`
3. 青龙部署 `workbuddy_checkin.py`：
   - 依赖：Python3 装 requests（可选）
   - 定时任务：`task workbuddy_checkin.py`，如 `30 8 * * *`
4. Token 约 90 天过期，过期后（青龙日志报 401）重跑 `export_token.py` 刷新

> 方案 A 和 B 可同时用：本机签到为主，青龙作为多账号/集中管理补充。

#### 环境变量（workbuddy_checkin.py，青龙用）

| 变量名                                    | 必填   | 说明                                                         |
| ----------------------------------------- | ------ | ------------------------------------------------------------ |
| `WB_ACCESS_TOKEN`                         | 二选一 | 单账号 Bearer Token（export_token.py 导出）                  |
| `WB_ACCESS_TOKENS`                        | 二选一 | 多账号，逗号/换行分隔                                        |
| `WB_COOKIE`                               | 二选一 | Cookie 会话模式（网页版）                                    |
| `WB_CLAIM_GIFT`                           | 否     | `1` 开启今日礼包领取                                         |
| `WB_GROWTH`                               | 否     | 成长中心开关，默认 `1` 开启（`0` 关闭）；Cookie 模式不支持、自动跳过 |
| `WB_PROXY` / `WB_NOTIFY` / `WB_WARN_DAYS` | 否     | 代理 / 通知开关 / 过期提前提醒天数。通知走青龙 notify.py，不可用时自动用 `DD_BOT_TOKEN`/`DD_BOT_SECRET`（环境变量或 `/ql/data/config/config.sh`）直发钉钉兜底 |

#### 文件说明

| 文件                   | 用途                                                         |
| ---------------------- | ------------------------------------------------------------ |
| `signin.py`            | 88lin 全功能签到（签到+成长中心+补签+连登兑换），本机运行    |
| `install-windows.ps1`  | 一键创建 Windows 计划任务（来自 88lin 仓库，已审计）         |
| `export_token.py`      | 解密导出 Token → workbuddy_token.txt（青龙用）               |
| `workbuddy_checkin.py` | 青龙面板签到脚本（Token/Cookie 双模式；成长中心全套对齐 88lin `signin.py auto`：旅行/任务/补登/兑换/盲盒） |
| `test_growth_mock.py`  | 成长中心模块离线自测（mock 接口，不联网）                    |
| `get_token.py`         | 备用：网页登录窗口抓 Token（v3，网页版为 Cookie 会话时用）   |

#### 安全说明

- 签到接口逆向自桌面端 app.asar，只向官方接口 `copilot.tencent.com` 发送你自己的凭据
- 加密凭据的密钥只在短生命周期的客户端子进程内使用，不落盘、不进日志
- `signin.py` 永远不打印 Token；`export_token.py` 是为青龙场景特意导出的
- 仅供个人学习交流，请遵守 WorkBuddy 服务条款
