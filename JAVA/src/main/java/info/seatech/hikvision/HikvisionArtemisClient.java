package info.seatech.hikvision;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.time.Duration;
import java.util.Base64;

public final class HikvisionArtemisClient implements ArtemisApi {
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(15);

    private final AppConfig config;
    private final ObjectMapper mapper;
    private final HttpClient httpClient;

    public HikvisionArtemisClient(AppConfig config, ObjectMapper mapper) {
        this(config, mapper, insecureHttpClient());
    }

    HikvisionArtemisClient(AppConfig config, ObjectMapper mapper, HttpClient httpClient) {
        this.config = config;
        this.mapper = mapper;
        this.httpClient = httpClient;
    }

    @Override
    public ObjectNode post(String path, ObjectNode body) throws ApiException {
        HttpRequest request;
        try {
            request = HttpRequest.newBuilder(URI.create(config.apiBaseUrl() + path))
                    .timeout(REQUEST_TIMEOUT)
                    .header("Accept", "application/json")
                    .header("Content-Type", "application/json;charset=UTF-8")
                    .header("x-ca-key", config.accessKey())
                    .header("x-ca-signature-headers", "x-ca-key")
                    .header("X-Ca-Signature", signature(path))
                    .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body), StandardCharsets.UTF_8))
                    .build();
        } catch (RuntimeException | JsonProcessingException exception) {
            throw new ApiException("构造请求失败: path=" + path, false, exception);
        }

        HttpResponse<String> response;
        try {
            response = httpClient.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        } catch (IOException exception) {
            throw new ApiException("网络请求失败: path=" + path + ", " + exception.getMessage(), true, exception);
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw new ApiException("请求被中断: path=" + path, true, exception);
        }

        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new ApiException(
                    "HTTP 请求失败: path=" + path + ", status=" + response.statusCode()
                            + ", body=" + response.body(),
                    true
            );
        }

        JsonNode parsed;
        try {
            parsed = mapper.readTree(response.body());
        } catch (JsonProcessingException exception) {
            throw new ApiException("API 返回结果不是有效 JSON: path=" + path, true, exception);
        }
        if (!(parsed instanceof ObjectNode result)) {
            throw new ApiException("API 返回结果不是 JSON 对象: path=" + path, true);
        }
        if (!"0".equals(result.path("code").asText())) {
            throw new ApiException(
                    "path=" + path + ", code=" + display(result.get("code"))
                            + ", msg=" + display(result.get("msg"))
                            + ", data=" + display(result.get("data")),
                    false
            );
        }
        return result;
    }

    private String signature(String path) throws ApiException {
        String text = "POST\napplication/json\napplication/json;charset=UTF-8\n"
                + "x-ca-key:" + config.accessKey() + "\n" + path;
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(config.secretKey().getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return Base64.getEncoder().encodeToString(mac.doFinal(text.getBytes(StandardCharsets.UTF_8)));
        } catch (GeneralSecurityException exception) {
            throw new ApiException("生成 Artemis 签名失败", false, exception);
        }
    }

    private static String display(JsonNode node) {
        return node == null ? "null" : node.toString();
    }

    private static HttpClient insecureHttpClient() {
        try {
            // 对齐 Python requests 的 verify=False：私有证书环境同时跳过证书链和主机名校验。
            System.setProperty("jdk.internal.httpclient.disableHostnameVerification", "true");
            TrustManager[] trustAll = {new X509TrustManager() {
                @Override
                public X509Certificate[] getAcceptedIssuers() {
                    return new X509Certificate[0];
                }

                @Override
                public void checkClientTrusted(X509Certificate[] chain, String authType) {
                }

                @Override
                public void checkServerTrusted(X509Certificate[] chain, String authType) {
                }
            }};
            SSLContext context = SSLContext.getInstance("TLS");
            context.init(null, trustAll, new SecureRandom());
            SSLParameters parameters = new SSLParameters();
            parameters.setEndpointIdentificationAlgorithm("");
            return HttpClient.newBuilder()
                    .connectTimeout(REQUEST_TIMEOUT)
                    .sslContext(context)
                    .sslParameters(parameters)
                    .build();
        } catch (GeneralSecurityException exception) {
            throw new IllegalStateException("无法初始化兼容私有证书的 HTTP 客户端", exception);
        }
    }
}
