import base64, hashlib, hmac, json
from pathlib import Path
import requests

root = Path(__file__).resolve().parent
cfg = json.load(open(root.parent / "hikvision/config/config.json", encoding="utf-8"))

body = json.load(open(root / "registration_payload.example.json", encoding="utf-8"))
path = "/artemis/api/visitor/v1/registerment"
text = f"POST\napplication/json\napplication/json;charset=UTF-8\nx-ca-key:{cfg['AccessKey']}\n{path}"
sign = base64.b64encode(hmac.new(cfg["SecretKey"].encode(), text.encode(), hashlib.sha256).digest()).decode()
headers = {"Accept": "application/json",
           "Content-Type": "application/json;charset=UTF-8",
           "x-ca-key": cfg["AccessKey"],
           "x-ca-signature-headers": "x-ca-key",
           "X-Ca-Signature": sign}
response = requests.post(cfg["APIbaseUrl"] + path, json=body, headers=headers, verify=False, timeout=15)
print(response.status_code, response.json())
