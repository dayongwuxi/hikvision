import base64, hashlib, hmac, json, time
from pathlib import Path
import requests

root = Path(__file__).resolve().parent
cfg = json.load(open(root.parent / "hikvision/config/config.json", encoding="utf-8"))


def post(path, body):
    text = f"POST\napplication/json\napplication/json;charset=UTF-8\nx-ca-key:{cfg['AccessKey']}\n{path}"
    sign = base64.b64encode(hmac.new(cfg["SecretKey"].encode(), text.encode(), hashlib.sha256).digest()).decode()
    headers = {"Accept": "application/json", "Content-Type": "application/json;charset=UTF-8", "x-ca-key": cfg["AccessKey"], "x-ca-signature-headers": "x-ca-key", "X-Ca-Signature": sign}
    result = requests.post(cfg["APIbaseUrl"] + path, json=body, headers=headers, verify=False, timeout=15).json()
    if result.get("code") != "0": raise RuntimeError(result)
    return result

payload = json.load(open(root / "registration_payload.example.json", encoding="utf-8"))
for attempt in range(1, 6):
    payload["visitorInfoList"][0]["VisitorInfo"]["cards"][0]["cardNo"] = str(int(time.time() * 1000))
    visitor = post("/artemis/api/visitor/v1/registerment", payload)
    visitor_id, appoint_id = str(visitor["data"]["visitorId"]), str(visitor["data"]["appointRecordId"])
    error, waited = "unknown", False
    try:
        post("/artemis/api/acs/v1/privilege/group/single/addPersons", {"privilegeGroupId": "34", "type": 2, "list": [{"id": visitor_id}]})
        post("/artemis/api/visitor/v1/auth/reapplication", {"ImmediateDownload": 0, "personIds": visitor_id, "doorIndexCodes": "56"})
        print(f"第{attempt}次已触发下发，等待60秒后查询结果...", flush=True)
        time.sleep(60)
        waited = True
        detail = post("/artemis/api/visitor/v1/person/ID/elementDownloadDetail", {"id": visitor_id})
        status = detail["data"]["ElementDetailList"]["ElementDetail"][0]["ElementStatus"][0]["elementStatus"]
        if str(status) == "0":
            print(json.dumps({"success": True, "visitorId": visitor_id, "attempts": attempt, "detail": detail}, ensure_ascii=False, indent=2)); break
        error = f"elementStatus={status}"
    except Exception as exception:
        error = exception
    print(f"第{attempt}次下发失败: {error}，正在CheckOut")
    post("/artemis/api/visitor/v1/visitor/out", {"appointRecordId": appoint_id})
    if attempt < 5 and not waited:
        print("本轮在等待前失败，等待60秒后再重试...", flush=True)
        time.sleep(60)
else:
    raise RuntimeError("重新创建并下发5次后仍未成功")