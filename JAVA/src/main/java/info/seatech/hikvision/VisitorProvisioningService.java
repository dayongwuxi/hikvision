package info.seatech.hikvision;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;

public final class VisitorProvisioningService {
    public enum DownloadOutcome { SUCCESS, FAILED, UNKNOWN }
    public enum MembershipCheck { CONFIRMED, NOT_CONFIRMED, UNKNOWN }
    public enum GroupAddOutcome { SUCCESS, FAILED, UNKNOWN }

    @FunctionalInterface
    public interface Sleeper {
        void sleep(long seconds) throws InterruptedException;
    }

    public record Settings(
            String privilegeGroupId,
            String doorIndexCode,
            String visitStartTime,
            String visitEndTime,
            int batchStart,
            int batchStop,
            int maxApiAttempts,
            int groupConfirmAttempts,
            int downloadPollAttempts,
            int maxReapplicationAttempts,
            int failureConfirmationPolls,
            int retryIntervalSeconds,
            int groupConfirmIntervalSeconds,
            int downloadPollIntervalSeconds,
            int cleanupGraceSeconds
    ) {
        public static Settings pythonDefaults() {
            return new Settings(
                    "34", "56",
                    "2026-08-24T23:00:00+09:00",
                    "2037-12-31T23:59:59+09:00",
                    5010, 5100,
                    4, 6, 6, 3, 2,
                    20, 5, 20, 30
            );
        }
    }

    private static final String PERSON_LIST = "/artemis/api/acs/v1/privilege/group/single/personList";
    private static final String ADD_PERSONS = "/artemis/api/acs/v1/privilege/group/single/addPersons";
    private static final String DELETE_PERSONS = "/artemis/api/acs/v1/privilege/group/single/deletePersons";
    private static final String REAPPLICATION = "/artemis/api/visitor/v1/auth/reapplication";
    private static final String DOWNLOAD_DETAIL = "/artemis/api/visitor/v1/person/ID/elementDownloadDetail";
    private static final String REGISTERMENT = "/artemis/api/visitor/v1/registerment";

    private final ArtemisApi api;
    private final ObjectMapper mapper;
    private final Settings settings;
    private final Path successRecordPath;
    private final Sleeper sleeper;

    public VisitorProvisioningService(
            ArtemisApi api,
            ObjectMapper mapper,
            Settings settings,
            Path successRecordPath
    ) {
        this(api, mapper, settings, successRecordPath, seconds -> Thread.sleep(seconds * 1_000L));
    }

    VisitorProvisioningService(
            ArtemisApi api,
            ObjectMapper mapper,
            Settings settings,
            Path successRecordPath,
            Sleeper sleeper
    ) {
        this.api = api;
        this.mapper = mapper;
        this.settings = settings;
        this.successRecordPath = successRecordPath;
        this.sleeper = sleeper;
    }

