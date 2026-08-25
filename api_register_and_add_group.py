import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Literal

import requests


root = Path(__file__).resolve().parent
with (root / "config/config.json").open(encoding="utf-8") as config_file:
    cfg = json.load(config_file)

PRIVILEGE_GROUP_ID = "34"
DOOR_INDEX_CODE = "56"
VISIT_START_TIME = "2026-08-24T23:00:00+09:00"
VISIT_END_TIME = "2026-12-31T23:59:59+09:00"

BATCH_START = 4000
BATCH_STOP = BATCH_START + 10
MAX_API_ATTEMPTS = 4
GROUP_CONFIRM_ATTEMPTS = 6
DOWNLOAD_POLL_ATTEMPTS = 6
MAX_REAPPLICATION_ATTEMPTS = 3
FAILURE_CONFIRMATION_POLLS = 2
RETRY_INTERVAL_SECONDS = 20
GROUP_CONFIRM_INTERVAL_SECONDS = 5
DOWNLOAD_POLL_INTERVAL_SECONDS = 20
CLEANUP_GRACE_SECONDS = 30
SUCCESS_RECORD_PATH = root / "successful_visitor_records.jsonl"

DownloadOutcome = Literal["success", "failed", "unknown"]
MembershipCheck = Literal["confirmed", "not_confirmed", "unknown"]
GroupAddOutcome = Literal["success", "failed", "unknown"]


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
        raise RuntimeError(f"path={path}, 海康 API 返回结果不是 JSON 对象")
    if str(result.get("code")) != "0":
        raise RuntimeError(
            f"path={path}, code={result.get('code')}, "
            f"msg={result.get('msg')}, data={result.get('data')}"
        )
    return result


def as_list(value: Any) -> list[Any]:
    """Normalize an Artemis object-or-array field to a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def first_value(mapping: dict[str, Any], *keys: str) -> Any:
    """Get the first present key without treating 0 or an empty string as missing."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def text_value(value: Any) -> str:
    """Convert an optional value to text while preserving numeric zero."""
    return "" if value is None else str(value)


def normalize_status(status: Any) -> dict[str, str]:
    """Normalize status fields used by different HikCentral versions."""
    if not isinstance(status, dict):
        return {"status": str(status), "errorModule": "", "errorCode": ""}

    status_value = first_value(status, "elementStatus", "Status", "status")
    if status_value is None:
        return {}
    return {
        "status": str(status_value),
        "errorModule": text_value(
            first_value(status, "ErrorModule", "errorModule")
        ),
        "errorCode": text_value(first_value(status, "ErrorCode", "errorCode")),
    }


