# 门店助手部署指引（单机 Docker + PostgreSQL）

本文适用于一台 Linux 服务器，用 Docker Compose 同时运行一个 Octop 实例和一个 PostgreSQL 实例，并由 Nginx/同类 HTTPS 反向代理对外提供服务的首版部署。域名、证书、镜像标签和目录请替换为实际值。这里仅描述部署步骤，不会自动发布代码或迁移本地测试数据。

## 0. 发布前确认

- **使用本项目的定制源码或由它构建的镜像**。公司登录、门店选择器和 `xm-store` MCP 是本项目改动，不在上游 Octop 镜像/PyPI 包中。须让服务器检出已发布的本项目提交，或使用从该提交构建的镜像；不要拉上游 `latest` 代替。
- 不要把本地 `/tmp/xm-oct-local-test.*` 测试目录搬到服务器；它含测试管理员和不可用的 `local-mock` 模型。
- 服务器应能访问公司账号接口 `https://digital.yujianxiaomian.com`、门店接口 `https://app-container.yujianxiaomian.com` 和选定模型服务的地址；确认所需的 DNS、出口路由、证书链和白名单。
- 准备仅供门店助手使用的 HTTPS 域名和证书；只对外开放 443（以及签发证书/跳转所需的 80）。Octop 的 8088 端口只绑定服务器回环地址。
- Compose 将创建**专用 PostgreSQL 数据库与应用账号**。不要复用已有业务库；如需向量能力，可在该数据库中启用镜像自带的 `vector` 扩展。
- 首版仍保持**单个 Octop 实例**。PostgreSQL 解决控制面和 Agent 内存的持久化问题，但不能仅凭换库就认为 Agent 运行态支持多实例横向扩容。

## 1. 构建和交付镜像

在已检出**包含门店改动的发布提交**的源码根目录，运行：

```bash
docker build -f docker/Dockerfile -t xm-oct:store-v1 .
```

构建完成后，用 `docker image inspect xm-oct:store-v1` 确认镜像存在。推荐在 CI 中用发布提交构建、标记不可变版本，再推送到公司私有镜像仓库；如果在服务器本机构建，也必须确认服务器检出的正是该发布提交。后文的 `xm-oct:store-v1` 只是示例标签，正式环境请换成可追溯到提交的标签或镜像摘要。

构建前建议在同一提交上通过 `make all` 和 `cd dashboard && npx tsc -b`。本项目 Dockerfile 会把当前 `dashboard/` 源码编译进镜像；开发用的 Vite 服务（如本地 `15173`）不需要部署。

## 2. 准备 PostgreSQL、数据目录和 Compose 文件

Octop 使用 PostgreSQL 后，控制面和新 Agent 的内存默认使用同一数据库（Agent 各自使用独立 schema）。现有 SQLite 数据**不会自动迁移**到 PostgreSQL，不能把本地测试目录当作正式数据导入。

在服务器创建仅运维人员可读写的持久化目录 `/srv/xm-oct/data` 和部署目录 `/srv/xm-oct/deploy`。前者保存 Agent 工作区、配置、JWT/连接器加密密钥等文件；PostgreSQL 数据保存在下方 Compose 的 `pg_data` 持久化卷。**首次初始化前保持 `/srv/xm-oct/data` 和 `pg_data` 均为空**。

在 `/srv/xm-oct/deploy/.env` 中设置强随机数据库密码，并将文件权限设为 `0600`：

```dotenv
OCTOP_PG_PASSWORD=替换为至少32位的随机字母数字密码
```

例如可用 `openssl rand -hex 32` 生成密码；不要使用示例值，也不要把 `.env` 加入 Git、贴到工单或聊天中。Compose 只用这个文件做变量替换，密码会分别传给数据库和应用容器；能读取 `.env` 或容器环境变量的人员应视同拥有数据库凭证。同机容器通过 Compose 私有网络连接，不需要远程数据库部署使用的 `postgres.env` 或 `pg-root.crt`。这不影响浏览器访问所需的 HTTPS。

