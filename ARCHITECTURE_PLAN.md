# Azure Manager 重构架构方案

> 目标：将项目从“页面直接驱动 Azure API 的单体脚本”重构为“模块化单体 + 持久化任务 Worker + 快照缓存”的 Azure 运维平台。
>
> 本方案先解决边界、数据一致性和任务可靠性，再迁移页面。未获得明确实施授权前，不修改现有业务代码。

## 1. 重新定义系统职责

系统只负责四类事情：

1. 读取并展示 Azure 资源和能力快照。
2. 接收用户操作，生成可恢复的操作任务。
3. 由独立 Worker 执行 Azure 长任务，并持续记录真实状态。
4. 在后台同步公开价格、镜像、地域和规格目录。

页面请求不直接承担长时间 Azure 调用，也不负责建立价格目录。页面只读取本地已发布的数据，并提交任务。

### 明确不混合的概念

| 概念 | 来源 | 作用 |
|---|---|---|
| 规格能力 | Compute Resource SKUs API | 订阅/地域/Zone 的规格限制和能力 |
| VM 可调整规格 | `list_available_sizes` | 某台 VM 当前真正允许调整的目标 |
| 镜像目录 | Compute Gallery/Image API | 镜像 URN、架构、系统和版本 |
| 公开价格 | Retail Prices API | Azure 公共零售价，不代表订阅最终账单 |
| 实际账单 | Cost Management / Price Sheet | 订阅折扣、协议价和实际费用 |
| 资源操作状态 | Azure Resource API | 创建、开关机、调整规格等最终状态 |

规格、价格和操作状态不再通过一个接口或一个缓存对象互相覆盖。

## 2. 目标部署架构

采用模块化单体，而不是立即拆成微服务。这样保留 Flask 和现有页面的投入，同时把 Web 生命周期与 Azure Worker 生命周期分离。默认部署目标是 2 vCPU / 1 GiB 内存的单机。

```text
浏览器
  │ HTTPS
  ▼
反向代理
  │
  ├── Web：Flask 路由、鉴权、查询、提交任务
  ├── Worker：持久化任务、Azure 长操作、状态恢复
  ├── Scheduler：目录同步、VM 巡检、清理任务
  │
  ├── SQLite WAL：业务数据、快照、任务和事件（2c1g 单机模式）
  └── PostgreSQL / Redis：高并发或多实例模式的可选升级
          │
          ▼
      Azure Management APIs
      Azure Retail Prices API
      Azure Cost Management APIs
```

### 技术选择

- Web：继续使用 Flask，先拆分模块，不为了重写而更换框架。
- Worker：独立进程，2c1g 模式使用项目内置的单进程 DB 队列；不引入 Celery、RQ 等常驻组件。
- 数据库：2c1g 单机模式继续使用 SQLite WAL；数据量和并发增长后再迁移 PostgreSQL。
- 队列/锁：使用 SQLite 任务表、短事务、文件锁和租约；多实例模式再切换 Redis/PostgreSQL。
- 事件推送：优先 SSE，保留轮询作为降级方案。
- 数据库迁移：Alembic/Flask-Migrate，禁止继续依赖 `create_all` 兼容结构变化。

### 2c1g 资源预算

2c1g 模式只运行两个常驻进程：一个 Web 进程和一个 Worker 进程；调度器合并到 Worker 内，不单独启动。

| 进程 | 配置 | 目标内存 |
|---|---|---:|
| Web | Gunicorn 1 worker、2 threads | 180–260 MiB |
| Worker | 1 进程、并发 1 | 220–350 MiB |
| SQLite/WAL、系统和反向代理 | 单机文件和低频连接 | 150–250 MiB |
| 余量 | Azure SDK、临时响应、突发峰值 | 至少 150 MiB |

硬性约束：

- Web 和 Worker 不得各自启动一组线程池；Azure 操作全局并发上限为 1。
- Scheduler 不得作为第三个进程运行，只能由 Worker 的定时循环负责。
- Retail Prices 分页必须逐页处理和落库，不得把完整目录长期保存在内存。
- Resource SKU 响应、镜像响应和 Azure SDK 客户端不得写入无限增长的进程级缓存。
- 单机模式不支持多副本同时写入同一 SQLite 文件；需要扩容时必须先迁移 PostgreSQL。
- Docker 设置内存上限 768 MiB，并保留 256 MiB 给系统和反向代理；若机器可用内存不足，Worker 优先保证任务完成，暂停目录同步。

2c1g 模式牺牲吞吐换取稳定性：不同 VM 的操作排队执行，但任务不会因 Web 进程重启而丢失。

## 3. 模块边界

现有 `azure/app.py` 应逐步拆成以下模块：

