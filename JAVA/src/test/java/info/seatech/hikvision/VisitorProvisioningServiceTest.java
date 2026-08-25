package info.seatech.hikvision;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class VisitorProvisioningServiceTest {
    private final ObjectMapper mapper = new ObjectMapper();

    @TempDir
    Path tempDir;

    @Test
    void parsesObjectShapeAndPreservesNumericZero() throws Exception {
        VisitorProvisioningService service = service((path, body) -> ok(), defaults());
        ObjectNode response = (ObjectNode) mapper.readTree("""
                {"data":{"ElementDetailList":{"ElementDetail":{
                  "ID":0,
                  "BaseInfo":{"Name":"Door","ElementType":0,"Network":0},
                  "ElementStatus":{"Status":"2","ErrorModule":"ACS","ErrorCode":"DEVICE-ERROR"}
                }}}}
                """);

        List<ObjectNode> diagnostics = service.extractElementDiagnostics(response);

        assertEquals("failed", service.classifyDownloadDetail(response));
        assertEquals("0", diagnostics.get(0).path("id").asText());
        assertEquals("0", diagnostics.get(0).path("elementType").asText());
        assertEquals("DEVICE-ERROR",
                diagnostics.get(0).path("elementStatuses").path(0).path("errorCode").asText());
    }

    @Test
    void credentialFailurePreventsFalseSuccess() throws Exception {
        VisitorProvisioningService service = service((path, body) -> ok(), defaults());
        ObjectNode response = detail("0", "2");

        assertEquals(List.of("0"), service.extractElementStatuses(response));
        assertEquals(List.of("2"), service.extractCertificateStatuses(response));
        assertEquals("failed", service.classifyDownloadDetail(response));
    }

    @Test
    void searchesLaterMembershipPages() throws Exception {
        Deque<ObjectNode> responses = new ArrayDeque<>();
        responses.add((ObjectNode) mapper.readTree("{\"code\":\"0\",\"data\":{\"total\":501,\"list\":[{\"id\":\"another\"}]}}"));
        responses.add((ObjectNode) mapper.readTree("{\"code\":\"0\",\"data\":{\"total\":501,\"list\":[{\"personId\":\"visitor-1\"}]}}"));
        List<ObjectNode> bodies = new ArrayList<>();
        VisitorProvisioningService service = service((path, body) -> {
            bodies.add(body.deepCopy());
            return responses.removeFirst();
        }, defaults());

        assertTrue(service.groupContainsVisitor("visitor-1"));
        assertEquals(2, bodies.size());
        assertEquals(2, bodies.get(1).path("pageNo").asInt());
    }

    @Test
    void successfulAddContinuesWhenOptionalPersonListUnavailable() {
        VisitorProvisioningService service = service((path, body) -> {
            if (path.endsWith("addPersons")) {
                return ok();
            }
            throw new ApiException("personList is not authorized", false);
        }, defaults());

        assertEquals(VisitorProvisioningService.GroupAddOutcome.SUCCESS,
                service.addVisitorToGroup("visitor-1"));
    }

    @Test
    void uncertainAddDoesNotRetryWhenPersonListUnavailable() {
        List<String> paths = new ArrayList<>();
        VisitorProvisioningService service = service((path, body) -> {
            paths.add(path);
            throw new ApiException("service unavailable", true);
        }, defaults());

        assertEquals(VisitorProvisioningService.GroupAddOutcome.UNKNOWN,
                service.addVisitorToGroup("visitor-1"));
        assertEquals(1, paths.stream().filter(path -> path.endsWith("addPersons")).count());
    }

    @Test
    void staleFailureThenSuccessDoesNotReapply() {
        FlowResult result = runDownloadFlow(List.of(detail("2", null), detail("0", null)));

        assertEquals(VisitorProvisioningService.DownloadOutcome.SUCCESS, result.outcome());
        assertEquals(1, result.reapplications());
        assertEquals(2, result.detailQueries());
    }

    @Test
    void reappliesOnlyAfterTwoConfirmedFailures() {
        FlowResult result = runDownloadFlow(List.of(detail("2", null), detail("2", null), detail("0", null)));

        assertEquals(VisitorProvisioningService.DownloadOutcome.SUCCESS, result.outcome());
        assertEquals(2, result.reapplications());
        assertEquals(3, result.detailQueries());
    }

    @Test
    void pendingTimeoutDoesNotCreateSecondJob() {
        FlowResult result = runDownloadFlow(List.of(
                detail("1", null), detail("1", null), detail("1", null), detail("1", null)));

        assertEquals(VisitorProvisioningService.DownloadOutcome.UNKNOWN, result.outcome());
        assertEquals(1, result.reapplications());
        assertEquals(4, result.detailQueries());
    }

    private FlowResult runDownloadFlow(List<ObjectNode> details) {
        Deque<ObjectNode> responses = new ArrayDeque<>(details);
        List<String> paths = new ArrayList<>();
        VisitorProvisioningService.Settings settings = new VisitorProvisioningService.Settings(
                "34", "56", "start", "end", 4000, 4010,
                4, 6, 4, 2, 2, 0, 0, 0, 0);
        VisitorProvisioningService service = service((path, body) -> {
            paths.add(path);
            if (path.endsWith("elementDownloadDetail")) {
                return responses.removeFirst();
            }
            return ok();
        }, settings);

        VisitorProvisioningService.DownloadOutcome outcome = service.downloadVisitorPermission("visitor-1");
        int reapplications = (int) paths.stream().filter(path -> path.endsWith("auth/reapplication")).count();
        int detailQueries = (int) paths.stream().filter(path -> path.endsWith("elementDownloadDetail")).count();
        return new FlowResult(outcome, reapplications, detailQueries);
    }

    private VisitorProvisioningService service(ArtemisApi api, VisitorProvisioningService.Settings settings) {
        return new VisitorProvisioningService(api, mapper, settings, tempDir.resolve("records.jsonl"), seconds -> {
        });
    }

    private VisitorProvisioningService.Settings defaults() {
        VisitorProvisioningService.Settings defaults = VisitorProvisioningService.Settings.pythonDefaults();
        return new VisitorProvisioningService.Settings(
                defaults.privilegeGroupId(), defaults.doorIndexCode(), defaults.visitStartTime(), defaults.visitEndTime(),
                defaults.batchStart(), defaults.batchStop(), defaults.maxApiAttempts(), defaults.groupConfirmAttempts(),
                defaults.downloadPollAttempts(), defaults.maxReapplicationAttempts(), defaults.failureConfirmationPolls(),
                0, 0, 0, 0);
    }

    private ObjectNode ok() {
        ObjectNode node = mapper.createObjectNode();
        node.put("code", "0");
        node.put("data", "");
        return node;
    }

    private ObjectNode detail(String status, String certificateStatus) {
        ObjectNode root = mapper.createObjectNode();
        root.put("code", "0");
        ObjectNode element = root.putObject("data").putObject("ElementDetailList").putArray("ElementDetail").addObject();
        element.put("ID", "56");
        ObjectNode base = element.putObject("BaseInfo");
        base.put("Name", "Door 56");
        base.put("ElementType", 0);
        base.put("Network", 0);
        element.putArray("ElementStatus").addObject().put("elementStatus", status).put("errorCode", "E-1");
        if (certificateStatus != null) {
            element.putObject("CertificateStatusList").putArray("CertificateStatus").addObject()
                    .put("ID", "card-1").put("Type", 2).put("Status", certificateStatus)
                    .put("ErrorCode", "CARD-ERROR");
        }
        return root;
    }

    private record FlowResult(VisitorProvisioningService.DownloadOutcome outcome, int reapplications, int detailQueries) {
    }
}