在 `/srv/xm-oct/deploy/compose.yaml` 保存以下配置：

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    environment:
      POSTGRES_USER: xm_oct
      POSTGRES_DB: xm_oct
      POSTGRES_PASSWORD: ${OCTOP_PG_PASSWORD:?请在 .env 中设置数据库密码}
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U xm_oct -d xm_oct"]
      interval: 5s
      timeout: 5s
      retries: 10

  xm-oct:
    image: xm-oct:store-v1
    container_name: xm-oct
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    # 绕过镜像默认入口脚本：它用本地 octop.db 文件判断首启，不适用于 PostgreSQL。
    entrypoint: ["octop"]
    command: ["run", "--host", "0.0.0.0", "--port", "8088"]
    ports:
      - "127.0.0.1:8088:8088"
    environment:
      HOME: /data
      OCTOP_BIND_HOST: 0.0.0.0
      OCTOP_PORT: "8088"
      OCTOP_LOG_LEVEL: info
      OCTOP_ADMIN_USERNAME: admin
      OCTOP_DATABASE_DRIVER: postgresql
      OCTOP_DATABASE_HOST: postgres
      OCTOP_DATABASE_PORT: "5432"
      OCTOP_DATABASE_NAME: xm_oct
      OCTOP_DATABASE_USER: xm_oct
      OCTOP_DATABASE_PASSWORD: ${OCTOP_PG_PASSWORD:?请在 .env 中设置数据库密码}
    volumes:
      - /srv/xm-oct/data:/data/.octop

volumes:
  pg_data:
```

PostgreSQL 没有配置宿主机端口映射，只允许同一 Compose 网络中的 Octop 访问；不要额外开放 5432。这里使用独立 Compose 文件，是为了限定公网暴露范围、持久化位置和 PG 启动方式；仓库自带的 `docker/docker-compose.yml` 默认把 8088 绑定到所有网卡，且镜像默认入口脚本以本地 `octop.db` 是否存在判断首启。PG 模式若不绕过该脚本，容器重启会再次尝试 `octop init`。不要把公司账号密码、公司 token 或模型 API Key 写进 Compose 文件。模型服务优先在管理员页面配置。

先在部署目录执行 `docker compose config -q`；它只校验配置，不向终端输出含数据库密码的展开结果。**仅首次部署、且 PostgreSQL 卷和 Octop 数据目录均为空时**，先启动数据库，再执行一次初始化。以下命令在交互终端输入初始管理员密码，不把密码写进 Compose 文件或命令参数；密码必须符合项目策略（至少 8 位且包含字母和数字）：

```bash
docker compose config -q
docker compose up -d postgres
read -r -s -p "Initial admin password: " OCTOP_ADMIN_PASSWORD; echo
export OCTOP_ADMIN_PASSWORD
docker compose run --rm --no-deps --entrypoint octop -e OCTOP_ADMIN_PASSWORD xm-oct init --yes --admin-username admin
unset OCTOP_ADMIN_PASSWORD
```

不要对已经有管理员或业务数据的数据库重复运行 `octop init`，也不要使用 `--force`。随后启动：

```bash
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8088/api/health
```

确认健康检查响应中 `db: true`，然后用刚设置的本地管理员账号登录并立即修改密码。此 PG 部署方式**不会**自动生成 `/data/.octop/credential.txt`；初始密码应存入公司的凭据管理系统，不要贴到聊天、工单或构建日志。

## 3. 配置 HTTPS 反向代理

示例 Nginx 配置如下；将 `assistant.example.com` 和证书路径替换为实际值。`map` 放在 Nginx 的 `http` 上下文，两个 `server` 块也放在 `http` 上下文。若公司网关已提供 TLS 和 WebSocket 代理，按同等配置接入即可。

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name assistant.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name assistant.example.com;
    ssl_certificate     /etc/letsencrypt/live/assistant.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/assistant.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }
}
```

启用前执行 `nginx -t`，再按公司的变更流程重载 Nginx。浏览器访问 `https://assistant.example.com`；聊天使用 WebSocket，反向代理不能只转发普通 HTTP。按公司网络策略限制来源 IP 或接入已有的访问控制。

**密码与日志：** 浏览器向 Octop 发送的是 HTTPS POST；但现有公司登录接口要求 Octop 在向上游发起的 POST 请求**查询参数**中传递密码。因此还必须确认公司网关、出口代理、链路追踪和访问日志不会记录完整上游 URL/查询参数，也不能记录登录请求体。服务端已有 HTTPX 登录 URL 脱敏过滤器，但它不能替代整个链路的日志治理。如上游以后提供请求体传密码的接口，应优先迁移。

