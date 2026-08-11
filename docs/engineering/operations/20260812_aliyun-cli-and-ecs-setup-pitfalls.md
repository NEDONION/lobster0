# 阿里云 CLI 与 ECS 服务器配置踩坑记录

日期：2026-08-12
范围：`aliyun` CLI 初始化、RAM 子用户与授权、ECS 实例从零到可用的常见坑

> **与 [实机部署踩坑实录](../../getting-started/20260811_实机部署踩坑实录.md) 的分工**：那份是**腾讯云轻量 + Ubuntu 24.04** 上把 Lobster0 真正跑起来的现场记录（sudo PATH、clone GitHub、换源装依赖）；本文是**阿里云**侧、「服务器能 SSH 之前」的那一段（凭证、授权、安全组、公网、磁盘）。两者是接力关系——本文结束的地方就是那份开始的地方。

---

## 一、`aliyun configure` 初始化

### 坑 1：四个字段一个都不能留空

交互式 `aliyun configure` 会依次问四项，**前三项留空回车会直接失败**，而且失败信息是分两步暴露的，很容易误以为第一次就配好了：

```
Access Key Id []:            ← 留空 → AccessKeyId/AccessKeySecret is empty!
Default Region Id []:        ← 留空 → default RegionId is empty!
```

更坑的是它会先打印 `Saving profile[default] ...Done.`，**再**打印 `Configure Failed`。看到 Done 就以为成功是典型误判，一定要看最后几行。

正确的完整交互：

| 提示 | 填什么 |
|---|---|
| Access Key Id | `LTAI` 开头的一串 |
| Access Key Secret | 创建 AccessKey 时**只显示一次**的那串 |
| Default Region Id | 见下面的地域表，不确定填 `cn-hangzhou` |
| Default Output Format | 只支持 `json`，直接回车 |
| Default Language | `zh` 或 `en` |

### 坑 2：RegionId 填什么，以及它其实没那么重要

`Default Region Id` 只是**默认值**，不影响鉴权，任何命令都能用 `--region` 覆盖。所以卡在这一步纠结是没必要的。

控制台顶部导航栏显示的地域名 → RegionId 对照：

| 控制台显示 | RegionId |
|---|---|
| 华东1（杭州） | `cn-hangzhou` |
| 华东2（上海） | `cn-shanghai` |
| 华北1（青岛） | `cn-qingdao` |
| 华北2（北京） | `cn-beijing` |
| 华北3（张家口） | `cn-zhangjiakou` |
| 华南1（深圳） | `cn-shenzhen` |
| 中国香港 | `cn-hongkong` |
| 新加坡 | `ap-southeast-1` |
| 美国（硅谷） | `us-west-1` |

规律：拼音 + 连字符，`cn-` 前缀是中国内地。

配好之后查全量列表：

```bash
aliyun ecs DescribeRegions
```

### 坑 3：配完不验证，等到真跑业务命令才发现不通

`Configure Done!!!` 只代表**写入配置文件成功**，不代表凭证有效。务必立刻做一次真实 API 调用验证：

```bash
aliyun sts GetCallerIdentity
```

正常返回长这样，重点看 `IdentityType` 和 `Arn` 是不是你以为的那个身份：

```json
{
  "AccountId": "1245443925243536",
  "Arn": "acs:ram::1245443925243536:user/power-application-user",
  "IdentityType": "RAMUser"
}
```

只想看本地配置（不发请求）用：

```bash
aliyun configure list
```

---

## 二、RAM 子用户与权限

### 坑 4：以为「有 AccessKey」就等于「有权限」

`GetCallerIdentity` 能通，只说明这把钥匙是**有效的**，不说明它**能干什么**。这次实测就撞上了：身份验证通过，但连查自己挂了哪些策略都被拒。

```
ErrorCode: NoPermission
Action: ram:ListPoliciesForUser
NoPermissionType: ImplicitDeny
```

`ImplicitDeny`（隐式拒绝）= 没有任何策略授予过这个动作，不是被显式 Deny 挡了。判断路径：

- **ImplicitDeny** → 少授权，去加策略
- **ExplicitDeny** → 有策略明确禁止，要么改策略，要么换身份

