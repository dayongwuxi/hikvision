import base64, hashlib, hmac, json
from pathlib import Path
import requests

cfg = json.load(open(Path(__file__).resolve().parent.parent / "hikvision/config/config.json", encoding="utf-8"))
path = "/artemis/api/acs/v1/privilege/group"
text = f"POST\napplication/json\napplication/json;charset=UTF-8\nx-ca-key:{cfg['AccessKey']}\n{path}"
sign = base64.b64encode(hmac.new(cfg["SecretKey"].encode(), text.encode(), hashlib.sha256).digest()).decode()


headers = {"Accept": "application/json", "Content-Type": "application/json;charset=UTF-8", "x-ca-key": cfg["AccessKey"], "x-ca-signature-headers": "x-ca-key", "X-Ca-Signature": sign}
result = requests.post(cfg["APIbaseUrl"] + path, json={"pageNo": 1, "pageSize": 100, "type": 2}, headers=headers, verify=False, timeout=15).json()
if result.get("code") != "0": raise RuntimeError(result)
print(result)
for group in result["data"]["list"]: print(group["privilegeGroupId"], group["privilegeGroupName"])

path = "/artemis/api/resource/v1/acsDoor/acsDoorList"
result = requests.post(cfg["APIbaseUrl"] + path, json={"pageNo": 1, "pageSize": 200}, headers=headers, verify=False, timeout=15).json()
print(result)