```text
azure_manager/
├── web/
│   ├── auth_routes.py
│   ├── subscription_routes.py
│   ├── vm_routes.py
│   ├── catalog_routes.py
│   ├── operation_routes.py
│   └── error_handlers.py
├── domain/
│   ├── availability.py
│   ├── pricing.py
│   ├── images.py
│   ├── vm_operations.py
│   └── operation_state.py
├── providers/azure/
│   ├── clients.py
│   ├── compute.py
│   ├── pricing.py
│   ├── cost.py
│   └── errors.py
├── workers/
│   ├── operation_worker.py
│   ├── catalog_worker.py
│   └── scheduler.py
├── repositories/
├── models/
├── schemas/
└── app.py
```

规则：

- Web 层不能直接创建 Azure SDK Client。
- Provider 层不能写 Flask session 或渲染模板。
- Domain 层不能依赖具体的 Azure SDK 类型。
- Repository 层负责数据库读写，不在路由中拼接复杂查询。
- 每个外部调用必须返回统一结果或统一异常类型。

## 4. 数据模型重新设计

### 4.1 资源和订阅

保留账户和订阅概念，但所有 Azure 资源使用稳定的 `resource_id` 作为主业务键：

- `accounts`
- `subscriptions`
- `azure_resources`
- `virtual_machines`
- `network_interfaces`
- `public_ips`
- `disks`

缓存不再只依赖 VM 名称和资源组，避免重命名或跨资源组时产生错配。

### 4.2 能力快照

新增不可变快照模型：

- `catalog_snapshots`：订阅、地域、目录类型、开始时间、完成时间、状态、错误。
- `sku_capabilities`：snapshot_id、SKU、架构、能力、限制、Zone、Trusted Launch 兼容性。
- `image_catalog_entries`：snapshot_id、URN、OS、架构、版本和来源。
- `vm_resize_snapshots`：subscription、resource_id、当前 VM 上下文、目标规格和有效期。

新快照完整生成后才发布为 `active_snapshot_id`。刷新失败时继续使用上一份完整快照，不删除旧数据，不产生半套目录。

### 4.3 价格目录

价格不能再按订阅复制成页面专属缓存。拆成：

- `public_price_catalog`：地域、armSkuName、OS、计费模式、货币、meter/product、小时价、有效期、抓取时间。
- `price_sync_runs`：地域、游标、页数、状态、重试次数、最近错误、下次执行时间。
- `subscription_price_overrides`：未来接入 Price Sheet 或协议价格时保存订阅专属覆盖。

价格状态必须有明确枚举：

```text
fresh              已同步且在有效期内
stale              有旧价格但需要刷新
pending             尚未同步
temporarily_failed  Azure 接口超时或限流
no_public_price     已完成查询但没有匹配公开报价
```

`pending` 和 `no_public_price` 绝不能在页面上使用同一段文案。

### 4.4 操作任务和事件

将现在的单行 `DeploymentTask` 扩展为：

- `operations`：操作主记录、幂等键、目标 resource_id、状态、租约和重试信息。
- `operation_steps`：规划、校验、停止、更新、启动、确认等步骤。
- `operation_events`：每次进度、Azure request id、耗时和错误分类。
- `operation_locks`：按资源和订阅控制并发。
- `worker_leases`：Worker 心跳和任务租约。

操作状态：

```text
accepted → validating → running → reconciling → succeeded
                                      └────────→ failed
                                      └────────→ uncertain
```

Worker 重启后不能直接把任务写成“应用进程重启导致任务中断”。应进入 `reconciling`，重新读取 Azure 实际状态，再决定成功、失败或需要用户确认。

## 5. 价格系统设计

这是本项目最需要重做的部分。

### 5.1 页面请求绝不访问 Retail Prices

价格接口只读本地目录：

```text
GET /api/v1/catalog/sku?subscription_id=...&location=...
  → 返回规格快照 + 当前已知价格状态

GET /api/v1/catalog/prices?location=...&sku=...
  → 只读 public_price_catalog
```

首次没有价格时，页面仍然展示完整规格，价格标为 `pending`，同时创建一个幂等的后台同步请求。页面请求不会等待 Azure 价格接口。

### 5.2 后台价格同步

价格 Worker 采用以下规则：

1. 同一地域只能有一个活动刷新任务。
2. 同一地域的多个刷新请求合并为一个任务。
3. 支持单 SKU 精确查询；需要全量目录时在后台执行，不阻塞用户。
4. 每页处理后立即落库并保存 `NextPageLink`，中断后从游标继续。
5. 遇到 429 按 `Retry-After` 退避，不自行高频重试。
6. 连续超时或 429 触发地域级熔断，暂停一段时间并继续提供旧缓存。
7. 新数据完成校验后再发布，永远不因刷新失败清空旧目录。
8. Cloud Services、Windows、Spot、Reservation 等记录在入库时分类，不在页面临时猜测。