想让子用户能自查权限，需要额外授 `AliyunRAMReadOnlyAccess`。否则只能用主账号在控制台看。

### 坑 5：`AdministratorAccess` ≠ 主账号

想让子用户「权限和主账号差不多」，标准做法是挂系统策略 `AdministratorAccess`。但它只覆盖**云资源**，下面这些照样不行：

| 能力 | 说明 |
|---|---|
| 费用中心 / 账单 | 需主账号先在 *费用中心 → 用户设置* 打开「允许 RAM 用户查看账单」开关，**再**授 `AliyunBSSFullAccess` / `AliyunBSSReadOnlyAccess`。少任何一步都不行 |
| 提交工单 | 同样是「开关 + 授权」两步，策略是 `AliyunSupportFullAccess` |
| 实名认证、改账号手机号/密码、注销账号 | 永久只能主账号，无法委派 |

「开关 + 授权」这个双重门是最容易漏的一环——只授策略不开开关，报错还是 NoPermission，很容易误判成策略写错了。

用 CLI 建用户并授权（前提：当前身份有 RAM 写权限）：

```bash
aliyun ram CreateUser --UserName cli-admin --DisplayName cli-admin
```

```bash
aliyun ram AttachPolicyToUser --PolicyType System --PolicyName AdministratorAccess --UserName cli-admin
```

```bash
aliyun ram CreateAccessKey --UserName cli-admin
```

最后一条会把 AccessKeySecret 直接打到 stdout，注意别进日志、别进 shell history。

### 坑 6：AccessKey 泄露的处置

AccessKey Secret 一旦离开你的终端（粘进聊天、贴进 issue、commit 进仓库、打进日志），就必须按**已泄露**处理：

1. RAM 控制台 → 用户 → AccessKey 管理 → **先禁用**（保留观察窗口）
2. 换上新 AccessKey，确认业务正常
3. **删除**旧的
4. 如果这把钥匙有 `AdministratorAccess`，顺带去操作审计（ActionTrail）翻一遍这段时间的调用记录

顺序上先禁用后删除，是为了万一有你忘了的服务在用它，禁用后能从报错里定位到，删掉就查不回来了。

### 坑 7：本机长期存明文 AK

`aliyun configure` 把凭证明文写在 `~/.aliyun/config.json`。至少做一次：

```bash
chmod 600 ~/.aliyun/config.json
```

更好的替代方案，按场景选：

- **CI / 容器**：用环境变量，不落盘

  ```bash
  export ALIBABA_CLOUD_ACCESS_KEY_ID=xxx
  export ALIBABA_CLOUD_ACCESS_KEY_SECRET=xxx
  export ALIBABA_CLOUD_REGION_ID=cn-beijing
  ```

- **跑在 ECS 上**：用实例 RAM 角色，完全不需要长期密钥

  ```bash
  aliyun configure --mode EcsRamRole --profile ecs
  ```

- **多账号 / 多环境**：用 profile 隔离，避免「以为在测试环境结果打到生产」

  ```bash
  aliyun configure --profile prod
  ```

  之后所有命令带 `--profile prod`。这是最值得养成的习惯之一。

---

## 三、ECS 实例开出来之后

### 坑 8：安全组默认什么都不放行

新实例连不上，九成是安全组。它是**白名单**，默认入方向只开 22（Linux）或 3389（Windows），而且有些镜像/购买路径连 22 都不开。

排查顺序（按撞坑频率从高到低）：

1. **安全组入方向规则** —— 端口有没有开、授权对象是不是 `0.0.0.0/0` 或你的出口 IP
2. **实例内防火墙** —— CentOS 系的 `firewalld`、Ubuntu 的 `ufw` 会二次拦截，两层都得放行
3. **服务有没有监听在 `0.0.0.0`** —— 绑在 `127.0.0.1` 上，安全组开了也没用，用 `ss -lntp` 确认
4. **公网 IP / 带宽** —— 见下条

一个常被忽略的点：安全组规则**改完立即生效**，不用重启实例。如果改完还不通，说明问题不在这一层，别浪费时间反复重启。

### 坑 9：买了实例但没有公网 IP，或带宽是 0

按量付费实例如果创建时没勾「分配公网 IPv4 地址」，开出来就是纯内网机器。补救方式是绑定 EIP（弹性公网 IP），而不是重建。

