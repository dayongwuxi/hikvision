#!/usr/bin/env python3
"""Create one HikCentral visitor and independently retry device provisioning.

The registration API is never retried. A SQLite state database keyed by
business_key prevents a later invocation from creating the same visitor again.
If registration times out, the state is marked REGISTER_UNKNOWN and automatic
creation is stopped because HikCentral might already have created the visitor.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sqlite3
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REGISTER_PATH = "/artemis/api/visitor/v1/registerment"
ADD_PERSON_PATH = "/artemis/api/acs/v1/privilege/group/single/addPersons"
REAPPLICATION_PATH = "/artemis/api/visitor/v1/auth/reapplication"
DOWNLOAD_DETAIL_PATH = (
    "/artemis/api/visitor/v1/person/ID/elementDownloadDetail"
)


class ArtemisTransportError(RuntimeError):
    """HTTP, network, TLS, or response decoding error."""


class ArtemisClient:
    """Minimal HikCentral Artemis client compatible with the existing Node code."""

    def __init__(
        self,
        base_url: str,
        access_key: str,
        secret_key: str,
        *,
        timeout_seconds: float = 15.0,
        verify_tls: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context()
        if not verify_tls:
            # The project HikCentral server currently uses a private/self-signed cert.
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE

    def _headers(self, path: str) -> Dict[str, str]:
        # Keep the canonical string identical to src/exechikopenapi.js.
        canonical = (
            "POST\n"
            "application/json\n"
            "application/json;charset=UTF-8\n"
            f"x-ca-key:{self.access_key}\n"
            f"{path}"
        )
        signature = base64.b64encode(
            hmac.new(
                self.secret_key.encode("utf-8"),
                canonical.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        return {
            "Accept": "application/json",
            "Content-Type": "application/json;charset=UTF-8",
            "x-ca-key": self.access_key,
            "x-ca-signature-headers": "x-ca-key",
            "X-Ca-Signature": signature,
        }

    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body,
            headers=self._headers(path),
            method="POST",
        )
        try:
            with urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            ) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise ArtemisTransportError(
                f"HTTP {exc.code}: {response_body[:500]}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ArtemisTransportError(str(exc)) from exc

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArtemisTransportError(
                f"API returned non-JSON data: {raw[:500]}"
            ) from exc
        if not isinstance(result, dict):
            raise ArtemisTransportError("API response is not a JSON object")
        return result

    def register_visitor(self, registration: Dict[str, Any]) -> Dict[str, Any]:
        return self.post(REGISTER_PATH, registration)

    def add_person(
        self, visitor_id: str, privilege_group_id: str
    ) -> Dict[str, Any]:
        return self.post(
            ADD_PERSON_PATH,
            {
                "privilegeGroupId": privilege_group_id,
                "type": 1,
                "list": [{"id": visitor_id}],
            },
        )

    def trigger_download(
        self, visitor_id: str, door_index_code: str
    ) -> Dict[str, Any]:
        return self.post(
            REAPPLICATION_PATH,
            {
                "ImmediateDownload": 0,
                "personIds": visitor_id,
                "doorIndexCodes": door_index_code,
            },
        )

    def download_detail(self, visitor_id: str) -> Dict[str, Any]:
        return self.post(DOWNLOAD_DETAIL_PATH, {"id": visitor_id})


@dataclass
class ProvisionRecord:
    business_key: str
    status: str
    visitor_id: Optional[str]
    appoint_record_id: Optional[str]
    qr_code_image: Optional[str]
    last_error: Optional[str]


class ProvisionStore:
    """Persistent guard that prevents repeated visitor registration."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS visitor_provision (
                    business_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    visitor_id TEXT,
                    appoint_record_id TEXT,
                    qr_code_image TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> ProvisionRecord:
        return ProvisionRecord(
            business_key=row["business_key"],
            status=row["status"],
            visitor_id=row["visitor_id"],
            appoint_record_id=row["appoint_record_id"],
            qr_code_image=row["qr_code_image"],
            last_error=row["last_error"],
        )

    def claim(self, business_key: str) -> Tuple[str, ProvisionRecord]:
        """Return CREATE, RESUME, COMPLETED, or BLOCKED."""
        now = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM visitor_provision WHERE business_key=?",
                (business_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO visitor_provision
                    (business_key, status, created_at, updated_at)
                    VALUES (?, 'REGISTERING', ?, ?)
                    """,
                    (business_key, now, now),
                )
                connection.commit()
                return (
                    "CREATE",
                    ProvisionRecord(
                        business_key, "REGISTERING", None, None, None, None
                    ),
                )

            record = self._record(row)
            connection.commit()
            if record.status == "COMPLETED":
                return "COMPLETED", record
            if record.visitor_id:
                return "RESUME", record
            return "BLOCKED", record
        finally:
            connection.close()

    def update(
        self,
        business_key: str,
        status: str,
        *,
        visitor_id: Optional[str] = None,
        appoint_record_id: Optional[str] = None,
        qr_code_image: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE visitor_provision
                   SET status=?,
                       visitor_id=COALESCE(?, visitor_id),
                       appoint_record_id=COALESCE(?, appoint_record_id),
                       qr_code_image=COALESCE(?, qr_code_image),
                       last_error=?,
                       updated_at=?
                 WHERE business_key=?
                """,
                (
                    status,
                    visitor_id,
                    appoint_record_id,
                    qr_code_image,
                    last_error,
                    now,
                    business_key,
                ),
            )
            connection.commit()
        finally:
            connection.close()


