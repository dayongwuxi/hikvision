import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, Sequence

import requests


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "config.json"
SUCCESS_RECORD_PATH = ROOT / "successful_visitor_records.jsonl"

PRIVILEGE_GROUP_ID = "34"
ADD_PERSONS_PATH = "/artemis/api/acs/v1/privilege/group/single/addPersons"
DOWNLOAD_DETAIL_PATH = (
    "/artemis/api/visitor/v1/person/ID/elementDownloadDetail"
)
REAPPLICATION_PATH = "/artemis/api/visitor/v1/auth/reapplication"

DOWNLOAD_POLL_ATTEMPTS = 6
MAX_REAPPLICATION_ATTEMPTS = 5
FAILURE_CONFIRMATION_POLLS = 2
RETRY_INTERVAL_SECONDS = 10

# 在这里填写需要 Check In 的 visitorId，可填写一个或多个。
# 只有 successful_visitor_records.jsonl 中 checkin="OFF" 的访客会被处理。
# 例如：visitorIds = ["11430", "11431"]
visitorIds: list[str] = ["11529"]

DownloadOutcome = Literal["success", "failed", "unknown"]


def load_config(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load and validate the Artemis connection settings."""
    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)

    required_keys = ("AccessKey", "SecretKey", "APIbaseUrl")
    missing = [key for key in required_keys if not config.get(key)]
    if missing:
        raise ValueError(f"配置缺少必填字段: {', '.join(missing)}")
    return config


def build_headers(path: str, config: dict[str, Any]) -> dict[str, str]:
    """Generate an Artemis signature for the exact API path."""
    text = (
        "POST\n"
        "application/json\n"
        "application/json;charset=UTF-8\n"
        f"x-ca-key:{config['AccessKey']}\n"
        f"{path}"
    )
    signature = base64.b64encode(
        hmac.new(
            str(config["SecretKey"]).encode("utf-8"),
            text.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "x-ca-key": str(config["AccessKey"]),
        "x-ca-signature-headers": "x-ca-key",
        "X-Ca-Signature": signature,
    }


def post(
    path: str,
    body: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Call one Artemis API and reject HTTP or Hikvision business errors."""
    response = requests.post(
        str(config["APIbaseUrl"]).rstrip("/") + path,
        json=body,
        headers=build_headers(path, config),
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
    """Return the first present key, preserving zero and empty strings."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def text_value(value: Any) -> str:
    return "" if value is None else str(value)


def normalize_status(status: Any) -> dict[str, str]:
    """Normalize status fields returned by different HikCentral versions."""
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
    """Extract device and credential statuses from a download response."""
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

        element_statuses = [
            normalized
            for item in as_list(
                first_value(element, "ElementStatus", "elementStatus")
            )
            if (normalized := normalize_status(item))
        ]

        certificate_list = first_value(
            element, "CertificateStatusList", "certificateStatusList"
        )
        if isinstance(certificate_list, dict):
            certificates = first_value(
                certificate_list, "CertificateStatus", "certificateStatus"
            )
        else:
            certificates = certificate_list

        certificate_statuses: list[dict[str, str]] = []
        for certificate in as_list(certificates):
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


def classify_download_detail(detail: dict[str, Any]) -> str:
    """Classify the asynchronous result without treating unknown as success."""
    statuses: list[str] = []
    for diagnostic in extract_element_diagnostics(detail):
        statuses.extend(item["status"] for item in diagnostic["elementStatuses"])
        statuses.extend(
            item["status"] for item in diagnostic["certificateStatuses"]
        )

    if statuses and all(status == "0" for status in statuses):
        return "success"
    if any(status in {"2", "3"} for status in statuses):
        return "failed"
    if statuses:
        return "pending"
    return "unknown"


def print_download_detail(label: str, detail: dict[str, Any]) -> None:
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


def read_records(record_path: Path) -> list[dict[str, Any]]:
    """Read JSONL, including the legacy CP932 full-width comma corruption."""
    raw_data = record_path.read_bytes()
    raw_data = raw_data.replace(b'"\x81\x43"', b'", "')
    text = raw_data.decode("utf-8")

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{record_path.name} 第 {line_number} 行不是有效 JSON: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"{record_path.name} 第 {line_number} 行不是 JSON 对象"
            )
        records.append(record)
    return records


def select_off_visitors(
    record_path: Path,
    visitor_ids: Sequence[str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Select requested OFF visitors and obtain each door code from JSONL."""
    records = read_records(record_path)
    requested_ids = set(visitor_ids)
    found_ids: set[str] = set()
    off_records: dict[str, list[dict[str, Any]]] = {}

    for record in records:
        visitor_id = str(record.get("visitorId", ""))
        if visitor_id not in requested_ids:
            continue
        found_ids.add(visitor_id)
        if record.get("checkin") == "OFF":
            off_records.setdefault(visitor_id, []).append(record)

    missing_ids = requested_ids - found_ids
    if missing_ids:
        raise ValueError(
            "记录文件中找不到 visitorId: " + ", ".join(sorted(missing_ids))
        )

    selected: list[tuple[str, str]] = []
    skipped: list[str] = []
    for visitor_id in visitor_ids:
        matches = off_records.get(visitor_id, [])
        if not matches:
            skipped.append(visitor_id)
            continue

        door_codes = {
            str(record.get("doorIndexCode", "")).strip() for record in matches
        }
        if "" in door_codes:
            raise ValueError(
                f"visitorId={visitor_id} 的 OFF 记录缺少 doorIndexCode"
            )
        if len(door_codes) != 1:
            raise ValueError(
                f"visitorId={visitor_id} 的 OFF 记录包含不同 doorIndexCode: "
                + ", ".join(sorted(door_codes))
            )
        selected.append((visitor_id, door_codes.pop()))

    return selected, skipped


def write_records_atomically(
    record_path: Path,
    records: Sequence[dict[str, Any]],
) -> None:
    """Replace the JSONL only after its complete new content is flushed."""
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{record_path.name}.",
        suffix=".tmp",
        dir=record_path.parent,
        delete=False,
    )
    temp_path = Path(temp_file.name)
    try:
        with temp_file:
            for record in records:
                temp_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, record_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def set_checkin_on(record_path: Path, visitor_id: str) -> int:
    """Atomically mark every matching OFF record ON after device success."""
    records = read_records(record_path)
    found = False
    changed_count = 0
    for record in records:
        if str(record.get("visitorId", "")) != visitor_id:
            continue
        found = True
        if record.get("checkin") == "OFF":
            record["checkin"] = "ON"
            changed_count += 1

    if not found:
        raise ValueError(f"记录文件中找不到 visitorId: {visitor_id}")
    if changed_count:
        write_records_atomically(record_path, records)
    return changed_count


def add_person_to_group(visitor_id: str, config: dict[str, Any]) -> None:
    """Add one visitor to the fixed meeting-room privilege group."""
    post(
        ADD_PERSONS_PATH,
        {
            "privilegeGroupId": PRIVILEGE_GROUP_ID,
            "type": 2,
            "list": [{"id": visitor_id}],
        },
        config,
    )


def poll_download_result(
    visitor_id: str,
    config: dict[str, Any],
) -> DownloadOutcome:
    """Poll one download job; require two failure snapshots before reapplying."""
    consecutive_failures = 0
    for poll_attempt in range(1, DOWNLOAD_POLL_ATTEMPTS + 1):
        print(
            f"等待 {RETRY_INTERVAL_SECONDS} 秒后查询下发结果："
            f"第 {poll_attempt}/{DOWNLOAD_POLL_ATTEMPTS} 次"
        )
        time.sleep(RETRY_INTERVAL_SECONDS)

        try:
            detail = post(DOWNLOAD_DETAIL_PATH, {"id": visitor_id}, config)
        except Exception as exc:
            print(f"查询设备下发结果失败: {exc}")
            continue

        status = classify_download_detail(detail)
        print(f"visitorId={visitor_id}，下发状态={status}")
        if status == "success":
            print_download_detail("●下发成功●", detail)
            return "success"

        if status == "failed":
            consecutive_failures += 1
            print_download_detail("设备返回下发失败：", detail)
            if consecutive_failures >= FAILURE_CONFIRMATION_POLLS:
                return "failed"
            print("继续确认一次，避免把旧的失败快照误判为新任务失败")
            continue

        consecutive_failures = 0
        if status == "pending":
            print("设备任务仍在处理中，继续查询，不重复触发下发")
        else:
            print_download_detail("返回中没有可识别的下发状态：", detail)

    print(f"visitorId={visitor_id} 在查询期限内没有得到明确结果")
    return "unknown"


def reapply_until_success(
    visitor_id: str,
    door_index_code: str,
    config: dict[str, Any],
) -> DownloadOutcome:
    """Create at most five reapplication jobs, only after confirmed failure."""
    for attempt in range(1, MAX_REAPPLICATION_ATTEMPTS + 1):
        print(
            f"重新下发 visitorId={visitor_id}："
            f"第 {attempt}/{MAX_REAPPLICATION_ATTEMPTS} 次"
        )
        try:
            post(
                REAPPLICATION_PATH,
                {
                    "ImmediateDownload": 0,
                    "personIds": visitor_id,
                    "doorIndexCodes": door_index_code,
                },
                config,
            )
        except Exception as exc:
            print(f"触发重新下发失败: {exc}")
            if attempt < MAX_REAPPLICATION_ATTEMPTS:
                print(f"等待 {RETRY_INTERVAL_SECONDS} 秒后重试")
                time.sleep(RETRY_INTERVAL_SECONDS)
                continue
            return "unknown"

        outcome = poll_download_result(visitor_id, config)
        if outcome == "success":
            return "success"
        if outcome == "unknown":
            # 前一个任务可能仍在执行；不能盲目创建第二个下发任务。
            return "unknown"
        if attempt < MAX_REAPPLICATION_ATTEMPTS:
            print("本次下发已确认失败，准备再次重新下发")

    return "failed"


def process_visitor(
    visitor_id: str,
    door_index_code: str,
    config: dict[str, Any],
) -> DownloadOutcome:
    """Add to group, inspect automatic download, then reapply on failure."""
    print(f"加入权限组 {PRIVILEGE_GROUP_ID}: visitorId={visitor_id}")
    try:
        add_person_to_group(visitor_id, config)
    except Exception as exc:
        print(f"加入权限组失败，未触发重新下发: {exc}")
        return "unknown"

    outcome = poll_download_result(visitor_id, config)
    if outcome != "failed":
        return outcome
    return reapply_until_success(visitor_id, door_index_code, config)


def normalize_visitor_ids(values: Sequence[Any]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values))


