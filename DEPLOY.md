# 52Pokemon 部署说明

## GitHub 私有仓库

新建私有仓库 `Augfif/pokemon_purchase`，默认分支使用 `main`，将本目录内容作为仓库根目录。不要使用公开的 `chaoxing_bot` 仓库保存 `pool.db`。

添加以下 Actions Secrets：

| Secret | 用途 |
| --- | --- |
| `POKEMON_ACCOUNTS_JSON` | 账号凭据 JSON 数组 |
| `POKEMON_COUPON_URL` | `https://18364454.xyz/muyu-pokemon/api/coupon` |
| `POKEMON_COUPON_TOKEN` | 服务器兑换码接口 Token |
| `WXPUSHER_SPT` | 每月一号提醒 |

服务器侧的 `GITHUB_TRIGGER_TOKEN` 使用细粒度 PAT，授予该私有仓库的
Actions 读写、Contents 读写权限；Actions 写权限用于触发购买，Contents
写权限用于从管理页修改 `pool.db`。`POKEMON_COUPON_TOKEN` 与服务器
`.env` 中的 `COUPON_API_TOKEN` 必须一致。

## 服务器目录

业务文件只放在：

```text
/www/wwwroot/muyu/pokemon_purchase/
  releases/<时间>-<commit>/
  shared/config/.env
  shared/data/
  shared/logs/
  archive/
  current -> releases/...
```

首次部署生成的随机管理员密码和触发链接分别保存在
`shared/config/muyu-admin-password.txt` 和
`shared/config/muyu-trigger-url.txt`，权限均为 `600`。

现有 `/www/wwwroot/muyu` 下的其他项目不修改、不删除。

Docker 服务只监听本机端口 `127.0.0.1:18082`。Nginx 使用现有 `18364454.xyz` 站点的扩展配置，将 `/muyu-pokemon/` 反向代理到该端口；不修改原有 server 块。

## 首次上线

1. 在服务器创建版本目录、`shared` 目录和 `.env`。
2. 在 `.env` 设置 `APP_SECRET_KEY`、`ADMIN_PASSWORD_HASH`、`TRIGGER_SLUG`、`COUPON_API_TOKEN`、`GITHUB_TRIGGER_TOKEN`、`GITHUB_OWNER=Augfif`、`GITHUB_REPO=pokemon_purchase`。
3. 构建 Docker 镜像并启动容器，使用 `-v /www/wwwroot/muyu/pokemon_purchase/shared:/app/shared` 挂载数据目录。
4. 创建 Nginx include 引用，执行 `nginx -t` 后 reload。
5. 打开管理页填写兑换码。
6. 在管理页“购买网站地址”填写当前可用域名，默认
   `https://web1.52pokemon.cc`；服务器触发和手动 Actions 都会使用该地址。
7. 打开 `/admin/accounts/` 添加账号 ID，并设置可用/冷却日期。
   如果账号已经在 `POKEMON_ACCOUNTS_JSON` 中，点击“从 GitHub Secret 同步
   账号 ID”，等待 `Sync Account Pool` 完成即可。
8. 在 GitHub Actions 手动运行一次 `dry_run=true`。
9. 检查截图和 `pool.db` 后，再由触发页执行真实任务。

## 运行规则

- 成功下单：账号进入 30 天冷却。
- 登录、验证码或凭据错误：账号标记为 `needs_attention`，不消耗冷却。
- 站点临时故障：账号释放为 `available`，下次可以重试。
- 每月一号北京时间 08:05：WxPusher 提醒更新兑换码，不自动下单。
- 成功、dry-run 和失败都会上传 `pokemon-screenshots-<run_id>` 截图产物；截图只保留在私有 Actions 产物中，不提交到仓库。
- 首页遮罩关闭和套餐入口点击遇到 Vue/Vuetify 重绘导致的
  `Node with given id does not belong to the document` 时会重新定位并重试。
- 套餐入口连续被公告弹窗拦截时，会自动尝试直接访问 `/plan`。
- 左侧“套餐购买”导航没有进入页面时，会尝试首页的“开通套餐”和“购买套餐”按钮。
- 套餐卡片因加载动画重绘时，会重新定位卡片和价格元素并重试。
- 登录接口 `api123.136470.xyz/api/v1/passport/auth/login` 返回 500 或页面未跳转时，
  默认会重新提交登录 3 次；浏览器网络错误日志会随截图一起上传到私有产物。
- GitHub Actions 使用无头 Chrome；遇到站点滑块、人机验证或验证码时会快速停止，
  不会尝试绕过。此时需要改用带可见浏览器的服务器 Runner，或提供已通过验证的
  会话。
- 截图和日志不输出兑换码和账号密码。