def extract_element_diagnostics(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract device and credential status details from a download response."""
    data = detail.get("data")
    if not isinstance(data, dict):
        return []

    detail_list = first_value(data, "ElementDetailList", "elementDetailList")
    if isinstance(detail_list, dict):
        elements = first_value(detail_list, "ElementDetail", "elementDetail")
    else:
        elements = detail_list

    diagnostics: list[dict[str, Any]] = []
    for element in as_list(elements):
        if not isinstance(element, dict):
            continue

        base_info = first_value(element, "BaseInfo", "baseInfo")
        if not isinstance(base_info, dict):
            base_info = {}

        raw_element_statuses = first_value(
            element, "ElementStatus", "elementStatus"
        )
        element_statuses = [
            normalized
            for item in as_list(raw_element_statuses)
            if (normalized := normalize_status(item))
        ]

        certificate_list = first_value(
            element, "CertificateStatusList", "certificateStatusList"
        )
        if isinstance(certificate_list, dict):
            raw_certificates = first_value(
                certificate_list, "CertificateStatus", "certificateStatus"
            )
        else:
            raw_certificates = certificate_list

        certificate_statuses: list[dict[str, str]] = []
        for certificate in as_list(raw_certificates):
            if not isinstance(certificate, dict):
                continue
            normalized = normalize_status(certificate)
            if not normalized:
                continue
            normalized.update(
                {
                    "id": text_value(first_value(certificate, "ID", "id")),
                    "type": text_value(first_value(certificate, "Type", "type")),
                }
            )
            certificate_statuses.append(normalized)

        diagnostics.append(
            {
                "id": text_value(first_value(element, "ID", "id")),
                "name": text_value(
                    first_value(base_info, "Name", "name")
                    or first_value(element, "Name", "name")
                ),
                "elementType": text_value(
                    first_value(base_info, "ElementType", "elementType")
                ),
                "network": text_value(
                    first_value(base_info, "Network", "network")
                ),
                "elementStatuses": element_statuses,
                "certificateStatuses": certificate_statuses,
            }
        )
    return diagnostics


def extract_element_statuses(detail: dict[str, Any]) -> list[str]:
    """Extract normalized elementStatus values from a download response."""
    statuses: list[str] = []
    for diagnostic in extract_element_diagnostics(detail):
        for status in diagnostic["elementStatuses"]:
            statuses.append(status["status"])
    return statuses


def extract_certificate_statuses(detail: dict[str, Any]) -> list[str]:
    """Extract normalized card, face, fingerprint, and other credential statuses."""
    statuses: list[str] = []
    for diagnostic in extract_element_diagnostics(detail):
        for status in diagnostic["certificateStatuses"]:
            statuses.append(status["status"])
    return statuses


def classify_download_detail(detail: dict[str, Any]) -> str:
    """Classify the asynchronous device result without hiding unknown states."""
    element_statuses = extract_element_statuses(detail)
    certificate_statuses = extract_certificate_statuses(detail)
    statuses = element_statuses + certificate_statuses
    if statuses and all(status == "0" for status in statuses):
        return "success"
    if any(status in {"2", "3"} for status in statuses):
        return "failed"
    if statuses:
        return "pending"
    return "unknown"


def print_download_detail(label: str, detail: dict[str, Any]) -> None:
    """Print both normalized diagnostics and the original Hikvision response."""
    print(label)
    print(
        json.dumps(
            {
                "diagnostics": extract_element_diagnostics(detail),
                "rawResponse": detail,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def group_contains_visitor(visitor_id: str) -> bool:
    """Check whether the visitor is visible in the configured privilege group."""
    page_size = 500
    page_no = 1
    while True:
        result = post(
            "/artemis/api/acs/v1/privilege/group/single/personList",
            {
                "privilegeGroupId": PRIVILEGE_GROUP_ID,
                "type": 2,
                "pageNo": page_no,
                "pageSize": page_size,
            },
        )
        data = result.get("data")
        if not isinstance(data, dict):
            return False
        people = data.get("list")
        if isinstance(people, dict):
            people = [people]
        if not isinstance(people, list):
            return False
        if any(
            isinstance(person, dict)
            and str(first_value(person, "id", "personId")) == visitor_id
            for person in people
        ):
            return True

        try:
            total = int(data.get("total", len(people)))
        except (TypeError, ValueError):
            total = len(people)
        if not people or page_no * page_size >= total:
            return False
        page_no += 1


def wait_for_group_membership(
    visitor_id: str,
    expected: bool = True,
) -> MembershipCheck:
    """Return confirmed, not_confirmed, or unknown for an optional reference API."""
    for attempt in range(1, GROUP_CONFIRM_ATTEMPTS + 1):
        try:
            is_member = group_contains_visitor(visitor_id)
            print(
                f"权限组确认：第 {attempt}/{GROUP_CONFIRM_ATTEMPTS} 次，"
                f"isMember={is_member}"
            )
            if is_member == expected:
                return "confirmed"
        except Exception as exc:
            # personList 在海康流程中是可选参考接口。权限或版本不支持时，
            # 不应重复调用，也不能据此推翻 addPersons 的成功结果。
            print(f"查询权限组成员失败，状态记为 unknown: {exc}")
            return "unknown"

        if attempt < GROUP_CONFIRM_ATTEMPTS:
            time.sleep(GROUP_CONFIRM_INTERVAL_SECONDS)
    return "not_confirmed"


def add_visitor_to_group(visitor_id: str) -> GroupAddOutcome:
    """Associate a visitor without making the optional personList a hard dependency."""
    for attempt in range(1, MAX_API_ATTEMPTS + 1):
        print(f"权限组关联：第 {attempt}/{MAX_API_ATTEMPTS} 次")
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
        except Exception as exc:
            print(f"加入权限组失败: {exc}")
            # 响应丢失时，服务端可能已经完成关联；先查询再决定是否重试。
            membership = wait_for_group_membership(visitor_id)
            if membership == "confirmed":
                print("虽然 addPersons 返回异常，但已确认访客存在于权限组")
                return "success"
            if membership == "unknown":
                print(
                    "addPersons 与 personList 的结果都无法确定，停止该访客的"
                    "自动下发和清理，避免重复添加或误删"
                )
                return "unknown"
            if attempt < MAX_API_ATTEMPTS:
                time.sleep(RETRY_INTERVAL_SECONDS)
                continue
            return "failed"

        membership = wait_for_group_membership(visitor_id)
        if membership == "confirmed":
            return "success"
        if membership == "unknown":
            print(
                "addPersons 已明确成功，但 personList 无法使用；"
                f"等待 {RETRY_INTERVAL_SECONDS} 秒后继续设备下发"
            )
            time.sleep(RETRY_INTERVAL_SECONDS)
            return "success"

        print("addPersons 已成功，但多次查询仍未在权限组中找到访客")
        return "failed"

    return "failed"


def poll_download_result(visitor_id: str) -> DownloadOutcome:
    """Poll one application job without creating another application job."""
    consecutive_failures = 0
    for poll_attempt in range(1, DOWNLOAD_POLL_ATTEMPTS + 1):
        print(
            f"等待 {DOWNLOAD_POLL_INTERVAL_SECONDS} 秒后查询下发结果："
            f"第 {poll_attempt}/{DOWNLOAD_POLL_ATTEMPTS} 次"
        )
        time.sleep(DOWNLOAD_POLL_INTERVAL_SECONDS)

        try:
            # 查询访客权限在设备上的下发结果。
            detail = post(
                "/artemis/api/visitor/v1/person/ID/elementDownloadDetail",
                {"id": visitor_id},
            )
        except Exception as exc:
            print(f"查询设备下发结果失败: {exc}")
            continue

        status = classify_download_detail(detail)
        statuses = extract_element_statuses(detail)
        print(f"下发查询结果：classification={status}, elementStatus={statuses}")

        if status == "success":
            print_download_detail("●下发成功●", detail)
            return "success"

        if status == "failed":
            consecutive_failures += 1
            print_download_detail(
                "设备返回下发失败，保留完整错误信息：",
                detail,
            )
            if consecutive_failures >= FAILURE_CONFIRMATION_POLLS:
                print(
                    f"连续 {consecutive_failures} 次确认下发失败，"
                    "允许进入重新下发处理"
                )
                return "failed"
            print("先继续查询一次，避免把重新下发前的旧失败快照误判为新任务失败")
            continue

        consecutive_failures = 0
        if status == "pending":
            print("设备任务仍处于待处理状态，继续查询，不重复触发下发")
        else:
            print_download_detail(
                "返回结构中没有可识别的 elementStatus，继续查询：",
                detail,
            )

    print("在查询期限内没有得到明确的成功或连续失败结果")
    return "unknown"


def download_visitor_permission(visitor_id: str) -> DownloadOutcome:
    """Apply once, poll separately, and only reapply after confirmed failure."""
    for attempt in range(1, MAX_REAPPLICATION_ATTEMPTS + 1):
        action_name = "首次下发" if attempt == 1 else "重新下发"
        print(
            f"{action_name}：第 {attempt}/{MAX_REAPPLICATION_ATTEMPTS} 个下发任务"
        )
        try:
            # ImmediateDownload=0：立即下发，并包含以前下发失败的人员。
            post(
                "/artemis/api/visitor/v1/auth/reapplication",
                {
                    "ImmediateDownload": 0,
                    "personIds": visitor_id,
                    "doorIndexCodes": DOOR_INDEX_CODE,
                },
            )
        except Exception as exc:
            print(f"触发设备下发失败: {exc}")
            if attempt < MAX_REAPPLICATION_ATTEMPTS:
                time.sleep(RETRY_INTERVAL_SECONDS)
                continue
            return "unknown"

        outcome = poll_download_result(visitor_id)
        if outcome == "success":
            return "success"
        if outcome == "unknown":
            # 未知状态下不能确定前一个任务是否仍在执行，禁止盲目创建新任务。
            return "unknown"

        if attempt < MAX_REAPPLICATION_ATTEMPTS:
            print(
                f"本次下发已确认失败，等待 {RETRY_INTERVAL_SECONDS} 秒后重新下发"
            )
            time.sleep(RETRY_INTERVAL_SECONDS)

    return "failed"


def final_download_check(visitor_id: str) -> DownloadOutcome:
    """Perform a grace-period check before any destructive cleanup."""
    print(f"清理前等待 {CLEANUP_GRACE_SECONDS} 秒并做最后一次设备状态确认")
    time.sleep(CLEANUP_GRACE_SECONDS)
    try:
        detail = post(
            "/artemis/api/visitor/v1/person/ID/elementDownloadDetail",
            {"id": visitor_id},
        )
    except Exception as exc:
        print(f"清理前最终查询失败，状态不确定，禁止自动清理: {exc}")
        return "unknown"

    status = classify_download_detail(detail)
    print_download_detail(f"清理前最终状态：{status}", detail)
    if status == "success":
        return "success"
    if status == "failed":
        return "failed"
    return "unknown"


def save_successful_visitor(
    visitor_id: str,
    appoint_record_id: str,
    visitor_name: str,
) -> None:
    """Persist IDs required for later check-out or re-entry operations."""
    record = {
        "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "visitorGivenName": visitor_name,
        "visitorId": visitor_id,
        "appointRecordId": appoint_record_id,
        "privilegeGroupId": PRIVILEGE_GROUP_ID,
        "doorIndexCode": DOOR_INDEX_CODE,
        "visitStartTime": VISIT_START_TIME,
        "visitEndTime": VISIT_END_TIME,
        "checkin": "ON",
    }
    with SUCCESS_RECORD_PATH.open("a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"已保存 visitorId 和 appointRecordId: {SUCCESS_RECORD_PATH}")


def cleanup_failed_visitor(
    visitor_id: str,
    appoint_record_id: str,
    visitor_name: str,
) -> None:
    """Clean up only after failure was confirmed and membership removal is visible."""
    try:
        is_member = group_contains_visitor(visitor_id)
    except Exception as exc:
        print(f"清理前查询权限组成员失败，将尝试执行解除权限组: {exc}")
        is_member = True

    if not is_member:
        print(f"访客已不在权限组中，无需再次解除: {visitor_name}")
    else:
        try:
            post(
                "/artemis/api/acs/v1/privilege/group/single/deletePersons",
                {
                    "privilegeGroupId": PRIVILEGE_GROUP_ID,
                    "type": 2,
                    "list": [{"id": visitor_id}],
                },
            )
            print(f"解除权限组请求成功: {visitor_name}")
        except Exception as exc:
            print(
                f"解除权限组失败，为保留后续撤权能力，停止签退和删除人员: "
                f"{visitor_name}, {exc}"
            )
            return

        removal_check = wait_for_group_membership(visitor_id, expected=False)
        if removal_check != "confirmed":
            reason = (
                "personList 不可用"
                if removal_check == "unknown"
                else "访客仍在权限组中"
            )
            print(
                f"未确认访客已从权限组移除（{reason}），"
                "为避免平台和设备状态失去关联，停止签退和删除人员"
            )
            return

    cleanup_actions = [
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
            # 权限组已经确认解除；一个后续动作失败时仍尝试其余动作。
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
        group_outcome = add_visitor_to_group(visitor_id)
        if group_outcome == "unknown":
            print(
                "权限组关联结果不确定，本次不继续下发，也不执行自动清理；"
                "请根据 visitorId 人工核对"
            )
            continue
        if group_outcome == "failed":
            print("权限组关联失败，开始清理未下发的访客")
            cleanup_failed_visitor(
                visitor_id,
                appoint_record_id,
                visitor_name,
            )
            continue

        download_outcome = download_visitor_permission(visitor_id)
        if download_outcome == "success":
            try:
                save_successful_visitor(
                    visitor_id,
                    appoint_record_id,
                    visitor_name,
                )
            except Exception as exc:
                print(f"下发成功，但保存访客ID记录失败: {exc}")
            continue

        final_outcome = final_download_check(visitor_id)
        if final_outcome == "success":
            print("最后确认时发现设备下发已经成功，取消清理")
            try:
                save_successful_visitor(
                    visitor_id,
                    appoint_record_id,
                    visitor_name,
                )
            except Exception as exc:
                print(f"下发成功，但保存访客ID记录失败: {exc}")
        elif download_outcome == "failed" and final_outcome == "failed":
            print("最终确认仍为下发失败，开始统一清理访客")
            cleanup_failed_visitor(
                visitor_id,
                appoint_record_id,
                visitor_name,
            )
        else:
            print(
                "没有得到两阶段一致的失败结论，为避免删除仍在异步处理的访客，"
                "本次不执行自动清理"
            )


if __name__ == "__main__":
    main()
