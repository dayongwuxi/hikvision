import contextlib
import io
import sys
import types
import unittest
from unittest.mock import patch

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.RequestException = OSError
    requests_stub.post = None
    sys.modules["requests"] = requests_stub

import api_register_and_add_group as visitor_api


def detail(status, certificate_status=None):
    element = {
        "ID": "56",
        "BaseInfo": {"Name": "Door 56", "ElementType": 0, "Network": 0},
        "ElementStatus": [{"elementStatus": status, "errorCode": "E-1"}],
    }
    if certificate_status is not None:
        element["CertificateStatusList"] = {
            "CertificateStatus": [
                {
                    "ID": "card-1",
                    "Type": 2,
                    "Status": certificate_status,
                    "ErrorCode": "CARD-ERROR",
                }
            ]
        }
    return {
        "code": "0",
        "data": {"ElementDetailList": {"ElementDetail": [element]}},
    }


class DownloadDetailTests(unittest.TestCase):
    def test_parses_documented_object_shape_and_preserves_zero(self):
        response = {
            "data": {
                "ElementDetailList": {
                    "ElementDetail": {
                        "ID": 0,
                        "BaseInfo": {
                            "Name": "Door",
                            "ElementType": 0,
                            "Network": 0,
                        },
                        "ElementStatus": {
                            "Status": "2",
                            "ErrorModule": "ACS",
                            "ErrorCode": "DEVICE-ERROR",
                        },
                    }
                }
            }
        }

        diagnostics = visitor_api.extract_element_diagnostics(response)

        self.assertEqual(visitor_api.classify_download_detail(response), "failed")
        self.assertEqual(diagnostics[0]["id"], "0")
        self.assertEqual(diagnostics[0]["elementType"], "0")
        self.assertEqual(
            diagnostics[0]["elementStatuses"][0]["errorCode"],
            "DEVICE-ERROR",
        )

    def test_credential_failure_prevents_false_success(self):
        response = detail(0, certificate_status=2)

        self.assertEqual(visitor_api.extract_element_statuses(response), ["0"])
        self.assertEqual(visitor_api.extract_certificate_statuses(response), ["2"])
        self.assertEqual(visitor_api.classify_download_detail(response), "failed")


class GroupMembershipTests(unittest.TestCase):
    def test_searches_later_pages_for_new_visitor(self):
        pages = [
            {
                "code": "0",
                "data": {"total": 501, "list": [{"id": "another"}]},
            },
            {
                "code": "0",
                "data": {"total": 501, "list": [{"personId": "visitor-1"}]},
            },
        ]

        with patch.object(visitor_api, "post", side_effect=pages) as post_mock:
            found = visitor_api.group_contains_visitor("visitor-1")

        self.assertTrue(found)
        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(post_mock.call_args_list[1].args[1]["pageNo"], 2)

    def test_optional_person_list_failure_returns_unknown_once(self):
        with (
            patch.object(visitor_api, "post", side_effect=RuntimeError("no permission"))
            as post_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            outcome = visitor_api.wait_for_group_membership("visitor-1")

        self.assertEqual(outcome, "unknown")
        self.assertEqual(post_mock.call_count, 1)

    def test_successful_add_continues_when_person_list_is_unavailable(self):
        def fake_post(path, body):
            if path.endswith("addPersons"):
                return {"code": "0", "data": ""}
            raise RuntimeError("personList is not authorized")

        with (
            patch.object(visitor_api, "post", side_effect=fake_post),
            patch.object(visitor_api.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            outcome = visitor_api.add_visitor_to_group("visitor-1")

        self.assertEqual(outcome, "success")

    def test_uncertain_add_does_not_retry_when_person_list_is_unavailable(self):
        calls = []

        def fake_post(path, body):
            calls.append(path)
            raise RuntimeError("service unavailable")

        with (
            patch.object(visitor_api, "post", side_effect=fake_post),
            patch.object(visitor_api.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            outcome = visitor_api.add_visitor_to_group("visitor-1")

        self.assertEqual(outcome, "unknown")
        self.assertEqual(sum(path.endswith("addPersons") for path in calls), 1)


class DownloadFlowTests(unittest.TestCase):
    def run_flow(self, detail_responses):
        calls = []
        responses = iter(detail_responses)

        def fake_post(path, body):
            calls.append((path, body))
            if path.endswith("elementDownloadDetail"):
                return next(responses)
            return {"code": "0", "data": ""}

        with (
            patch.object(visitor_api, "post", side_effect=fake_post),
            patch.object(visitor_api.time, "sleep"),
            patch.object(visitor_api, "DOWNLOAD_POLL_ATTEMPTS", 4),
            patch.object(visitor_api, "MAX_REAPPLICATION_ATTEMPTS", 2),
            patch.object(visitor_api, "FAILURE_CONFIRMATION_POLLS", 2),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            outcome = visitor_api.download_visitor_permission("visitor-1")

        reapplications = [
            call for call in calls if call[0].endswith("auth/reapplication")
        ]
        detail_queries = [
            call for call in calls if call[0].endswith("elementDownloadDetail")
        ]
        return outcome, reapplications, detail_queries

    def test_old_failure_snapshot_then_success_does_not_reapply(self):
        outcome, reapplications, detail_queries = self.run_flow(
            [detail(2), detail(0)]
        )

        self.assertEqual(outcome, "success")
        self.assertEqual(len(reapplications), 1)
        self.assertEqual(len(detail_queries), 2)

    def test_reapplies_only_after_two_confirmed_failures(self):
        outcome, reapplications, detail_queries = self.run_flow(
            [detail(2), detail(2), detail(0)]
        )

        self.assertEqual(outcome, "success")
        self.assertEqual(len(reapplications), 2)
        self.assertEqual(len(detail_queries), 3)

    def test_pending_timeout_does_not_create_second_job(self):
        outcome, reapplications, detail_queries = self.run_flow(
            [detail(1), detail(1), detail(1), detail(1)]
        )

        self.assertEqual(outcome, "unknown")
        self.assertEqual(len(reapplications), 1)
        self.assertEqual(len(detail_queries), 4)


if __name__ == "__main__":
    unittest.main()
