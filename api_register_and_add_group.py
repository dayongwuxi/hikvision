import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import requests


root = Path(__file__).resolve().parent
with (root / "config/config.json").open(encoding="utf-8") as config_file:
    cfg = json.load(config_file)

PRIVILEGE_GROUP_ID = "34"
DOOR_INDEX_CODE = "56"
VISIT_START_TIME = "2026-08-24T23:00:00+09:00"
VISIT_END_TIME = "2026-12-31T23:59:59+09:00"
BATCH_START = 3300
BATCH_STOP = BATCH_START + 10
MAX_ATTEMPTS = 4
RETRY_INTERVAL_SECONDS = 20


def build_headers(path: str) -> dict[str, str]:
    """Generate an Artemis signature for this exact API path."""
    text = (
        "POST\n"
        "application/json\n"
        "application/json;charset=UTF-8\n"
        f"x-ca-key:{cfg['AccessKey']}\n"
        f"{path}"
    )
    signature = base64.b64encode(
        hmac.new(
            str(cfg["SecretKey"]).encode("utf-8"),
            text.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "x-ca-key": str(cfg["AccessKey"]),
        "x-ca-signature-headers": "x-ca-key",
        "X-Ca-Signature": signature,
    }


def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Call one Artemis API and reject HTTP or Hikvision business errors."""
    response = requests.post(
        cfg["APIbaseUrl"] + path,
        json=body,
        headers=build_headers(path),
        verify=False,
        timeout=15,
    )
    response.raise_for_status()

    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("海康 API 返回结果不是 JSON 对象")
    if str(result.get("code")) != "0":
        raise RuntimeError(result)
    return result


def extract_element_statuses(detail: dict[str, Any]) -> list[str]:
    """Extract all elementStatus values from a download-detail response."""
    data = detail.get("data")
    if not isinstance(data, dict):
        return []
    detail_list = data.get("ElementDetailList")
    if not isinstance(detail_list, dict):
        return []
    elements = detail_list.get("ElementDetail")
    if isinstance(elements, dict):
        elements = [elements]
    if not isinstance(elements, list):
        return []

    statuses: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        element_statuses = element.get("ElementStatus")
        if isinstance(element_statuses, dict):
            element_statuses = [element_statuses]
        if not isinstance(element_statuses, list):
            continue
        for status in element_statuses:
            if isinstance(status, dict) and "elementStatus" in status:
                statuses.append(str(status["elementStatus"]))
    return statuses


def add_visitor_to_group(visitor_id: str) -> bool:
    """Retry only the privilege-group association step."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"权限组关联：第 {attempt}/{MAX_ATTEMPTS} 次")
        try:
            # 将访客加入指定门禁权限组；type=2 表示访客。
            post(
                "/artemis/api/acs/v1/privilege/group/single/addPersons",
                {
                    "privilegeGroupId": PRIVILEGE_GROUP_ID,
                    "type": 2,
                    "list": [{"id": visitor_id}],
                },
            )
            return True
        except Exception as exc:
            print(f"加入权限组失败: {exc}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_INTERVAL_SECONDS)
    return False


def download_visitor_permission(visitor_id: str) -> bool:
    """Trigger device download and retry without checking out the visitor."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"设备下发：第 {attempt}/{MAX_ATTEMPTS} 次")
        try:
            # 重新触发访客权限下发到指定门禁设备。
            post(
                "/artemis/api/visitor/v1/auth/reapplication",
                {
                    "ImmediateDownload": 0,
                    "personIds": visitor_id,
                    "doorIndexCodes": DOOR_INDEX_CODE,
                },
            )

            print(f"已触发下发，等待 {RETRY_INTERVAL_SECONDS} 秒后查询结果")
            time.sleep(RETRY_INTERVAL_SECONDS)

            # 查询访客权限在设备上的下发结果。
            detail = post(
                "/artemis/api/visitor/v1/person/ID/elementDownloadDetail",
                {"id": visitor_id},
            )
            statuses = extract_element_statuses(detail)
            if statuses and all(status == "0" for status in statuses):
                print("●下发成功●")
                print(json.dumps(detail, ensure_ascii=False, indent=2))
                return True

            if statuses:
                print(f"下发尚未成功，elementStatus={statuses}")
            else:
                print("下发结果中没有找到 elementStatus")
        except Exception as exc:
            print(f"设备下发或结果查询失败: {exc}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_INTERVAL_SECONDS)
    return False


def cleanup_failed_visitor(
    visitor_id: str,
    appoint_record_id: str,
    visitor_name: str,
) -> None:
    """After all retries fail, perform each cleanup action independently."""
    cleanup_actions = [
        (
            "解除权限组",
            "/artemis/api/acs/v1/privilege/group/single/deletePersons",
            {
                "privilegeGroupId": PRIVILEGE_GROUP_ID,
                "type": 2,
                "list": [{"id": visitor_id}],
            },
        ),
        (
            "访客签退",
            "/artemis/api/visitor/v1/visitor/out",
            {"appointRecordId": appoint_record_id},
        ),
        (
            "删除人员",
            "/artemis/api/resource/v1/person/single/delete",
            {"personId": visitor_id},
        ),
    ]

    for action_name, path, body in cleanup_actions:
        try:
            post(path, body)
            print(f"{action_name}成功: {visitor_name}")
        except Exception as exc:
            # 一个清理动作失败时仍继续执行其余动作。
            print(f"{action_name}失败: {visitor_name}, {exc}")


def main() -> None:
    with (root / "registration_payload.example.json").open(
        encoding="utf-8"
    ) as payload_file:
        visitor_data = json.load(payload_file)

    visitor_data["visitStartTime"] = VISIT_START_TIME
    visitor_data["visitEndTime"] = VISIT_END_TIME

    for batch_number in range(BATCH_START, BATCH_STOP):
        visitor_info = visitor_data["visitorInfoList"][0]["VisitorInfo"]
        visitor_name = f"apitest02{batch_number:05}"
        visitor_info["visitorGivenName"] = visitor_name
        visitor_info["certificateNo"] = f"2{batch_number:05}"
        visitor_info["cards"][0]["cardNo"] = f"3{batch_number:05}"

        print("▼" * 48)
        try:
            # 创建访客预约；此接口不自动重试，以免生成重复访客。
            visitor = post(
                "/artemis/api/visitor/v1/registerment",
                visitor_data,
            )
        except requests.RequestException as exc:
            # 网络或 HTTP 异常时，服务器可能已经创建访客；停止整个批次。
            print(
                "访客登记结果不确定，为避免继续生成重复访客，停止批次: "
                f"{exc}"
            )
            break
        except Exception as exc:
            # 明确的海康业务错误不会创建成功，可以处理下一个编号。
            print(f"访客登记失败: {exc}")
            continue

        try:
            visitor_id = str(visitor["data"]["visitorId"])
            appoint_record_id = str(visitor["data"]["appointRecordId"])
        except (KeyError, TypeError) as exc:
            print(
                "登记返回成功但缺少 visitorId 或 appointRecordId，"
                f"为避免重复创建，停止批次: {exc}"
            )
            break

        print(
            json.dumps(
                {"visitorGivenName": visitor_name, "registerment": visitor},
                ensure_ascii=False,
                indent=2,
            )
        )
        time.sleep(RETRY_INTERVAL_SECONDS)
        group_added = add_visitor_to_group(visitor_id)
        time.sleep(RETRY_INTERVAL_SECONDS)
        download_succeeded = (
            group_added and download_visitor_permission(visitor_id)
        )
        if not download_succeeded:
            print("所有重试均失败，开始统一清理访客")
            cleanup_failed_visitor(
                visitor_id,
                appoint_record_id,
                visitor_name,
            )


if __name__ == "__main__":
    main()