    public void runBatch(Path payloadPath) throws IOException {
        JsonNode loaded = mapper.readTree(payloadPath.toFile());
        if (!(loaded instanceof ObjectNode visitorData)) {
            throw new IllegalArgumentException("訪問者データはJSONオブジェクトである必要があります: " + payloadPath);
        }
        visitorData.put("visitStartTime", settings.visitStartTime());
        visitorData.put("visitEndTime", settings.visitEndTime());

        ObjectNode visitorInfo = requireVisitorInfo(visitorData);
        for (int batchNumber = settings.batchStart(); batchNumber < settings.batchStop(); batchNumber++) {
            String visitorName = "apitest02" + String.format("%05d", batchNumber);
            visitorInfo.put("visitorGivenName", visitorName);
            visitorInfo.put("certificateNo", "2" + String.format("%05d", batchNumber));
            requireFirstCard(visitorInfo).put("cardNo", "3" + String.format("%05d", batchNumber));

            System.out.println("▼".repeat(48));
            ObjectNode visitor;
            try {
                visitor = api.post(REGISTERMENT, visitorData);
            } catch (ApiException exception) {
                if (exception.isResultUncertain()) {
                    System.out.println("訪問者登録の結果を確定できません。重複登録を防ぐため、バッチを停止します: " + exception.getMessage());
                    break;
                }
                System.out.println("訪問者登録に失敗しました: " + exception.getMessage());
                continue;
            }

            JsonNode data = visitor.get("data");
            JsonNode visitorIdNode = data == null ? null : data.get("visitorId");
            JsonNode appointIdNode = data == null ? null : data.get("appointRecordId");
            if (visitorIdNode == null || visitorIdNode.isNull() || appointIdNode == null || appointIdNode.isNull()) {
                System.out.println("登録成功レスポンスに visitorId または appointRecordId がありません。重複登録を防ぐため、バッチを停止します");
                break;
            }
            String visitorId = visitorIdNode.asText();
            String appointRecordId = appointIdNode.asText();

            ObjectNode output = mapper.createObjectNode();
            output.put("visitorGivenName", visitorName);
            output.set("registerment", visitor);
            System.out.println(mapper.writerWithDefaultPrettyPrinter().writeValueAsString(output));

            GroupAddOutcome groupOutcome = addVisitorToGroup(visitorId);
            if (groupOutcome == GroupAddOutcome.UNKNOWN) {
                System.out.println("権限グループへの関連付け結果を確定できないため、デバイス配信と自動クリーンアップは実行しません。visitorId を使用して手動で確認してください");
                continue;
            }
            if (groupOutcome == GroupAddOutcome.FAILED) {
                System.out.println("権限グループへの関連付けに失敗しました。未配信の訪問者をクリーンアップします");
                cleanupFailedVisitor(visitorId, appointRecordId, visitorName);
                continue;
            }

            DownloadOutcome downloadOutcome = downloadVisitorPermission(visitorId);
            if (downloadOutcome == DownloadOutcome.SUCCESS) {
                trySaveSuccessfulVisitor(visitorId, appointRecordId, visitorName);
                continue;
            }

            DownloadOutcome finalOutcome = finalDownloadCheck(visitorId);
            if (finalOutcome == DownloadOutcome.SUCCESS) {
                System.out.println("最終確認でデバイス配信の成功を検出したため、クリーンアップを中止します");
                trySaveSuccessfulVisitor(visitorId, appointRecordId, visitorName);
            } else if (downloadOutcome == DownloadOutcome.FAILED && finalOutcome == DownloadOutcome.FAILED) {
                System.out.println("最終確認でも配信失敗だったため、訪問者をクリーンアップします");
                cleanupFailedVisitor(visitorId, appointRecordId, visitorName);
            } else {
                System.out.println("2段階で一致した失敗結果を確認できませんでした。非同期処理中の訪問者を削除しないよう、自動クリーンアップは実行しません");
            }
        }
    }

    public boolean groupContainsVisitor(String visitorId) throws ApiException {
        int pageSize = 500;
        int pageNo = 1;
        while (true) {
            ObjectNode body = mapper.createObjectNode();
            body.put("privilegeGroupId", settings.privilegeGroupId());
            body.put("type", 2);
            body.put("pageNo", pageNo);
            body.put("pageSize", pageSize);
            ObjectNode result = api.post(PERSON_LIST, body);

            JsonNode data = result.get("data");
            if (data == null || !data.isObject()) {
                return false;
            }
            List<JsonNode> people = asList(data.get("list"));
            for (JsonNode person : people) {
                if (person.isObject() && visitorId.equals(text(firstValue(person, "id", "personId")))) {
                    return true;
                }
            }
            int total = people.size();
            try {
                JsonNode totalNode = data.get("total");
                if (totalNode != null) {
                    total = Integer.parseInt(totalNode.asText());
                }
            } catch (NumberFormatException ignored) {
                total = people.size();
            }
            if (people.isEmpty() || pageNo * pageSize >= total) {
                return false;
            }
            pageNo++;
        }
    }

    public MembershipCheck waitForGroupMembership(String visitorId, boolean expected) {
        for (int attempt = 1; attempt <= settings.groupConfirmAttempts(); attempt++) {
            try {
                boolean member = groupContainsVisitor(visitorId);
                System.out.printf("権限グループ確認: %d/%d 回目、isMember=%s%n",
                        attempt, settings.groupConfirmAttempts(), member);
                if (member == expected) {
                    return MembershipCheck.CONFIRMED;
                }
            } catch (ApiException exception) {
                System.out.println("権限グループのメンバー照会に失敗したため、状態を unknown とします: " + exception.getMessage());
                return MembershipCheck.UNKNOWN;
            }
            if (attempt < settings.groupConfirmAttempts() && !sleep(settings.groupConfirmIntervalSeconds())) {
                return MembershipCheck.UNKNOWN;
            }
        }
        return MembershipCheck.NOT_CONFIRMED;
    }

