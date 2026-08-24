# 海康访客创建与设备下发（Python）

`hikvision_visitor_provision.py` 执行以下流程：

1. 使用 `business-key` 检查本地 SQLite 状态。
2. 只调用一次 `/artemis/api/visitor/v1/registerment`。
3. 立即保存返回的 `visitorId` 和 `appointRecordId`。
4. 独立重试加入权限组，最多 5 次。
5. 独立重试触发设备下发，最多 5 次。
6. 独立轮询设备下发结果，最多 5 次。
7. 全部处理完以后，将结构化结果写入 `output/result.json`。

## 运行

先复制并修改示例请求参数，保证 `certificateNo` 和 `cardNo` 唯一：

```powershell
Copy-Item output\registration_payload.example.json output\registration_payload.json
```

然后执行：

```powershell
python output\hikvision_visitor_provision.py `
  --business-key "系统用户ID或其他稳定唯一键" `
  --payload output\registration_payload.json `
  --privilege-group-id "海康权限组ID" `
  --door-index-code "海康门ID"
```

程序默认读取项目现有的 `config/config.json`，最终结果同时打印到控制台并写入：

```text
output/result.json
```

去重状态保存在：

```text
output/visitor_provision.db
```

同一个 `business-key` 再次执行时会复用已经保存的 `visitorId`，不会再次创建访客。

如果登记请求发生网络超时，程序返回 `REGISTER_UNKNOWN` 并禁止自动重新登记。这种情况下海康可能已经创建访客，应先在海康平台查询后再补录，不能直接删除状态记录重试。

## 测试

测试使用模拟 API，不会连接海康，也不会真实创建访客：

```powershell
python -m unittest output\test_hikvision_visitor_provision.py -v
```
