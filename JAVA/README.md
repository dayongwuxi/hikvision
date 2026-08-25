# Java 版海康访客批处理

本工程完整对应上级目录 `api_register_and_add_group.py` 的处理流程。关联文件不复制、不改名，程序会直接使用：

- `../config/config.json`
- `../registration_payload.example.json`
- `../successful_visitor_records.jsonl`

当前批次参数也与 Python 文件当前值一致：处理 `4000` 至 `4009`，权限组 `34`，门禁 `56`。

## 构建与测试

在 `JAVA` 目录执行：

```powershell
mvn test
mvn package
```

## 运行

可以在项目根目录或 `JAVA` 目录运行：

```powershell
java -jar JAVA\target\hikvision-visitor-provisioning.jar
```

如果当前目录是 `JAVA`：

```powershell
java -jar target\hikvision-visitor-provisioning.jar
```

程序会向上查找 `api_register_and_add_group.py` 来定位项目根目录。和 Python 版一样，当前 HTTP 客户端兼容 HikCentral 私有证书，不校验证书链；只应在可信内网运行。
