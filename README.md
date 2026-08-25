# 海康访客批量创建、权限组关联与设备下发

本项目当前的执行入口是 [`api_register_and_add_group.py`](api_register_and_add_group.py)。脚本通过 HikCentral Artemis API 批量创建访客，将访客加入指定门禁权限组，并把权限下发到指定门禁设备。

> **注意：该脚本会调用真实的海康接口。下发最终失败时，脚本会自动解除权限组、将访客签退并删除人员。运行前请先核对批次范围、时间、权限组和门禁编号。**

## 运行环境

- Python 3.9 或更高版本
- 可访问 HikCentral Artemis API 的网络环境
- Python 包 `requests`

安装依赖：

```powershell
python -m pip install requests
```

## 配置 API 凭据

复制示例配置：

```powershell
Copy-Item config\config.example.json config\config.json
```

编辑 `config/config.json`：

```json
{
  "AccessKey": "你的 AccessKey",
  "SecretKey": "你的 SecretKey",
  "APIbaseUrl": "https://你的-hikcentral-地址"
}
```

`config/config.json` 已被 `.gitignore` 排除，不会提交到 Git。`APIbaseUrl` 末尾不要添加 `/`。

## 设置访客基础资料

脚本直接读取项目根目录下的 `registration_payload.example.json`。运行前请修改其中的访客姓氏、邮箱、公司、访问事由等基础资料。

以下字段会在运行时被脚本覆盖，无需逐条手工修改：

- `visitStartTime`
- `visitEndTime`
- `visitorGivenName`
- `certificateNo`
- `cards[0].cardNo`

## 设置运行参数

打开 `api_register_and_add_group.py`，在文件顶部确认以下常量：

| 常量 | 用途 | 当前值 |
| --- | --- | --- |
| `PRIVILEGE_GROUP_ID` | 海康门禁权限组 ID | `34` |
| `DOOR_INDEX_CODE` | 接收权限的门禁设备 ID | `56` |
| `VISIT_START_TIME` | 访问开始时间，包含时区 | `2026-08-24T23:00:00+09:00` |
| `VISIT_END_TIME` | 访问结束时间，包含时区 | `2026-12-31T23:59:59+09:00` |
| `BATCH_START` | 批次起始编号，包含该编号 | `3300` |
| `BATCH_STOP` | 批次结束编号，不包含该编号 | `BATCH_START + 10` |
| `MAX_ATTEMPTS` | 权限组关联和设备下发的最大尝试次数 | `4` |
| `RETRY_INTERVAL_SECONDS` | 步骤间隔、重试及下发结果查询等待秒数 | `20` |

当前设置会处理 `3300` 到 `3309`，共 10 名访客。第一个编号自动生成：

```text
visitorGivenName = apitest0203300
certificateNo    = 203300
cardNo           = 303300
```

再次运行相同批次可能产生重复访客或遇到证件号、卡号冲突。运行前应确认编号范围尚未使用。

## 运行

在项目根目录执行：

```powershell
python api_register_and_add_group.py
```

脚本会在控制台输出当前访客、接口结果、重试次数、设备下发状态和清理结果。

## 处理流程

对批次中的每个编号，脚本依次执行：

1. 生成访客姓名、证件号和卡号。
2. 调用 `/artemis/api/visitor/v1/registerment` 创建访客预约。
3. 从返回结果读取 `visitorId` 和 `appointRecordId`。
4. 等待 `RETRY_INTERVAL_SECONDS` 秒后，调用 `/artemis/api/acs/v1/privilege/group/single/addPersons` 加入权限组，失败时最多尝试 `MAX_ATTEMPTS` 次。
5. 权限组处理完成后再次等待 `RETRY_INTERVAL_SECONDS` 秒，再调用 `/artemis/api/visitor/v1/auth/reapplication` 触发设备下发。
6. 等待 `RETRY_INTERVAL_SECONDS` 秒后，调用 `/artemis/api/visitor/v1/person/ID/elementDownloadDetail` 查询结果。
7. 只有查询到至少一个 `elementStatus`，且所有状态均为 `0`，才判定下发成功。

访客登记接口不会自动重试：

- 遇到网络错误、HTTP 错误或登记结果不确定时，脚本会停止整个批次，避免重复创建访客。
- 遇到明确的海康业务错误时，脚本会跳过当前编号，继续处理下一个编号。
- 登记成功但响应缺少 `visitorId` 或 `appointRecordId` 时，脚本会停止整个批次。

## 失败清理

权限组关联失败，或设备下发在全部尝试后仍未成功时，脚本会依次执行：

1. 从权限组中移除访客。
2. 使用 `appointRecordId` 将访客签退。
3. 使用 `visitorId` 删除人员资源。

三个清理动作相互独立：其中一个失败不会阻止后续清理动作。请查看控制台输出，确认每项清理是否成功。

## TLS 提醒

当前代码使用 `verify=False`，不会验证 HikCentral 服务端的 TLS 证书。这适用于现有私有证书环境，但无法防止中间人攻击。若服务器已经配置受信任证书，应将代码改为启用证书验证。

## 旧版代码

`old/` 目录保存之前的单访客处理及测试代码，不是当前批量任务的执行入口。