Azure Retail Prices API 返回分页结果，且支持地域、SKU、服务和价格类型过滤，因此应把它作为后台目录同步源，而不是页面数据源。[官方文档](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)

### 5.3 价格和创建校验

价格缺失不代表资源不能创建；规格可用性和价格可见性必须分开：

- 规格限制决定能否选择和提交。
- 价格状态决定如何展示费用提醒。
- 创建前重新校验规格和镜像兼容性。
- 创建请求不因公开价格接口暂时不可用而误判为“无价格”或“免费”。

## 6. Azure 操作设计

### 6.1 Web 只提交命令

所有创建、开机、关机、重启、删除、重装、换 IP、改规格操作统一流程：

1. 校验输入和幂等键。
2. 写入 `operations` 和初始事件。
3. 返回 HTTP 202 和 operation_id。
4. Worker 获取租约并执行。
5. 每个 Azure 调用前后写入事件和心跳。
6. Worker 结束后读取 Azure 实际状态，再发布结果。

页面只能通过 SSE 或轮询读取操作状态，不能依据提交响应猜测 Azure 已完成。

### 6.2 幂等和并发

- 同一 resource_id 同时只能有一个变更操作。
- 同一订阅可配置并发上限，避免 Azure 管理 API 被突发调用。
- 重复点击使用相同幂等键，返回已有 operation_id。
- Worker 租约超时后由恢复 Worker 接管，不允许两个 Worker 同时执行同一任务。
- 对创建操作使用客户端请求键和资源命名预留，避免重试生成重复 VM。

### 6.3 恢复策略

Worker 重启后的恢复流程：

1. 找出租约过期的操作。
2. 根据 resource_id 查询 Azure 当前状态。
3. 判断最后一步是否已在 Azure 生效。
4. 已生效则继续下一步，未生效且可安全重试才重试。
5. 无法判断时标记 `uncertain`，提示用户确认，不盲目重复操作。

## 7. 可用性和创建流程

创建页必须把以下阶段分开显示：

```text
地域目录 → 规格能力 → 镜像目录 → 价格状态 → 创建前校验 → 提交任务
```

每一层都有自己的 `source`、`updated_at`、`state` 和 `error_code`。

创建前校验至少包括：

- 订阅是否允许该地域和资源类型；
- SKU 是否包含当前地域/Zone 限制；
- 架构和镜像是否匹配；
- Trusted Launch/Security Type 是否匹配；
- Spot、网络加速、磁盘控制器等能力是否匹配；
- 最终以 Azure Create VM 返回结果为权威。

前端只展示 Azure 已确认的限制，不把“价格缺失”“暂时超时”和“订阅不可用”混为一谈。

## 8. API 规范

所有 API 统一 JSON 格式：

```json
{
  "request_id": "req_...",
  "status": "success",
  "data": {},
  "meta": {"source": "snapshot", "updated_at": "..."},
  "error": null
}
```

错误格式：

```json
{
  "request_id": "req_...",
  "status": "error",
  "data": null,
  "error": {
    "code": "PRICE_SYNC_PENDING",
    "message": "价格目录正在后台同步",
    "retryable": true
  }
}
```

约定：

- 查询成功但数据过期仍返回 200，并在 meta 标记 `stale`。
- 后台任务创建返回 202。
- 参数错误返回 400，未登录返回 401，无权限返回 403，资源不存在返回 404，冲突返回 409，Azure 暂时不可用返回 503。
- API 永远不返回 Flask HTML 错误页。
- 所有请求带 request_id，Azure request id 写入操作事件。

## 9. 前端设计

保留当前服务端渲染方向，但把页面 JavaScript 拆成小模块：

- `api_client.js`：统一 JSON、错误和登录过期处理。
- `catalog_state.js`：规格/镜像/价格状态机。
- `operation_stream.js`：SSE 和轮询降级。
- `vm_create.js`：表单组合和提交。
- `vm_detail.js`：操作按钮和任务展示。

页面状态不能只使用一个“加载中”：

- 规格：加载中、已完成、订阅不可用、请求失败；
- 价格：已同步、旧缓存、同步中、暂时失败、无公开价格；
- 镜像：已缓存、后台刷新、无匹配架构；
- 操作：已提交、执行中、恢复中、成功、失败、未知。

## 10. 安全、可观测性和运维

### 安全