另外「带宽峰值 0 Mbps」的实例等同于没有公网出口，账单上还照常收实例费——这个坑不报错，只是死活连不上。

### 坑 10：80 / 443 需要备案

**中国内地地域**的 ECS，对外提供 HTTP/HTTPS 服务必须完成 ICP 备案，否则 80/443 会被阻断。这跟安全组无关，改配置改不出来。

> **对 Lobster0 部署不适用。** 飞书渠道走 WebSocket **出站**长连接，程序主动连出去、不接受入站回调，因此不需要公网回调地址、域名、证书或备案，入方向只放行 SSH 22 即可（见[实机部署踩坑实录](../../getting-started/20260811_实机部署踩坑实录.md)的「防火墙」一节）。只有当你额外要把 Web 控制台暴露到公网时，这条才会咬人。

规避方式：
- 用非标准端口（如 8080）临时验证
- 把地域选在**中国香港**或海外，不需要备案
- 或者老老实实备案，周期通常是几天到几周

选地域时如果目标是对外服务，**这一条决定了地域选择**，比延迟更优先。

### 坑 11：默认系统盘小、无数据盘

系统盘常见默认 40GB，跑几个容器镜像就满了。`df -h` 变成日常习惯。扩容路径：控制台扩容云盘 → **进系统里还要执行文件系统扩容**（`growpart` + `resize2fs` / `xfs_growfs`），只在控制台点扩容是不生效的，这是最典型的「以为扩了其实没扩」。

### 坑 12：镜像源慢

内地 ECS 用默认的官方源会很慢，换成阿里云内网源立刻起飞（内网走不计流量费）：

- Ubuntu/Debian：把 `/etc/apt/sources.list` 换成 `mirrors.cloud.aliyuncs.com`（**内网**，仅 ECS 内可用）或 `mirrors.aliyun.com`（公网）
- pip / npm 同理换镜像

区分 `mirrors.cloud.aliyuncs.com`（内网、免流量费、只有 ECS 能访问）和 `mirrors.aliyun.com`（公网、走带宽计费）是省钱点。

### 坑 13：时区和 NTP

部分镜像默认 UTC，日志时间对不上，排查问题时会非常混乱。开机第一件事：

```bash
timedatectl set-timezone Asia/Shanghai
```

> **别跟另一个长得很像的坑搞混。** Lobster0 启动时报 `heartbeat.timezone must be a valid IANA timezone` **不是**系统时区没设，而是 uv 安装的 Python 不带时区数据库（已通过补 `tzdata` 依赖修复）。系统时区正确也照样会报——两者根因无关，见[实机部署踩坑实录](../../getting-started/20260811_实机部署踩坑实录.md)的「已知问题速查」。

### 坑 14：内存小的实例没有 swap

1C1G / 2C2G 的实例默认无 swap，编译或跑 Node/Java 时容易被 OOM Killer 直接杀进程，现象是「进程莫名消失、没有任何错误日志」。看 `dmesg | grep -i oom` 确认。加个 swap 文件能救命。

---

## 四、快速自检清单

新环境从零配起时按这个顺序走，能避掉上面绝大多数坑：

- [ ] RAM 建子用户，不用主账号 AccessKey
- [ ] 按需授权；若要自查权限，额外加 `AliyunRAMReadOnlyAccess`
- [ ] 涉及账单/工单的，记得「开关 + 授权」两步都做
- [ ] `aliyun configure` 四项全填，别留空
- [ ] `aliyun sts GetCallerIdentity` 验证，确认 `IdentityType` 和 `Arn` 符合预期
- [ ] `chmod 600 ~/.aliyun/config.json`
- [ ] 多环境用 `--profile` 隔离
- [ ] ECS：确认有公网 IP 且带宽 > 0
- [ ] ECS：安全组 + 实例内防火墙**两层**都放行
- [ ] ECS：确认服务监听 `0.0.0.0` 而非 `127.0.0.1`
- [ ] ECS：内地地域跑 80/443 前先确认备案状态
- [ ] ECS：设时区、换镜像源、按需加 swap
- [ ] 云盘扩容后记得在系统内扩文件系统