    public GroupAddOutcome addVisitorToGroup(String visitorId) {
        for (int attempt = 1; attempt <= settings.maxApiAttempts(); attempt++) {
            System.out.printf("権限グループへの関連付け: %d/%d 回目%n", attempt, settings.maxApiAttempts());
            try {
                api.post(ADD_PERSONS, groupBody(visitorId));
            } catch (ApiException exception) {
                System.out.println("権限グループへの追加に失敗しました: " + exception.getMessage());
                MembershipCheck membership = waitForGroupMembership(visitorId, true);
                if (membership == MembershipCheck.CONFIRMED) {
                    System.out.println("addPersons はエラーを返しましたが、訪問者が権限グループに存在することを確認しました");
                    return GroupAddOutcome.SUCCESS;
                }
                if (membership == MembershipCheck.UNKNOWN) {
                    System.out.println("addPersons と personList の結果をどちらも確定できません。重複追加や誤削除を防ぐため、この訪問者の自動配信とクリーンアップを中止します");
                    return GroupAddOutcome.UNKNOWN;
                }
                if (attempt < settings.maxApiAttempts()) {
                    if (!sleep(settings.retryIntervalSeconds())) {
                        return GroupAddOutcome.UNKNOWN;
                    }
                    continue;
                }
                return GroupAddOutcome.FAILED;
            }

            MembershipCheck membership = waitForGroupMembership(visitorId, true);
            if (membership == MembershipCheck.CONFIRMED) {
                return GroupAddOutcome.SUCCESS;
            }
            if (membership == MembershipCheck.UNKNOWN) {
                System.out.printf("addPersons は成功しましたが、personList を使用できません。%d 秒待機してからデバイス配信を続行します%n",
                        settings.retryIntervalSeconds());
                if (!sleep(settings.retryIntervalSeconds())) {
                    return GroupAddOutcome.UNKNOWN;
                }
                return GroupAddOutcome.SUCCESS;
            }
            System.out.println("addPersons は成功しましたが、複数回照会しても権限グループ内に訪問者が見つかりませんでした");
            return GroupAddOutcome.FAILED;
        }
        return GroupAddOutcome.FAILED;
    }

    public DownloadOutcome pollDownloadResult(String visitorId) {
        int consecutiveFailures = 0;
        for (int attempt = 1; attempt <= settings.downloadPollAttempts(); attempt++) {
            System.out.printf("%d 秒待機してから配信結果を照会します: %d/%d 回目%n",
                    settings.downloadPollIntervalSeconds(), attempt, settings.downloadPollAttempts());
            if (!sleep(settings.downloadPollIntervalSeconds())) {
                return DownloadOutcome.UNKNOWN;
            }
            ObjectNode detail;
            try {
                detail = api.post(DOWNLOAD_DETAIL, idBody(visitorId));
            } catch (ApiException exception) {
                System.out.println("デバイス配信結果の照会に失敗しました: " + exception.getMessage());
                continue;
            }
            String classification = classifyDownloadDetail(detail);
            System.out.println("配信照会結果: classification=" + classification
                    + ", elementStatus=" + extractElementStatuses(detail));
            if ("success".equals(classification)) {
                printDownloadDetail("●配信成功●", detail);
                return DownloadOutcome.SUCCESS;
            }
            if ("failed".equals(classification)) {
                consecutiveFailures++;
                printDownloadDetail("デバイスから配信失敗が返されました。完全なエラー情報を表示します:", detail);
                if (consecutiveFailures >= settings.failureConfirmationPolls()) {
                    System.out.printf("配信失敗を連続 %d 回確認したため、再配信処理へ進みます%n", consecutiveFailures);
                    return DownloadOutcome.FAILED;
                }
                System.out.println("再配信前の古い失敗スナップショットを新しいジョブの失敗と誤判定しないよう、照会をもう一度続行します");
                continue;
            }
            consecutiveFailures = 0;
            if ("pending".equals(classification)) {
                System.out.println("デバイスジョブは処理待ちです。配信を重複して開始せず、照会を続行します");
            } else {
                printDownloadDetail("レスポンス内に認識可能な elementStatus がありません。照会を続行します:", detail);
            }
        }
        System.out.println("照会期間内に明確な成功結果または連続した失敗結果を取得できませんでした");
        return DownloadOutcome.UNKNOWN;
    }

