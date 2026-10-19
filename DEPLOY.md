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

服务器侧的 `GITHUB_TRIGGER_TOKEN` 使用细粒度 PAT，只授予该私有仓库的 Actions 写权限。`POKEMON_COUPON_TOKEN` 与服务器 `.env` 中的 `COUPON_API_TOKEN` 必须一致。

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

现有 `/www/wwwroot/muyu` 下的其他项目不修改、不删除。

Docker 服务只监听本机端口 `127.0.0.1:18082`。Nginx 使用现有 `18364454.xyz` 站点的扩展配置，将 `/muyu-pokemon/` 反向代理到该端口；不修改原有 server 块。

## 首次上线

1. 在服务器创建版本目录、`shared` 目录和 `.env`。
2. 在 `.env` 设置 `APP_SECRET_KEY`、`ADMIN_PASSWORD_HASH`、`TRIGGER_SLUG`、`COUPON_API_TOKEN`、`GITHUB_TRIGGER_TOKEN`、`GITHUB_OWNER=Augfif`、`GITHUB_REPO=pokemon_purchase`。
3. 构建 Docker 镜像并启动容器，使用 `-v /www/wwwroot/muyu/pokemon_purchase/shared:/app/shared` 挂载数据目录。
4. 创建 Nginx include 引用，执行 `nginx -t` 后 reload。
5. 打开管理页填写兑换码。
6. 在 GitHub Actions 手动运行一次 `dry_run=true`。
7. 检查截图和 `pool.db` 后，再由触发页执行真实任务。

## 运行规则

- 成功下单：账号进入 30 天冷却。
- 登录、验证码或凭据错误：账号标记为 `needs_attention`，不消耗冷却。
- 站点临时故障：账号释放为 `available`，下次可以重试。
- 每月一号北京时间 08:05：WxPusher 提醒更新兑换码，不自动下单。
- 失败截图和日志只保留在私有产物或服务器目录，不输出兑换码和账号密码。
