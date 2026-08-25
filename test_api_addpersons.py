import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.post = None
    sys.modules["requests"] = requests_stub

import api_addpersons as add_api


def detail(status: int, certificate_status: int | None = None):
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


class RecordTests(unittest.TestCase):
    def test_selects_only_off_visitors_and_reads_door_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = Path(temp_dir) / "records.jsonl"
            records = [
                {"visitorId": "11430", "doorIndexCode": "56", "checkin": "OFF"},
                {"visitorId": "11431", "doorIndexCode": "57", "checkin": "ON"},
            ]
            record_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )

            selected, skipped = add_api.select_off_visitors(
                record_path, ["11430", "11431"]
            )

        self.assertEqual(selected, [("11430", "56")])
        self.assertEqual(skipped, ["11431"])

    def test_missing_door_code_is_rejected_before_api_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = Path(temp_dir) / "records.jsonl"
            record_path.write_text(
                '{"visitorId": "11430", "checkin": "OFF"}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "doorIndexCode"):
                add_api.select_off_visitors(record_path, ["11430"])

    def test_checkin_update_is_scoped_to_successful_visitor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = Path(temp_dir) / "records.jsonl"
            record_path.write_text(
                '{"visitorId": "11430", "checkin": "OFF"}\n'
                '{"visitorId": "11431", "checkin": "OFF"}\n',
                encoding="utf-8",
            )

            changed = add_api.set_checkin_on(record_path, "11430")
            records = add_api.read_records(record_path)

        self.assertEqual(changed, 1)
        self.assertEqual(records[0]["checkin"], "ON")
        self.assertEqual(records[1]["checkin"], "OFF")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "AccessKey": "access",
            "SecretKey": "secret",
            "APIbaseUrl": "https://hikcentral.example/",
        }

    def test_add_person_uses_fixed_group_and_visitor_type(self):
        response = Mock()
        response.json.return_value = {"code": "0", "msg": "success"}

        with patch.object(add_api.requests, "post", return_value=response) as post:
            add_api.add_person_to_group("11430", self.config)

        response.raise_for_status.assert_called_once_with()
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "privilegeGroupId": "34",
                "type": 2,
                "list": [{"id": "11430"}],
            },
        )
        self.assertEqual(
            post.call_args.args[0],
            "https://hikcentral.example" + add_api.ADD_PERSONS_PATH,
        )

    def test_credential_failure_prevents_false_success(self):
        self.assertEqual(
            add_api.classify_download_detail(detail(0, certificate_status=2)),
            "failed",
        )

    def test_initial_success_does_not_reapply(self):
        calls = []

        def fake_post(path, body, config):
            calls.append((path, body))
            if path == add_api.DOWNLOAD_DETAIL_PATH:
                return detail(0)
            return {"code": "0", "data": ""}

        with (
            patch.object(add_api, "post", side_effect=fake_post),
            patch.object(add_api.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            outcome = add_api.process_visitor("11430", "56", self.config)

        self.assertEqual(outcome, "success")
        self.assertEqual(
            sum(path == add_api.REAPPLICATION_PATH for path, _ in calls), 0
        )

    def test_confirmed_failure_reapplies_with_record_door_code(self):
        calls = []
        responses = iter([detail(2), detail(2), detail(0)])

        def fake_post(path, body, config):
            calls.append((path, body))
            if path == add_api.DOWNLOAD_DETAIL_PATH:
                return next(responses)
            return {"code": "0", "data": ""}

        with (
            patch.object(add_api, "post", side_effect=fake_post),
            patch.object(add_api.time, "sleep"),
            patch.object(add_api, "DOWNLOAD_POLL_ATTEMPTS", 4),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            outcome = add_api.process_visitor("11430", "door-from-record", self.config)

        reapplications = [
            body for path, body in calls if path == add_api.REAPPLICATION_PATH
        ]
        self.assertEqual(outcome, "success")
        self.assertEqual(len(reapplications), 1)
        self.assertEqual(
            reapplications[0],
            {
                "ImmediateDownload": 0,
                "personIds": "11430",
                "doorIndexCodes": "door-from-record",
            },
        )

    def test_pending_timeout_does_not_blindly_reapply(self):
        calls = []

        def fake_post(path, body, config):
            calls.append(path)
            if path == add_api.DOWNLOAD_DETAIL_PATH:
                return detail(1)
            return {"code": "0", "data": ""}

        with (
            patch.object(add_api, "post", side_effect=fake_post),
            patch.object(add_api.time, "sleep"),
            patch.object(add_api, "DOWNLOAD_POLL_ATTEMPTS", 2),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            outcome = add_api.process_visitor("11430", "56", self.config)

        self.assertEqual(outcome, "unknown")
        self.assertNotIn(add_api.REAPPLICATION_PATH, calls)

    def test_reapplication_is_limited_to_five_attempts(self):
        calls = []

        def fake_post(path, body, config):
            calls.append(path)
            if path == add_api.DOWNLOAD_DETAIL_PATH:
                return detail(2)
            return {"code": "0", "data": ""}

        with (
            patch.object(add_api, "post", side_effect=fake_post),
            patch.object(add_api.time, "sleep"),
            patch.object(add_api, "DOWNLOAD_POLL_ATTEMPTS", 2),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            outcome = add_api.process_visitor("11430", "56", self.config)

        self.assertEqual(outcome, "failed")
        self.assertEqual(calls.count(add_api.REAPPLICATION_PATH), 5)


class MainTests(unittest.TestCase):
    def test_only_successful_visitor_is_changed_to_on(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = Path(temp_dir) / "records.jsonl"
            record_path.write_text(
                '{"visitorId":"11430","doorIndexCode":"56","checkin":"OFF"}\n'
                '{"visitorId":"11431","doorIndexCode":"56","checkin":"OFF"}\n',
                encoding="utf-8",
            )

            with (
                patch.object(add_api, "SUCCESS_RECORD_PATH", record_path),
                patch.object(add_api, "visitorIds", ["11430", "11431"]),
                patch.object(add_api, "load_config", return_value={}),
                patch.object(
                    add_api,
                    "process_visitor",
                    side_effect=["success", "failed"],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = add_api.main()

            records = add_api.read_records(record_path)

        self.assertEqual(exit_code, 1)
        self.assertEqual(records[0]["checkin"], "ON")
        self.assertEqual(records[1]["checkin"], "OFF")


if __name__ == "__main__":
    unittest.main()