    public DownloadOutcome downloadVisitorPermission(String visitorId) {
        for (int attempt = 1; attempt <= settings.maxReapplicationAttempts(); attempt++) {
            String action = attempt == 1 ? "初回配信" : "再配信";
            System.out.printf("%s: %d/%d 件目の配信ジョブ%n", action, attempt, settings.maxReapplicationAttempts());
            ObjectNode body = mapper.createObjectNode();
            body.put("ImmediateDownload", 0);
            body.put("personIds", visitorId);
            body.put("doorIndexCodes", settings.doorIndexCode());
            try {
                api.post(REAPPLICATION, body);
            } catch (ApiException exception) {
                System.out.println("デバイス配信の開始に失敗しました: " + exception.getMessage());
                if (attempt < settings.maxReapplicationAttempts()) {
                    if (!sleep(settings.retryIntervalSeconds())) {
                        return DownloadOutcome.UNKNOWN;
                    }
                    continue;
                }
                return DownloadOutcome.UNKNOWN;
            }
            DownloadOutcome outcome = pollDownloadResult(visitorId);
            if (outcome == DownloadOutcome.SUCCESS) {
                return DownloadOutcome.SUCCESS;
            }
            if (outcome == DownloadOutcome.UNKNOWN) {
                return DownloadOutcome.UNKNOWN;
            }
            if (attempt < settings.maxReapplicationAttempts()) {
                System.out.printf("今回の配信失敗を確認しました。%d 秒待機してから再配信します%n", settings.retryIntervalSeconds());
                if (!sleep(settings.retryIntervalSeconds())) {
                    return DownloadOutcome.UNKNOWN;
                }
            }
        }
        return DownloadOutcome.FAILED;
    }

    public DownloadOutcome finalDownloadCheck(String visitorId) {
        System.out.printf("クリーンアップ前に %d 秒待機し、デバイス状態を最終確認します%n", settings.cleanupGraceSeconds());
        if (!sleep(settings.cleanupGraceSeconds())) {
            return DownloadOutcome.UNKNOWN;
        }
        ObjectNode detail;
        try {
            detail = api.post(DOWNLOAD_DETAIL, idBody(visitorId));
        } catch (ApiException exception) {
            System.out.println("クリーンアップ前の最終照会に失敗し、状態を確定できないため、自動クリーンアップを禁止します: " + exception.getMessage());
            return DownloadOutcome.UNKNOWN;
        }
        String status = classifyDownloadDetail(detail);
        printDownloadDetail("クリーンアップ前の最終状態: " + status, detail);
        return switch (status) {
            case "success" -> DownloadOutcome.SUCCESS;
            case "failed" -> DownloadOutcome.FAILED;
            default -> DownloadOutcome.UNKNOWN;
        };
    }

    public void cleanupFailedVisitor(String visitorId, String appointRecordId, String visitorName) {
        boolean member;
        try {
            member = groupContainsVisitor(visitorId);
        } catch (ApiException exception) {
            System.out.println("クリーンアップ前の権限グループメンバー照会に失敗しました。権限グループからの削除を試行します: " + exception.getMessage());
            member = true;
        }
        if (!member) {
            System.out.println("訪問者はすでに権限グループに存在しないため、再度削除する必要はありません: " + visitorName);
        } else {
            try {
                api.post(DELETE_PERSONS, groupBody(visitorId));
                System.out.println("権限グループからの削除リクエストに成功しました: " + visitorName);
            } catch (ApiException exception) {
                System.out.println("権限グループからの削除に失敗しました。後から権限を解除できる状態を維持するため、チェックアウトと人物削除を中止します: "
                        + visitorName + ", " + exception.getMessage());
                return;
            }
            MembershipCheck removal = waitForGroupMembership(visitorId, false);
            if (removal != MembershipCheck.CONFIRMED) {
                String reason = removal == MembershipCheck.UNKNOWN ? "personList を使用できません" : "訪問者がまだ権限グループに存在します";
                System.out.println("訪問者が権限グループから削除されたことを確認できませんでした（" + reason + "）。プラットフォームとデバイス状態の関連付けを維持するため、チェックアウトと人物削除を中止します");
                return;
            }
        }
        executeCleanup("訪問者のチェックアウト", "/artemis/api/visitor/v1/visitor/out", "appointRecordId", appointRecordId, visitorName);
        executeCleanup("人物の削除", "/artemis/api/resource/v1/person/single/delete", "personId", visitorId, visitorName);
    }

