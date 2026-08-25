package info.seatech.hikvision;

import com.fasterxml.jackson.databind.ObjectMapper;

import java.nio.file.Files;
import java.nio.file.Path;

public final class Main {
    private Main() {
    }

    public static void main(String[] args) {
        Path projectRoot = locateProjectRoot(Path.of("").toAbsolutePath().normalize());
        Path configPath = projectRoot.resolve("config/config.json");
        Path payloadPath = projectRoot.resolve("registration_payload.example.json");
        Path recordPath = projectRoot.resolve("successful_visitor_records.jsonl");

        if (!Files.isRegularFile(configPath)) {
            System.err.println("找不到配置文件: " + configPath);
            System.err.println("请先将 config/config.example.json 复制为 config/config.json 并填写凭据。");
            System.exit(2);
        }
        if (!Files.isRegularFile(payloadPath)) {
            System.err.println("找不到访客资料文件: " + payloadPath);
            System.exit(2);
        }

        try {
            ObjectMapper mapper = new ObjectMapper();
            AppConfig config = AppConfig.load(configPath, mapper);
            ArtemisApi api = new HikvisionArtemisClient(config, mapper);
            VisitorProvisioningService service = new VisitorProvisioningService(
                    api,
                    mapper,
                    VisitorProvisioningService.Settings.pythonDefaults(),
                    recordPath
            );
            System.out.println("项目目录: " + projectRoot);
            System.out.println("配置文件: " + configPath);
            System.out.println("访客资料: " + payloadPath);
            service.runBatch(payloadPath);
        } catch (Exception exception) {
            System.err.println("程序启动或批处理失败: " + exception.getMessage());
            exception.printStackTrace(System.err);
            System.exit(1);
        }
    }

    static Path locateProjectRoot(Path start) {
        Path current = start;
        while (current != null) {
            if (Files.isRegularFile(current.resolve("api_register_and_add_group.py"))) {
                return current;
            }
            current = current.getParent();
        }
        if ("JAVA".equalsIgnoreCase(start.getFileName() == null ? "" : start.getFileName().toString())) {
            return start.getParent();
        }
        return start;
    }
}
