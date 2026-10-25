# 52Pokemon 套餐购买助手

这是一个独立的 Python + Selenium 项目，支持 Windows 本地运行和 GitHub Actions 无头运行。主流程为：

1. 登录账号；
2. 定位“入门精灵球”；
3. 选择“续费套餐”或“立即订购”；
4. 选择月付；
5. 取消旧的待支付订单；
6. 从服务器读取当前兑换码并使用；
7. 确认月付为 0 后点击“立即订购”；
8. 进入订单详情并返回订单号。

默认站点为 `https://web1.52pokemon.cc`。服务器管理页的“购买网站地址”可以随时
切换域名，触发 Actions 时会自动传入；手动运行 Actions 也可以覆盖 `base_url`，
不需要修改购买脚本。不同域名可能加载不同版本的前端或安全验证，运行摘要会记录
本次实际使用的地址。

## 服务器触发与账号池

生产运行采用以下分工：

- `pool.db`：账号 ID、冷却时间、租约和运行结果；不保存邮箱、密码、Cookie、Token 或兑换码；
- GitHub Secret `POKEMON_ACCOUNTS_JSON`：账号凭据；
- 服务器管理页：保存当月兑换码；
- GitHub Actions：领取兑换码、选择可用账号并执行任务；
- Actions 产物：成功、dry-run 和失败时都会上传浏览器截图；
- 服务器账号池页面：查看账号状态、修改冷却日期、手动恢复为可用；
- 服务器账号池页面可以从 `POKEMON_ACCOUNTS_JSON` 同步账号 ID；
- 每月一号：WxPusher 提醒更新兑换码，不自动下单。

详细部署步骤见 [DEPLOY.md](DEPLOY.md)。

账号池页面的路径是 `/muyu-pokemon/admin/accounts/`，需要先登录管理页。账号
密码仍只放在 GitHub Secret 中；页面只管理非敏感状态和冷却日期。

## 本地使用

在 PowerShell 中安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

设置账号、兑换码和 Chrome 路径：

```powershell
$env:POKEMON_EMAIL = '你的登录邮箱'
$env:POKEMON_PASSWORD = '你的密码'
$env:POKEMON_REDEEM_CODE = '当前兑换码'
$env:POKEMON_CHROME_BINARY = 'D:\path\to\chrome.exe'
$env:POKEMON_CHROMEDRIVER = 'D:\path\to\chromedriver.exe'
.\.venv\Scripts\python.exe purchase.py --non-interactive
```

首次验证建议先运行检查模式，确认月付为 0 后不会点击下单：

```powershell
.\.venv\Scripts\python.exe purchase.py --non-interactive --dry-run
```

默认每个页面操作后等待 1.5 秒。可调整登录等待和操作间隔：

```powershell
$env:POKEMON_LOGIN_WAIT = '300'
$env:POKEMON_STEP_DELAY = '2'
```

## 账号池命令

```powershell
python pool.py --db pool.db --init
python pool.py --db pool.db --stats
```

账号凭据格式：

```json
[
  {"id": "acct-01", "label": "账号一", "email": "登录邮箱", "password": "登录密码"}
]
```

`id` 必须稳定且唯一。账号成功使用后进入 30 天冷却；失败只标记结果，不写回解密后的凭据。

## 安全边界

- 不把账号、密码、Cookie、Token 或兑换码写入仓库。
- 不绕过验证码；无头运行遇到验证码时账号会标记为 `needs_attention`。
- 如果站点显示滑块/人机安全验证，GitHub Actions 会以
  `security_verification_required` 快速停止；需要可见浏览器或已通过验证的
  会话才能继续。
- 首页公告遮罩和套餐导航遇到 Vue/Vuetify 重绘导致的 stale element 时会重新
  查找节点并重试，避免因为浏览器节点失效直接终止。
- 套餐入口连续被弹窗拦截时，会尝试直接打开 `/plan` 路由作为回退。
- 首页“套餐购买”导航无效时，会依次尝试“开通套餐”“购买套餐”入口。
- 套餐卡片在加载动画中重绘时会重新查找，避免 `stale element reference` 中断。
- 登录请求会识别 `passport/auth/login` 的 HTTP 500；默认最多连续提交 3 次，
  每次等待约 25 秒。失败时还会保存浏览器网络错误日志。
- 通用页面/DOM 错误会保持账号可重试；只有明确的账号、密码或验证码问题才会
  标记为 `needs_attention`。
- 每次 Actions 运行会在 `pokemon-screenshots-<run_id>` 产物中保留页面截图，便于排查登录或网络问题。
- 为适配站点当前证书异常，Chrome 启用了证书错误忽略参数；这仅作用于脚本自己的浏览器配置。
- 实际下单前以订单页金额为准；`--dry-run` 不会点击“立即订购”。