    public List<ObjectNode> extractElementDiagnostics(JsonNode detail) {
        JsonNode data = detail.get("data");
        if (data == null || !data.isObject()) {
            return List.of();
        }
        JsonNode detailList = firstValue(data, "ElementDetailList", "elementDetailList");
        JsonNode elements = detailList != null && detailList.isObject()
                ? firstValue(detailList, "ElementDetail", "elementDetail") : detailList;
        List<ObjectNode> diagnostics = new ArrayList<>();
        for (JsonNode element : asList(elements)) {
            if (!element.isObject()) {
                continue;
            }
            JsonNode base = firstValue(element, "BaseInfo", "baseInfo");
            if (base == null || !base.isObject()) {
                base = mapper.createObjectNode();
            }
            ArrayNode elementStatuses = mapper.createArrayNode();
            for (JsonNode item : asList(firstValue(element, "ElementStatus", "elementStatus"))) {
                ObjectNode normalized = normalizeStatus(item);
                if (!normalized.isEmpty()) {
                    elementStatuses.add(normalized);
                }
            }
            JsonNode certificateList = firstValue(element, "CertificateStatusList", "certificateStatusList");
            JsonNode certificates = certificateList != null && certificateList.isObject()
                    ? firstValue(certificateList, "CertificateStatus", "certificateStatus") : certificateList;
            ArrayNode certificateStatuses = mapper.createArrayNode();
            for (JsonNode certificate : asList(certificates)) {
                if (!certificate.isObject()) {
                    continue;
                }
                ObjectNode normalized = normalizeStatus(certificate);
                if (normalized.isEmpty()) {
                    continue;
                }
                normalized.put("id", text(firstValue(certificate, "ID", "id")));
                normalized.put("type", text(firstValue(certificate, "Type", "type")));
                certificateStatuses.add(normalized);
            }
            ObjectNode diagnostic = mapper.createObjectNode();
            diagnostic.put("id", text(firstValue(element, "ID", "id")));
            JsonNode name = firstValue(base, "Name", "name");
            if (name == null || name.isNull() || name.asText().isEmpty()) {
                name = firstValue(element, "Name", "name");
            }
            diagnostic.put("name", text(name));
            diagnostic.put("elementType", text(firstValue(base, "ElementType", "elementType")));
            diagnostic.put("network", text(firstValue(base, "Network", "network")));
            diagnostic.set("elementStatuses", elementStatuses);
            diagnostic.set("certificateStatuses", certificateStatuses);
            diagnostics.add(diagnostic);
        }
        return diagnostics;
    }

    public List<String> extractElementStatuses(JsonNode detail) {
        return extractStatuses(detail, "elementStatuses");
    }

    public List<String> extractCertificateStatuses(JsonNode detail) {
        return extractStatuses(detail, "certificateStatuses");
    }

    public String classifyDownloadDetail(JsonNode detail) {
        List<String> statuses = new ArrayList<>(extractElementStatuses(detail));
        statuses.addAll(extractCertificateStatuses(detail));
        if (!statuses.isEmpty() && statuses.stream().allMatch("0"::equals)) {
            return "success";
        }
        if (statuses.stream().anyMatch(status -> "2".equals(status) || "3".equals(status))) {
            return "failed";
        }
        return statuses.isEmpty() ? "unknown" : "pending";
    }

    private ObjectNode normalizeStatus(JsonNode status) {
        ObjectNode normalized = mapper.createObjectNode();
        if (status == null || !status.isObject()) {
            normalized.put("status", text(status));
            normalized.put("errorModule", "");
            normalized.put("errorCode", "");
            return normalized;
        }
        JsonNode value = firstValue(status, "elementStatus", "Status", "status");
        if (value == null) {
            return normalized;
        }
        normalized.put("status", text(value));
        normalized.put("errorModule", text(firstValue(status, "ErrorModule", "errorModule")));
        normalized.put("errorCode", text(firstValue(status, "ErrorCode", "errorCode")));
        return normalized;
    }

    private List<String> extractStatuses(JsonNode detail, String field) {
        List<String> statuses = new ArrayList<>();
        for (ObjectNode diagnostic : extractElementDiagnostics(detail)) {
            for (JsonNode status : diagnostic.withArray(field)) {
                statuses.add(status.path("status").asText());
            }
        }
        return statuses;
    }