## 4. 管理员初始化门店助手

1. 用本地管理员账号登录，进入「管理 → 模型」，配置真实模型服务商、API Key、Base URL 和模型 ID，并设置可用的默认模型。不要沿用本地测试的 `local-mock`。
2. 创建或配置「门店助手」Agent，选择该模型并开启**共享**，确认 Agent 状态为运行中。公司账号自动成为普通 Octop 用户；共享 Agent 可供多人使用，而会话仍按用户隔离。不要把管理员的私人 Agent/工作区当作门店助手共享。
3. 公司账号使用登录页默认的公司账号入口登录。登录成功进入聊天页后，页面应立即请求 `GET /api/xm-store/stores` 并显示该账号有权限的门店；无须先发送消息。选中门店后，后端通过 `PUT /api/xm-store/selection` 校验并保存选择。
4. 在对话中询问“我有权限的门店有哪些”时，可使用只读 MCP 工具 `list_my_stores`；选择器初始化**不**调用 MCP。当前首版尚未接入具体门店业务查询/写入工具，不能把选中门店误当成已经作用于所有 Agent 回答。

## 5. 上线验收

- 管理员确认模型可正常回复，Agent 为运行中；浏览器控制台没有持续的 WebSocket 连接错误。
- 用两个不同的公司测试账号分别登录：各自只看到本人有权限的门店，各自的会话列表互不串用。切换会话时门店选择应恢复；新会话仅沿用仍有权限的上次选择。
- 用测试账号验证门店列表故障显示“重试”，公司凭证失效会回到登录页；无可用门店时不执行需要门店上下文的工具。
- 在后端或网关验证伪造门店 ID 会被拒绝。不要用修改浏览器页面选项来代替服务端权限校验。
- `curl -fsS https://assistant.example.com/api/health` 返回 `ok: true`；确认公网不能直接访问服务器的 8088 端口。

## 6. 备份、升级与回滚

需要**配套备份 PostgreSQL 数据库和 `/srv/xm-oct/data` 目录**：数据库含用户、会话索引、门店选择、加密连接器凭证及默认 Agent 内存；本地目录含工作区、配置、JWT/加密密钥等文件。只备份其中一边，恢复后可能无法登录、解密或读取会话。至少安排每日备份、异机保存和定期恢复演练。备份文件须加密并限制访问。Compose 的 `pg_data` 卷也必须持久保留；`docker compose down -v` 会删除该卷，不能用于日常停机或升级。

Octop 自带的 `octop backup create` 支持 PG，但当前 Octop 镜像**没有安装 `pg_dump`/`pg_restore`**，因此不要直接依赖 Octop 容器内该命令做 PG 备份。可使用同机 PostgreSQL 容器自带的 `pg_dump`，并配套保存本地数据目录。详细的备份和恢复策略仍应符合公司规范。

升级前只停止 Octop 应用，保持数据库容器运行，取得同一维护窗口内的 PG 备份和本地目录快照，再换镜像。以下命令在部署目录运行，数据库密码不会出现在命令参数中：

```bash
docker compose stop xm-oct
umask 077
docker compose exec -T postgres pg_dump -U xm_oct -d xm_oct -Fc > /srv/xm-oct-db-before-upgrade.dump
tar -C /srv/xm-oct -czf /srv/xm-oct-data-before-upgrade.tar.gz data
# 将 compose.yaml 中的 image 改为已验证的新版本标签
docker compose up -d
curl -fsS http://127.0.0.1:8088/api/health
```

确认导出命令成功且备份文件非空后，把两份备份转移到受控位置，不要长期放在服务器根目录。升级可能自动执行数据库迁移，**回滚必须同时恢复旧镜像、升级前的 PG 数据库和配套的数据目录快照**；不能只换回旧镜像，也不能把新数据库与旧密钥目录混用。建议先恢复到隔离环境验证，再按公司的回滚审批流程切换正式服务。

相关能力与限制另见 [门店助手接入说明](xm-store-assistant.md)、[数据库配置](configuration.md#agent-memory-vs-control-plane)、[Docker 说明](../docker/README_CN.md) 和 [备份命令](cli.md#octop-backup)。