def main() -> int:
    visitor_ids = normalize_visitor_ids(visitorIds)
    if not visitor_ids:
        print("失败：请先在程序顶部的 visitorIds 数组中填写 visitorId")
        return 2
    if any(not visitor_id for visitor_id in visitor_ids):
        print("失败：visitorIds 数组中不能包含空的 visitorId")
        return 2

    try:
        selected, skipped = select_off_visitors(
            SUCCESS_RECORD_PATH,
            visitor_ids,
        )
    except Exception as exc:
        print(f"失败：{exc}")
        return 1

    if skipped:
        print(
            "跳过：以下 visitorId 在记录中不是 checkin=OFF："
            + ", ".join(skipped)
        )
    if not selected:
        print("没有需要 Check In 的访客。")
        return 0

    try:
        config = load_config()
    except Exception as exc:
        print(f"失败：{exc}")
        return 1

    succeeded: list[str] = []
    failed: list[str] = []
    for visitor_id, door_index_code in selected:
        print("▼" * 48)
        outcome = process_visitor(visitor_id, door_index_code, config)
        if outcome != "success":
            failed.append(visitor_id)
            print(
                f"visitorId={visitor_id} 未确认下发成功，保留 checkin=OFF"
            )
            continue

        try:
            changed_count = set_checkin_on(SUCCESS_RECORD_PATH, visitor_id)
        except Exception as exc:
            failed.append(visitor_id)
            print(f"下发成功，但更新 checkin 失败: {exc}")
            continue

        succeeded.append(visitor_id)
        print(
            f"Check In 成功：visitorId={visitor_id}，"
            f"已将 {changed_count} 条记录改为 checkin=ON"
        )

    print("=" * 48)
    print(f"成功 {len(succeeded)} 人: {', '.join(succeeded) or '-'}")
    print(f"失败或状态未知 {len(failed)} 人: {', '.join(failed) or '-'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
