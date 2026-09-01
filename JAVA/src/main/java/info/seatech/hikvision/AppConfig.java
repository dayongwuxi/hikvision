package info.seatech.hikvision;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.file.Path;

public record AppConfig(String accessKey, String secretKey, String apiBaseUrl) {
    public static AppConfig load(Path path, ObjectMapper mapper) throws IOException {
        JsonNode root = mapper.readTree(path.toFile());
        return new AppConfig(
                required(root, "AccessKey", path),
                required(root, "SecretKey", path),
                required(root, "APIbaseUrl", path).replaceAll("/+$", "")
        );
    }

    private static String required(JsonNode root, String name, Path path) {
        String value = root.path(name).asText("").trim();
        if (value.isEmpty()) {
            throw new IllegalArgumentException("設定ファイルに必須項目がありません: " + name + ": " + path);
        }
        return value;
    }
}
