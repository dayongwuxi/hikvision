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
                    "2026-12-31T23:59:59+09:00",
                    4000, 4010,
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
            throw new IllegalArgumentException("访客资料必须是 JSON 对象: " + payloadPath);
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
                    System.out.println("访客登记结果不确定，为避免继续生成重复访客，停止批次: " + exception.getMessage());
                    break;
                }
                System.out.println("访客登记失败: " + exception.getMessage());
                continue;
            }

            JsonNode data = visitor.get("data");
            JsonNode visitorIdNode = data == null ? null : data.get("visitorId");
            JsonNode appointIdNode = data == null ? null : data.get("appointRecordId");
            if (visitorIdNode == null || visitorIdNode.isNull() || appointIdNode == null || appointIdNode.isNull()) {
                System.out.println("登记返回成功但缺少 visitorId 或 appointRecordId，为避免重复创建，停止批次");
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
                System.out.println("权限组关联结果不确定，本次不继续下发，也不执行自动清理；请根据 visitorId 人工核对");
                continue;
            }
            if (groupOutcome == GroupAddOutcome.FAILED) {
                System.out.println("权限组关联失败，开始清理未下发的访客");
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
                System.out.println("最后确认时发现设备下发已经成功，取消清理");
                trySaveSuccessfulVisitor(visitorId, appointRecordId, visitorName);
            } else if (downloadOutcome == DownloadOutcome.FAILED && finalOutcome == DownloadOutcome.FAILED) {
                System.out.println("最终确认仍为下发失败，开始统一清理访客");
                cleanupFailedVisitor(visitorId, appointRecordId, visitorName);
            } else {
                System.out.println("没有得到两阶段一致的失败结论，为避免删除仍在异步处理的访客，本次不执行自动清理");
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
                System.out.printf("权限组确认：第 %d/%d 次，isMember=%s%n",
                        attempt, settings.groupConfirmAttempts(), member);
                if (member == expected) {
                    return MembershipCheck.CONFIRMED;
                }
            } catch (ApiException exception) {
                System.out.println("查询权限组成员失败，状态记为 unknown: " + exception.getMessage());
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
            System.out.printf("权限组关联：第 %d/%d 次%n", attempt, settings.maxApiAttempts());
            try {
                api.post(ADD_PERSONS, groupBody(visitorId));
            } catch (ApiException exception) {
                System.out.println("加入权限组失败: " + exception.getMessage());
                MembershipCheck membership = waitForGroupMembership(visitorId, true);
                if (membership == MembershipCheck.CONFIRMED) {
                    System.out.println("虽然 addPersons 返回异常，但已确认访客存在于权限组");
                    return GroupAddOutcome.SUCCESS;
                }
                if (membership == MembershipCheck.UNKNOWN) {
                    System.out.println("addPersons 与 personList 的结果都无法确定，停止该访客的自动下发和清理，避免重复添加或误删");
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
                System.out.printf("addPersons 已明确成功，但 personList 无法使用；等待 %d 秒后继续设备下发%n",
                        settings.retryIntervalSeconds());
                if (!sleep(settings.retryIntervalSeconds())) {
                    return GroupAddOutcome.UNKNOWN;
                }
                return GroupAddOutcome.SUCCESS;
            }
            System.out.println("addPersons 已成功，但多次查询仍未在权限组中找到访客");
            return GroupAddOutcome.FAILED;
        }
        return GroupAddOutcome.FAILED;
    }

    public DownloadOutcome pollDownloadResult(String visitorId) {
        int consecutiveFailures = 0;
        for (int attempt = 1; attempt <= settings.downloadPollAttempts(); attempt++) {
            System.out.printf("等待 %d 秒后查询下发结果：第 %d/%d 次%n",
                    settings.downloadPollIntervalSeconds(), attempt, settings.downloadPollAttempts());
            if (!sleep(settings.downloadPollIntervalSeconds())) {
                return DownloadOutcome.UNKNOWN;
            }
            ObjectNode detail;
            try {
                detail = api.post(DOWNLOAD_DETAIL, idBody(visitorId));
            } catch (ApiException exception) {
                System.out.println("查询设备下发结果失败: " + exception.getMessage());
                continue;
            }
            String classification = classifyDownloadDetail(detail);
            System.out.println("下发查询结果：classification=" + classification
                    + ", elementStatus=" + extractElementStatuses(detail));
            if ("success".equals(classification)) {
                printDownloadDetail("●下发成功●", detail);
                return DownloadOutcome.SUCCESS;
            }
            if ("failed".equals(classification)) {
                consecutiveFailures++;
                printDownloadDetail("设备返回下发失败，保留完整错误信息：", detail);
                if (consecutiveFailures >= settings.failureConfirmationPolls()) {
                    System.out.printf("连续 %d 次确认下发失败，允许进入重新下发处理%n", consecutiveFailures);
                    return DownloadOutcome.FAILED;
                }
                System.out.println("先继续查询一次，避免把重新下发前的旧失败快照误判为新任务失败");
                continue;
            }
            consecutiveFailures = 0;
            if ("pending".equals(classification)) {
                System.out.println("设备任务仍处于待处理状态，继续查询，不重复触发下发");
            } else {
                printDownloadDetail("返回结构中没有可识别的 elementStatus，继续查询：", detail);
            }
        }
        System.out.println("在查询期限内没有得到明确的成功或连续失败结果");
        return DownloadOutcome.UNKNOWN;
    }

    public DownloadOutcome downloadVisitorPermission(String visitorId) {
        for (int attempt = 1; attempt <= settings.maxReapplicationAttempts(); attempt++) {
            String action = attempt == 1 ? "首次下发" : "重新下发";
            System.out.printf("%s：第 %d/%d 个下发任务%n", action, attempt, settings.maxReapplicationAttempts());
            ObjectNode body = mapper.createObjectNode();
            body.put("ImmediateDownload", 0);
            body.put("personIds", visitorId);
            body.put("doorIndexCodes", settings.doorIndexCode());
            try {
                api.post(REAPPLICATION, body);
            } catch (ApiException exception) {
                System.out.println("触发设备下发失败: " + exception.getMessage());
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
                System.out.printf("本次下发已确认失败，等待 %d 秒后重新下发%n", settings.retryIntervalSeconds());
                if (!sleep(settings.retryIntervalSeconds())) {
                    return DownloadOutcome.UNKNOWN;
                }
            }
        }
        return DownloadOutcome.FAILED;
    }

    public DownloadOutcome finalDownloadCheck(String visitorId) {
        System.out.printf("清理前等待 %d 秒并做最后一次设备状态确认%n", settings.cleanupGraceSeconds());
        if (!sleep(settings.cleanupGraceSeconds())) {
            return DownloadOutcome.UNKNOWN;
        }
        ObjectNode detail;
        try {
            detail = api.post(DOWNLOAD_DETAIL, idBody(visitorId));
        } catch (ApiException exception) {
            System.out.println("清理前最终查询失败，状态不确定，禁止自动清理: " + exception.getMessage());
            return DownloadOutcome.UNKNOWN;
        }
        String status = classifyDownloadDetail(detail);
        printDownloadDetail("清理前最终状态：" + status, detail);
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
            System.out.println("清理前查询权限组成员失败，将尝试执行解除权限组: " + exception.getMessage());
            member = true;
        }
        if (!member) {
            System.out.println("访客已不在权限组中，无需再次解除: " + visitorName);
        } else {
            try {
                api.post(DELETE_PERSONS, groupBody(visitorId));
                System.out.println("解除权限组请求成功: " + visitorName);
            } catch (ApiException exception) {
                System.out.println("解除权限组失败，为保留后续撤权能力，停止签退和删除人员: "
                        + visitorName + ", " + exception.getMessage());
                return;
            }
            MembershipCheck removal = waitForGroupMembership(visitorId, false);
            if (removal != MembershipCheck.CONFIRMED) {
                String reason = removal == MembershipCheck.UNKNOWN ? "personList 不可用" : "访客仍在权限组中";
                System.out.println("未确认访客已从权限组移除（" + reason + "），为避免平台和设备状态失去关联，停止签退和删除人员");
                return;
            }
        }
        executeCleanup("访客签退", "/artemis/api/visitor/v1/visitor/out", "appointRecordId", appointRecordId, visitorName);
        executeCleanup("删除人员", "/artemis/api/resource/v1/person/single/delete", "personId", visitorId, visitorName);
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
            System.out.println(label + "（格式化诊断信息失败: " + exception.getMessage() + "）");
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
        System.out.println("已保存 visitorId 和 appointRecordId: " + successRecordPath);
    }

    private void trySaveSuccessfulVisitor(String visitorId, String appointRecordId, String visitorName) {
        try {
            saveSuccessfulVisitor(visitorId, appointRecordId, visitorName);
        } catch (IOException exception) {
            System.out.println("下发成功，但保存访客ID记录失败: " + exception.getMessage());
        }
    }

    private void executeCleanup(String action, String path, String key, String value, String visitorName) {
        ObjectNode body = mapper.createObjectNode();
        body.put(key, value);
        try {
            api.post(path, body);
            System.out.println(action + "成功: " + visitorName);
        } catch (ApiException exception) {
            System.out.println(action + "失败: " + visitorName + ", " + exception.getMessage());
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
            throw new IllegalArgumentException("registration_payload.example.json 缺少 visitorInfoList[0].VisitorInfo");
        }
        return object;
    }

    private static ObjectNode requireFirstCard(ObjectNode visitorInfo) {
        JsonNode node = visitorInfo.path("cards").path(0);
        if (!(node instanceof ObjectNode object)) {
            throw new IllegalArgumentException("registration_payload.example.json 缺少 cards[0]");
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
            System.out.println("等待被中断，停止当前自动流程");
            return false;
        }
    }
}