def api_succeeded(response: Dict[str, Any]) -> bool:
    return str(response.get("code")) == "0"


def download_completed(response: Dict[str, Any]) -> bool:
    """Match the elementStatus == 0 success rule used by the Node backend."""
    if not api_succeeded(response):
        return False

    data = response.get("data")
    if not isinstance(data, dict):
        return False
    detail_list = data.get("ElementDetailList")
    if not isinstance(detail_list, dict):
        return False
    details = detail_list.get("ElementDetail")
    if isinstance(details, dict):
        details = [details]
    if not isinstance(details, list) or not details:
        return False

    statuses = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        item_statuses = detail.get("ElementStatus", [])
        if isinstance(item_statuses, dict):
            item_statuses = item_statuses.get("ElementStatus", item_statuses)
        if isinstance(item_statuses, dict):
            item_statuses = [item_statuses]
        if isinstance(item_statuses, list):
            statuses.extend(
                item
                for item in item_statuses
                if isinstance(item, dict) and "elementStatus" in item
            )

    return bool(statuses) and all(
        str(item["elementStatus"]) == "0" for item in statuses
    )


def retry_step(
    operation: Callable[[], Dict[str, Any]],
    success: Callable[[Dict[str, Any]], bool],
    *,
    max_attempts: int,
    interval_seconds: float,
    sleeper: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    last_response: Optional[Dict[str, Any]] = None
    errors = []
    for attempt in range(1, max_attempts + 1):
        try:
            last_response = operation()
            if success(last_response):
                return {
                    "success": True,
                    "attempts": attempt,
                    "response": last_response,
                }
            errors.append(
                f"attempt {attempt}: API code={last_response.get('code')}, "
                f"msg={last_response.get('msg', '')}"
            )
        except ArtemisTransportError as exc:
            errors.append(f"attempt {attempt}: {exc}")

        if attempt < max_attempts:
            sleeper(interval_seconds)

    return {
        "success": False,
        "attempts": max_attempts,
        "response": last_response,
        "errors": errors,
    }


def provision_visitor(
    *,
    client: ArtemisClient,
    store: ProvisionStore,
    business_key: str,
    registration: Dict[str, Any],
    privilege_group_id: str,
    door_index_code: str,
    max_attempts: int = 5,
    retry_interval_seconds: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Register once, then retry assignment/download and return the final result."""
    action, record = store.claim(business_key)
    result: Dict[str, Any] = {
        "success": False,
        "businessKey": business_key,
        "visitorCreated": False,
        "reusedVisitor": action in {"RESUME", "COMPLETED"},
        "visitorId": record.visitor_id,
        "appointRecordId": record.appoint_record_id,
        "steps": {},
    }

    if action == "COMPLETED":
        result.update(
            {
                "success": True,
                "status": "COMPLETED",
                "message": "该业务键已处理完成，未重复创建访客。",
            }
        )
        return result

    if action == "BLOCKED":
        result.update(
            {
                "status": record.status,
                "message": (
                    "登记结果不确定或登记已失败。为防止重复访客，"
                    "程序不会再次调用 registerment；请先在海康查询并补录 visitorId。"
                ),
                "error": record.last_error,
            }
        )
        return result

    if action == "CREATE":
        # Deliberately no retry around this call.
        try:
            register_response = client.register_visitor(registration)
        except ArtemisTransportError as exc:
            store.update(
                business_key,
                "REGISTER_UNKNOWN",
                last_error=str(exc),
            )
            result["steps"]["register"] = {
                "success": False,
                "attempts": 1,
                "error": str(exc),
            }
            result.update(
                {
                    "status": "REGISTER_UNKNOWN",
                    "message": (
                        "访客登记请求结果不确定，已停止自动重试，"
                        "避免在海康生成重复访客。"
                    ),
                }
            )
            return result

        result["steps"]["register"] = {
            "success": api_succeeded(register_response),
            "attempts": 1,
            "response": register_response,
        }
        if not api_succeeded(register_response):
            error = (
                f"code={register_response.get('code')}, "
                f"msg={register_response.get('msg', '')}"
            )
            store.update(
                business_key, "REGISTER_FAILED", last_error=error
            )
            result.update(
                {
                    "status": "REGISTER_FAILED",
                    "message": "海康访客登记失败，登记接口未重试。",
                }
            )
            return result

        data = register_response.get("data")
        if not isinstance(data, dict) or not data.get("visitorId"):
            error = "registerment returned code 0 but visitorId is missing"
            store.update(
                business_key, "REGISTER_UNKNOWN", last_error=error
            )
            result.update(
                {
                    "status": "REGISTER_UNKNOWN",
                    "message": error,
                }
            )
            return result

        visitor_id = str(data["visitorId"])
        appoint_record_id = (
            str(data["appointRecordId"])
            if data.get("appointRecordId") is not None
            else None
        )
        qr_code_image = data.get("qrCodeImage")
        store.update(
            business_key,
            "REGISTERED",
            visitor_id=visitor_id,
            appoint_record_id=appoint_record_id,
            qr_code_image=qr_code_image,
        )
        result.update(
            {
                "visitorCreated": True,
                "visitorId": visitor_id,
                "appointRecordId": appoint_record_id,
            }
        )
    else:
        visitor_id = str(record.visitor_id)

    assign_result = retry_step(
        lambda: client.add_person(visitor_id, privilege_group_id),
        api_succeeded,
        max_attempts=max_attempts,
        interval_seconds=retry_interval_seconds,
        sleeper=sleeper,
    )
    result["steps"]["assignPrivilege"] = assign_result
    if not assign_result["success"]:
        store.update(
            business_key,
            "ASSIGN_FAILED",
            last_error=json.dumps(assign_result, ensure_ascii=False),
        )
        result.update(
            {
                "status": "ASSIGN_FAILED",
                "message": "访客已创建，但加入权限组失败；可用相同业务键重新执行。",
            }
        )
        return result

    reapply_result = retry_step(
        lambda: client.trigger_download(visitor_id, door_index_code),
        api_succeeded,
        max_attempts=max_attempts,
        interval_seconds=retry_interval_seconds,
        sleeper=sleeper,
    )
    result["steps"]["triggerDownload"] = reapply_result
    if not reapply_result["success"]:
        store.update(
            business_key,
            "REAPPLY_FAILED",
            last_error=json.dumps(reapply_result, ensure_ascii=False),
        )
        result.update(
            {
                "status": "REAPPLY_FAILED",
                "message": "访客已创建且已加入权限组，但触发设备下发失败。",
            }
        )
        return result

    verify_result = retry_step(
        lambda: client.download_detail(visitor_id),
        download_completed,
        max_attempts=max_attempts,
        interval_seconds=retry_interval_seconds,
        sleeper=sleeper,
    )
    result["steps"]["verifyDownload"] = verify_result
    if not verify_result["success"]:
        store.update(
            business_key,
            "VERIFY_FAILED",
            last_error=json.dumps(verify_result, ensure_ascii=False),
        )
        result.update(
            {
                "status": "VERIFY_FAILED",
                "message": "设备下发已触发，但在规定次数内未确认下发成功。",
            }
        )
        return result

    store.update(business_key, "COMPLETED", last_error=None)
    result.update(
        {
            "success": True,
            "status": "COMPLETED",
            "message": "访客创建、权限分配和设备下发全部成功。",
        }
    )
    return result


def load_client_config(config_path: Path) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    required = ("APIbaseUrl", "AccessKey", "SecretKey")
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise ValueError(f"配置文件缺少字段: {', '.join(missing)}")
    return config


def write_result(result: Dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    parser = argparse.ArgumentParser(
        description="创建一次海康访客并重试权限及设备下发"
    )
    parser.add_argument("--business-key", required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--privilege-group-id", required=True)
    parser.add_argument("--door-index-code", required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=project_dir / "config" / "config.json",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=script_dir / "visitor_provision.db",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=script_dir / "result.json",
    )
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--retry-interval", type=float, default=2.0)
    parser.add_argument(
        "--verify-tls",
        action="store_true",
        help="验证服务端 TLS 证书；当前私有证书环境通常不启用",
    )
    args = parser.parse_args()

    try:
        config = load_client_config(args.config)
        with args.payload.open("r", encoding="utf-8") as file:
            registration = json.load(file)
        client = ArtemisClient(
            config["APIbaseUrl"],
            str(config["AccessKey"]),
            str(config["SecretKey"]),
            verify_tls=args.verify_tls,
        )
        result = provision_visitor(
            client=client,
            store=ProvisionStore(args.state_db),
            business_key=args.business_key,
            registration=registration,
            privilege_group_id=args.privilege_group_id,
            door_index_code=args.door_index_code,
            max_attempts=args.max_attempts,
            retry_interval_seconds=args.retry_interval,
        )
    except Exception as exc:
        result = {
            "success": False,
            "status": "PROGRAM_ERROR",
            "message": str(exc),
        }

    write_result(result, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n结果文件: {args.output.resolve()}", file=sys.stderr)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
