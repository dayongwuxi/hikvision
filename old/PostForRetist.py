# coding:utf-8
import hmac
import json
import pprint
import base64
import urllib3
import hashlib
import requests
from pathlib import Path
from urllib3.exceptions import InsecureRequestWarning
urllib3.disable_warnings(InsecureRequestWarning)

config_path = Path(__file__).resolve().parent / "config" / "config.json"
with config_path.open(encoding="utf-8") as config_file:
    config = json.load(config_file)
appKey = str(config["AccessKey"])
appSecret = str(config["SecretKey"])


def sign(key, value):
    temp = hmac.new(key.encode(), value.encode(), digestmod=hashlib.sha256)
    return base64.b64encode(temp.digest()).decode()


def openapipost(url, body):
    host = config["APIbaseUrl"].rstrip("/") + "{}"
    # print(host.format(url))
    sign_str = "POST\n*/*\napplication/json\nx-ca-key:{key}\n{url}".format(key=appKey, url=url)
    signature = sign(appSecret, sign_str)
    # print(sign_str, signature)
    headers = \
        {
            'Accept': '*/*',
            'Content-Type': 'application/json',
            'X-Ca-Signature-Headers': 'x-ca-key',
            'x-ca-key': appKey,
            'X-Ca-Signature': signature,
        }
    res = requests.post(url=host.format(url), data=json.dumps(body), headers=headers, verify=False)
    # print(res.status_code)
    pprint.pprint(res.json())


if __name__ == "__main__":
    """
    # url = '/artemis/api/common/v1/version'
    # url = '/artemis/api/visitor/v1/visitor/out'
    url = '/artemis/api/visitor/v1/registerment'
    body = {
        "receptionistId": "",
        "visitStartTime": "2024-12-14T15:00:00+09:00",
        "visitEndTime": "2024-12-15T15:10:00+09:00",
        "visitPurposeType": 0,
        "visitPurpose": "visitor",
        "visitorInfoList": [{
            "VisitorInfo": {
                "visitorFamilyName": "san",
                "visitorGivenName": "wu",
                "gender": 1,
                "email": "999999@qq.com",
                "phoneNo": "13600000000",
                "plateNo": "A666",
                "companyName": "AAA",
                "certificateType": 111,
                "certificateNo": "null",
                "remark": "visitor",
                "faces": [{
                    "faceData": ""
                    }]
            }
            }]
        }
    for i in range(100):
        openapipost(url, body)
        time.sleep(5)
    # 削除

    url = '/artemis/api/visitor/v1/visitor/out'
    body = {"appointRecordId": "7313"}
    for i in range(7333):
        recordid_str = "{}".format(7333-i)
        print(i)
        # print(recordid_str)
        body["appointRecordId"] = recordid_str
        # pprint.pprint(body)
        openapipost(url, body)
    """
    # 'visitorId': '6997'
    url = '/artemis/api/acs/v1/privilege/group/single/addPersons'
    body = {
            "privilegeGroupId": "15",
            "type": 2,
            "list": [
                {
                  "id": "6989"
                }
            ]
    }
    openapipost(url, body)