- 正式环境使用 PostgreSQL，不将数据库和 WAL 文件作为普通下载文件处理。
- Azure Secret 优先放入 Docker Secret、环境密钥服务或 Azure Key Vault；应用数据库只保存引用或加密值。
- 将 Flask 会话密钥、数据库加密密钥和备份密钥分离，支持轮换。
- Service Principal 使用最小权限，账户变更和敏感操作写入审计日志。
- 日志禁止写出 token、client secret、完整请求头和密码。

### 可观测性

记录以下指标：

- Azure API 按服务/地域的请求量、延迟、成功率、429、5xx、超时；
- 价格目录同步进度、页数、游标、最后成功时间；
- Worker 队列深度、任务等待时间、租约过期数；
- 每个目录快照的年龄和状态；
- 创建/调整规格的最终成功率和 `uncertain` 数量。

设置健康检查：

- Web 存活；
- 数据库可写；
- Worker 心跳；
- Scheduler 最近运行时间；
- Azure 凭据仅在实际任务中验证，不因健康检查频繁调用 Azure。

## 11. 分阶段实施计划

### 阶段 0：冻结现状和建立契约

**目标**：停止继续向旧链路叠加补丁，记录现有行为和数据迁移策略。

**交付物**：架构决策记录、API 错误契约、数据库迁移基线、回滚方案。

**成功标准**：现有功能测试可重复；明确哪些旧字段和接口将在过渡期保留。

### 阶段 1：目录快照和价格读写分离

**目标**：规格、镜像和价格全部改为持久化快照；页面请求不再直接访问 Retail Prices。

**交付物**：catalog repository、price repository、后台同步任务、快照发布机制。

**成功标准**：Azure 价格接口不可用时，页面仍能加载规格并显示准确状态；不会清空旧价格。

### 阶段 2：独立 Worker 和任务恢复

**目标**：将所有长 Azure 操作移出 Gunicorn。

**交付物**：operations、steps、events、leases、幂等键、恢复 Worker、SSE/轮询接口。

**成功标准**：重启 Web 容器不会丢失任务；同一 VM 不会并发变更；恢复后能查询并纠正 Azure 实际状态。

### 阶段 3：拆分 Azure Provider 和领域服务

**目标**：移除路由中的 Azure SDK 调用和复杂数据库逻辑。

**交付物**：Azure provider、统一错误分类、availability/image/pricing/vm-operation domain services。

**成功标准**：服务层可用 mock 完整测试；页面路由只负责鉴权、输入、调用服务和返回协议。

### 阶段 4：2c1g 单机部署和可选扩展

**目标**：先完成 2c1g 单机模式，再提供 PostgreSQL + Redis 的扩展部署配置。

**交付物**：Web/Worker 双进程 Compose 配置、SQLite 迁移、备份恢复、内存限制、日志和指标；另提供 PostgreSQL/Redis profile。

**成功标准**：2c1g 运行 24 小时无 OOM；Web/Worker 可独立重启；备份可在干净环境恢复；旧数据不会丢失；升级 profile 后可支持多实例。

### 阶段 5：前端状态机和旧链路下线

**目标**：前端按目录状态和操作状态展示，不再使用多个页面内联脚本互相修改同一状态。

**交付物**：统一 API 客户端、目录状态组件、操作事件组件、旧接口兼容层下线。

**成功标准**：价格暂时失败不会显示成免费或无公开价格；页面不会因价格接口阻塞规格和镜像；所有操作进度可追踪。

## 12. 最终验收指标

- 页面首屏不等待 Retail Prices API，规格快照响应目标小于 1 秒。
- 价格接口限流时，规格和镜像仍可用，旧价格不被清空。
- 同一地域同一时间最多一个价格同步任务。
- 429 和超时能够自动退避、熔断，并保留可诊断错误。
- Web Worker 重启不会把未完成 Azure 操作直接判为失败。
- 所有长任务具备 operation_id、步骤、事件、Azure request id 和最终资源状态。
- 创建、调整规格和生命周期操作具备幂等性。
- API 错误统一为 JSON，浏览器不会再解析 HTML 为 JSON。
- 数据库升级、备份和恢复都有自动化测试。
- 2c1g 单机模式常驻内存低于 768 MiB，压力和任务恢复测试不触发 OOM。

## 13. 明确不采用的方案

- 不在页面请求中同步拉取地域全量价格目录。
- 不通过更换缓存版本号让所有价格同时失效。
- 不把“没有价格”“请求失败”“订阅不可用”统一显示为一个空值。
- 不继续依赖 Gunicorn Worker 内的线程执行 Azure 长任务。
- 不用增加重试次数解决 429；重试必须由队列、退避和熔断控制。
- 不在没有数据库迁移脚本的情况下继续修改生产表结构。
