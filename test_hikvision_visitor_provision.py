import tempfile
import unittest
from pathlib import Path

from hikvision_visitor_provision import (
    ArtemisTransportError,
    ProvisionStore,
    provision_visitor,
)


SUCCESS_DETAIL = {
    "code": "0",
    "data": {
        "ElementDetailList": {
            "ElementDetail": [
                {"ElementStatus": [{"elementStatus": 0}]}
            ]
        }
    },
}


class FakeClient:
    def __init__(self):
        self.register_calls = 0
        self.assign_calls = 0
        self.reapply_calls = 0
        self.detail_calls = 0

    def register_visitor(self, _payload):
        self.register_calls += 1
        return {
            "code": "0",
            "data": {
                "visitorId": "visitor-001",
                "appointRecordId": "appointment-001",
                "qrCodeImage": "qr",
            },
        }

    def add_person(self, _visitor_id, _group_id):
        self.assign_calls += 1
        return {"code": "1"} if self.assign_calls < 2 else {"code": "0"}

    def trigger_download(self, _visitor_id, _door_id):
        self.reapply_calls += 1
        return {"code": "0"}

    def download_detail(self, _visitor_id):
        self.detail_calls += 1
        if self.detail_calls < 2:
            return {
                "code": "0",
                "data": {
                    "ElementDetailList": {
                        "ElementDetail": [
                            {"ElementStatus": [{"elementStatus": 1}]}
                        ]
                    }
                },
            }
        return SUCCESS_DETAIL


class TimeoutClient(FakeClient):
    def register_visitor(self, _payload):
        self.register_calls += 1
        raise ArtemisTransportError("timeout")


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "state.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_provision(self, client, key="booking-001"):
        return provision_visitor(
            client=client,
            store=ProvisionStore(self.database),
            business_key=key,
            registration={"visitorInfoList": []},
            privilege_group_id="group-001",
            door_index_code="door-001",
            max_attempts=5,
            retry_interval_seconds=0,
            sleeper=lambda _seconds: None,
        )

    def test_registration_once_and_downstream_independent_retries(self):
        client = FakeClient()
        result = self.run_provision(client)

        self.assertTrue(result["success"])
        self.assertEqual(client.register_calls, 1)
        self.assertEqual(client.assign_calls, 2)
        self.assertEqual(client.reapply_calls, 1)
        self.assertEqual(client.detail_calls, 2)

        second_client = FakeClient()
        second_result = self.run_provision(second_client)
        self.assertTrue(second_result["success"])
        self.assertTrue(second_result["reusedVisitor"])
        self.assertEqual(second_client.register_calls, 0)

    def test_timeout_is_not_automatically_registered_again(self):
        first_client = TimeoutClient()
        first_result = self.run_provision(first_client, "booking-timeout")
        self.assertEqual(first_result["status"], "REGISTER_UNKNOWN")
        self.assertEqual(first_client.register_calls, 1)

        second_client = FakeClient()
        second_result = self.run_provision(second_client, "booking-timeout")
        self.assertFalse(second_result["success"])
        self.assertEqual(second_result["status"], "REGISTER_UNKNOWN")
        self.assertEqual(second_client.register_calls, 0)


if __name__ == "__main__":
    unittest.main()
