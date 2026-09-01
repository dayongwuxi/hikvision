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
            System.err.println("設定ファイルが見つかりません: " + configPath);
            System.err.println("config/config.example.json を config/config.json にコピーし、認証情報を入力してください。");
            System.exit(2);
        }
        if (!Files.isRegularFile(payloadPath)) {
            System.err.println("訪問者データファイルが見つかりません: " + payloadPath);
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
            System.out.println("プロジェクトディレクトリ: " + projectRoot);
            System.out.println("設定ファイル: " + configPath);
            System.out.println("訪問者データ: " + payloadPath);
            service.runBatch(payloadPath);
        } catch (Exception exception) {
            System.err.println("プログラムの起動または一括処理に失敗しました: " + exception.getMessage());
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
