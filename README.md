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
| `BATCH_START` | 批次起始编号，包含该编号 | `4000` |
| `BATCH_STOP` | 批次结束编号，不包含该编号 | `BATCH_START + 10` |
| `MAX_API_ATTEMPTS` | 权限组关联接口最大尝试次数 | `4` |
| `GROUP_CONFIRM_ATTEMPTS` | 权限组成员状态最大确认次数 | `6` |
| `GROUP_CONFIRM_INTERVAL_SECONDS` | 权限组成员状态确认间隔 | `5` |
| `MAX_REAPPLICATION_ATTEMPTS` | 最多创建的设备下发任务数 | `3` |
| `DOWNLOAD_POLL_ATTEMPTS` | 每个下发任务最大查询次数 | `6` |
| `FAILURE_CONFIRMATION_POLLS` | 确认下发失败所需的连续失败状态次数 | `2` |
| `RETRY_INTERVAL_SECONDS` | 普通 API 重试等待间隔 | `20` |
| `DOWNLOAD_POLL_INTERVAL_SECONDS` | 设备下发结果查询间隔 | `20` |
| `CLEANUP_GRACE_SECONDS` | 最终检查前的清理宽限时间 | `30` |

当前设置会处理 `4000` 到 `4009`，共 10 名访客。第一个编号自动生成：

```text
visitorGivenName = apitest0204000
certificateNo    = 204000
cardNo           = 304000
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
4. 调用 `/artemis/api/acs/v1/privilege/group/single/addPersons` 加入权限组。
5. 尽力调用 `/artemis/api/acs/v1/privilege/group/single/personList`，等待新访客在权限组成员列表中可见。该接口是可选确认：如果没有权限、版本不支持或调用失败，不会推翻已经成功的 `addPersons`，脚本等待一个传播间隔后继续下发。
6. 调用一次 `/artemis/api/visitor/v1/auth/reapplication` 触发设备下发。
7. 定时调用 `/artemis/api/visitor/v1/person/ID/elementDownloadDetail` 查询同一个下发任务，不在每次查询前重复触发下发。
8. 状态 `0` 表示成功；状态 `1` 继续等待；状态 `2` 或 `3` 表示失败。连续两次确认失败后，才允许重新调用 `auth/reapplication`。
9. 同时检查设备状态和 `CertificateStatusList` 中的卡、脸、指纹等凭证状态，并在失败时输出完整错误码。
10. 下发成功后，将 `visitorId` 和 `appointRecordId` 追加保存到本地 `successful_visitor_records.jsonl`。该文件包含访客标识，已被 `.gitignore` 排除，不会上传到 GitHub。

访客登记接口不会自动重试：

- 遇到网络错误、HTTP 错误或登记结果不确定时，脚本会停止整个批次，避免重复创建访客。
- 遇到明确的海康业务错误时，脚本会跳过当前编号，继续处理下一个编号。
- 登记成功但响应缺少 `visitorId` 或 `appointRecordId` 时，脚本会停止整个批次。
- 如果 `addPersons` 自身返回异常，并且 `personList` 也无法确认结果，脚本不会重复添加、继续下发或自动清理该访客，需要根据控制台中的 `visitorId` 人工核对。

## 失败清理

权限组关联失败，或设备下发经过轮询和重新下发后被明确确认失败时，脚本会依次执行：

1. 从权限组中移除访客。
2. 使用 `appointRecordId` 将访客签退。
3. 使用 `visitorId` 删除人员资源。

设备下发失败后，脚本会先等待宽限期并进行最后一次查询。只有前后两阶段都明确为失败，才允许自动清理；待处理、返回结构异常或查询失败等未知状态不会自动删除访客。

解除权限组后，脚本会先通过 `personList` 确认访客已经不在权限组中，然后再签退和删除人员。如果解除权限组失败或无法确认，脚本会停止后续清理，保留人工撤权所需的平台记录。

## 离线测试

测试使用模拟的海康返回数据，不会调用真实接口：

```powershell
python -m unittest -v test_api_register_and_add_group.py
```

## TLS 提醒

当前代码使用 `verify=False`，不会验证 HikCentral 服务端的 TLS 证书。这适用于现有私有证书环境，但无法防止中间人攻击。若服务器已经配置受信任证书，应将代码改为启用证书验证。

## 旧版代码

`old/` 目录保存之前的单访客处理及测试代码，不是当前批量任务的执行入口。