    private void printDownloadDetail(String label, JsonNode detail) {
        ObjectNode output = mapper.createObjectNode();
        output.set("diagnostics", mapper.valueToTree(extractElementDiagnostics(detail)));
        output.set("rawResponse", detail);
        try {
            System.out.println(label);
            System.out.println(mapper.writerWithDefaultPrettyPrinter().writeValueAsString(output));
        } catch (JsonProcessingException exception) {
            System.out.println(label + "（診断情報の整形に失敗しました: " + exception.getMessage() + "）");
        }
    }

    private void saveSuccessfulVisitor(String visitorId, String appointRecordId, String visitorName) throws IOException {
        ObjectNode record = mapper.createObjectNode();
        record.put("savedAt", OffsetDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ssxx")));
        record.put("visitorGivenName", visitorName);
        record.put("visitorId", visitorId);
        record.put("appointRecordId", appointRecordId);
        record.put("privilegeGroupId", settings.privilegeGroupId());
        record.put("doorIndexCode", settings.doorIndexCode());
        record.put("visitStartTime", settings.visitStartTime());
        record.put("visitEndTime", settings.visitEndTime());
        record.put("checkin", "ON");
        Files.writeString(
                successRecordPath,
                mapper.writeValueAsString(record) + System.lineSeparator(),
                StandardCharsets.UTF_8,
                StandardOpenOption.CREATE,
                StandardOpenOption.APPEND
        );
        System.out.println("visitorId と appointRecordId を保存しました: " + successRecordPath);
    }

    private void trySaveSuccessfulVisitor(String visitorId, String appointRecordId, String visitorName) {
        try {
            saveSuccessfulVisitor(visitorId, appointRecordId, visitorName);
        } catch (IOException exception) {
            System.out.println("配信は成功しましたが、訪問者IDレコードの保存に失敗しました: " + exception.getMessage());
        }
    }

    private void executeCleanup(String action, String path, String key, String value, String visitorName) {
        ObjectNode body = mapper.createObjectNode();
        body.put(key, value);
        try {
            api.post(path, body);
            System.out.println(action + "に成功しました: " + visitorName);
        } catch (ApiException exception) {
            System.out.println(action + "に失敗しました: " + visitorName + ", " + exception.getMessage());
        }
    }

    private ObjectNode groupBody(String visitorId) {
        ObjectNode body = mapper.createObjectNode();
        body.put("privilegeGroupId", settings.privilegeGroupId());
        body.put("type", 2);
        ObjectNode person = mapper.createObjectNode();
        person.put("id", visitorId);
        body.putArray("list").add(person);
        return body;
    }

    private ObjectNode idBody(String visitorId) {
        ObjectNode body = mapper.createObjectNode();
        body.put("id", visitorId);
        return body;
    }

    private static ObjectNode requireVisitorInfo(ObjectNode payload) {
        JsonNode node = payload.path("visitorInfoList").path(0).path("VisitorInfo");
        if (!(node instanceof ObjectNode object)) {
            throw new IllegalArgumentException("registration_payload.example.json に visitorInfoList[0].VisitorInfo がありません");
        }
        return object;
    }

    private static ObjectNode requireFirstCard(ObjectNode visitorInfo) {
        JsonNode node = visitorInfo.path("cards").path(0);
        if (!(node instanceof ObjectNode object)) {
            throw new IllegalArgumentException("registration_payload.example.json に cards[0] がありません");
        }
        return object;
    }

    private static List<JsonNode> asList(JsonNode value) {
        if (value == null || value.isNull()) {
            return List.of();
        }
        if (value.isArray()) {
            List<JsonNode> items = new ArrayList<>();
            value.forEach(items::add);
            return items;
        }
        if (value.isObject()) {
            return List.of(value);
        }
        return List.of();
    }

    private static JsonNode firstValue(JsonNode mapping, String... keys) {
        if (mapping == null || !mapping.isObject()) {
            return null;
        }
        for (String key : keys) {
            if (mapping.has(key)) {
                return mapping.get(key);
            }
        }
        return null;
    }

    private static String text(JsonNode value) {
        if (value == null || value.isNull()) {
            return "";
        }
        return value.isTextual() ? value.textValue() : value.asText();
    }

    private boolean sleep(long seconds) {
        try {
            sleeper.sleep(seconds);
            return true;
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            System.out.println("待機が中断されたため、現在の自動処理を停止します");
            return false;
        }
    }
}
