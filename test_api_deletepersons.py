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

import api_deletepersons as delete_api


class DeletePersonsTests(unittest.TestCase):
    def test_delete_persons_uses_fixed_group_and_visitor_type(self):
        response = Mock()
        response.json.return_value = {"code": "0", "msg": "success"}

        config = {
            "AccessKey": "access",
            "SecretKey": "secret",
            "APIbaseUrl": "https://hikcentral.example/",
        }
        with patch.object(delete_api.requests, "post", return_value=response) as post:
            delete_api.delete_persons(["11430", "11431"], config)

        response.raise_for_status.assert_called_once_with()
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "privilegeGroupId": "34",
                "type": 2,
                "list": [{"id": "11430"}, {"id": "11431"}],
            },
        )
        self.assertEqual(
            post.call_args.args[0],
            "https://hikcentral.example" + delete_api.DELETE_PERSONS_PATH,
        )

    def test_business_error_is_rejected(self):
        response = Mock()
        response.json.return_value = {"code": "500", "msg": "failed"}
        config = {
            "AccessKey": "access",
            "SecretKey": "secret",
            "APIbaseUrl": "https://hikcentral.example",
        }

        with (
            patch.object(delete_api.requests, "post", return_value=response),
            self.assertRaisesRegex(RuntimeError, "code=500"),
        ):
            delete_api.delete_persons(["11430"], config)

    def test_prepares_all_matching_records_and_preserves_other_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = Path(temp_dir) / "records.jsonl"
            records = [
                {"visitorId": "11430", "checkin": "ON"},
                {"visitorId": "11431", "checkin": "ON"},
                {"visitorId": "11430", "checkin": "OFF"},
            ]
            record_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )

            updated, matched_count, changed_count = (
                delete_api.prepare_updated_records(record_path, ["11430"])
            )

        self.assertEqual(matched_count, 1)
        self.assertEqual(changed_count, 1)
        self.assertEqual(updated[0]["checkin"], "OFF")
        self.assertEqual(updated[1]["checkin"], "ON")
        self.assertEqual(updated[2]["checkin"], "OFF")

    def test_reads_existing_cp932_full_width_comma_corruption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = Path(temp_dir) / "records.jsonl"
            record_path.write_bytes(
                b'{"visitorId": "11430"\x81\x43"checkin": "ON"}\r\n'
            )

            records = delete_api.read_records(record_path)

        self.assertEqual(
            records,
            [{"visitorId": "11430", "checkin": "ON"}],
        )

    def test_missing_visitor_is_rejected_before_api_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = Path(temp_dir) / "records.jsonl"
            record_path.write_text(
                '{"visitorId": "11430", "checkin": "ON"}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "11499"):
                delete_api.prepare_updated_records(record_path, ["11499"])

    def test_api_failure_does_not_change_record_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = Path(temp_dir) / "records.jsonl"
            original = b'{"visitorId": "11430"\x81\x43"checkin": "ON"}\r\n'
            record_path.write_bytes(original)

            with (
                patch.object(delete_api, "SUCCESS_RECORD_PATH", record_path),
                patch.object(delete_api, "visitorIds", ["11430"]),
                patch.object(delete_api, "load_config", return_value={}),
                patch.object(
                    delete_api,
                    "delete_persons",
                    side_effect=RuntimeError("API failed"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = delete_api.main()

            self.assertEqual(exit_code, 1)
            self.assertEqual(record_path.read_bytes(), original)

    def test_api_success_atomically_updates_record_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = Path(temp_dir) / "records.jsonl"
            record_path.write_text(
                '{"visitorId": "11430", "checkin": "ON"}\n'
                '{"visitorId": "11431", "checkin": "ON"}\n',
                encoding="utf-8",
            )

            with (
                patch.object(delete_api, "SUCCESS_RECORD_PATH", record_path),
                patch.object(delete_api, "visitorIds", ["11430"]),
                patch.object(delete_api, "load_config", return_value={}),
                patch.object(delete_api, "delete_persons") as delete_persons,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = delete_api.main()

            updated = delete_api.read_records(record_path)
            self.assertEqual(exit_code, 0)
            delete_persons.assert_called_once_with(["11430"], {})
            self.assertEqual(updated[0]["checkin"], "OFF")
            self.assertEqual(updated[1]["checkin"], "ON")


if __name__ == "__main__":
    unittest.main()
