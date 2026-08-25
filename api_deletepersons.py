import base64
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional, Sequence

import requests


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "config.json"
SUCCESS_RECORD_PATH = ROOT / "successful_visitor_records.jsonl"

PRIVILEGE_GROUP_ID = "34"
DELETE_PERSONS_PATH = (
    "/artemis/api/acs/v1/privilege/group/single/deletePersons"
)

# 在这里填写需要清除预约权限的 visitorId，可填写一个或多个。
# 例如：visitorIds = ["11430", "11431"]
visitorIds: list[str] = ["11529"]


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


def delete_persons(visitor_ids: Sequence[str], config: dict[str, Any]) -> None:
    """Remove visitors from the fixed privilege group, or raise on failure."""
    body = {
        "privilegeGroupId": PRIVILEGE_GROUP_ID,
        "type": 2,
        "list": [{"id": visitor_id} for visitor_id in visitor_ids],
    }
    response = requests.post(
        str(config["APIbaseUrl"]).rstrip("/") + DELETE_PERSONS_PATH,
        json=body,
        headers=build_headers(DELETE_PERSONS_PATH, config),
        verify=False,
        timeout=15,
    )
    response.raise_for_status()

    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("海康 API 返回结果不是 JSON 对象")
    if str(result.get("code")) != "0":
        raise RuntimeError(
            f"code={result.get('code')}, msg={result.get('msg')}, "
            f"data={result.get('data')}"
        )


def read_records(record_path: Path) -> list[dict[str, Any]]:
    """Read JSONL, including the legacy CP932 full-width comma corruption."""
    raw_data = record_path.read_bytes()
    # An earlier local edit wrote CP932 0x81 0x43 (a full-width comma) between
    # JSON properties. It cannot be decoded as UTF-8, so repair only the exact
    # property-boundary byte sequence before parsing.
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


def prepare_updated_records(
    record_path: Path,
    visitor_ids: Sequence[str],
) -> tuple[list[dict[str, Any]], int, int]:
    """Validate records and prepare matching check-in states for atomic output."""
    records = read_records(record_path)
    requested_ids = set(visitor_ids)
    matched_ids: set[str] = set()
    changed_count = 0

    for record in records:
        visitor_id = str(record.get("visitorId", ""))
        if visitor_id not in requested_ids:
            continue
        matched_ids.add(visitor_id)
        if record.get("checkin") != "OFF":
            record["checkin"] = "OFF"
            changed_count += 1

    missing_ids = requested_ids - matched_ids
    if missing_ids:
        raise ValueError(
            "记录文件中找不到 visitorId: " + ", ".join(sorted(missing_ids))
        )
    return records, len(matched_ids), changed_count


def stage_records(record_path: Path, records: Sequence[dict[str, Any]]) -> Path:
    """Write and flush the replacement JSONL before the destructive API call."""
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
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def main() -> int:
    visitor_ids = list(dict.fromkeys(str(item).strip() for item in visitorIds))
    if not visitor_ids:
        print("失败：请先在程序顶部的 visitorIds 数组中填写 visitorId")
        return 2
    if any(not visitor_id for visitor_id in visitor_ids):
        print("失败：visitorIds 数组中不能包含空的 visitorId")
        return 2

    temp_path: Optional[Path] = None
    try:
        records, matched_count, changed_count = prepare_updated_records(
            SUCCESS_RECORD_PATH,
            visitor_ids,
        )
        temp_path = stage_records(SUCCESS_RECORD_PATH, records)
        config = load_config()
        delete_persons(visitor_ids, config)
        os.replace(temp_path, SUCCESS_RECORD_PATH)
        temp_path = None
    except Exception as exc:
        print(f"失败：{exc}")
        return 1
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    print(
        f"成功：已从权限组 {PRIVILEGE_GROUP_ID} 清除 "
        f"visitorId={', '.join(visitor_ids)} 的预约权限。"
    )
    print(
        f"已更新 {SUCCESS_RECORD_PATH.name}：匹配 {matched_count} 个 visitorId，"
        f"修改 {changed_count} 条记录为 checkin=OFF。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
