import base64, hashlib, hmac, json, time
from pathlib import Path
import requests

root = Path(__file__).resolve().parent
cfg = json.load(open(root.parent / "hikvision/config/config.json", encoding="utf-8"))


def build_headers(path):
    text = (
        f"POST\n"
        f"application/json\n"
        f"application/json;charset=UTF-8\n"
        f"x-ca-key:{cfg['AccessKey']}\n"
        f"{path}"
    )

    signature = base64.b64encode(
        hmac.new(
            cfg["SecretKey"].encode(),
            text.encode(),
            hashlib.sha256,
        ).digest()
    ).decode()

    return {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "x-ca-key": cfg["AccessKey"],
        "x-ca-signature-headers": "x-ca-key",
        "X-Ca-Signature": signature,
    }


def post(path, body):
    response = requests.post(
        cfg["APIbaseUrl"] + path,
        json=body,
        headers=build_headers(path),
        verify=False,
        timeout=15,
    )
    response.raise_for_status()

    result = response.json()
    if result.get("code") != "0":
        raise RuntimeError(result)

    return result

visitor_data = json.load(open(root / "registration_payload.example.json", encoding="utf-8"))
visitor_data["visitStartTime"] = "2026-08-24T23:00:00+09:00"
visitor_data["visitEndTime"] = "2026-08-25T12:00:00+09:00"

for attempt in range(3020, 3030):

    visitorGivenName = f'apitest02{attempt:05}'
    visitor_data["visitorInfoList"][0]["VisitorInfo"]['visitorGivenName'] = visitorGivenName

    certificateNo = f'2{attempt:05}'
    visitor_data["visitorInfoList"][0]["VisitorInfo"]['certificateNo'] = certificateNo

    cardNo = f'3{attempt:05}'
    visitor_data["visitorInfoList"][0]["VisitorInfo"]['cards'][0]['cardNo'] = cardNo
    # print(visitor_data["visitorInfoList"][0]["VisitorInfo"]['cards'][0]['cardNo'])
    print("▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼")
    # print(visitor_data)

    try:
        # 创建访客
        visitor = post("/artemis/api/visitor/v1/registerment", visitor_data)
    except Exception as e:
        print(f"予期しないエラーが発生しました: {e}")
        continue

    visitor_id = str(visitor["data"]["visitorId"])
    appointRecordId = str(visitor["data"]["appointRecordId"])
    privilegeGroupId = '34'

    print(json.dumps(
        #{"visitorGivenName":visitorGivenName,"visitorId": visitor_id,"appointRecordId": appointRecordId, "registerment": visitor },
        {"visitorGivenName": visitorGivenName, "registerment": visitor},
        ensure_ascii=False, indent=2))

    print("■■■START　N回RETRY■■■")
    retry_result = False
    for repeat in range(1, 5):
        print("■■■",repeat,"回RETRY■■■")
        try:
            # post("/artemis/api/visitor/v1/visitor/out", {"appointRecordId": appointRecordId})
            group = post("/artemis/api/acs/v1/privilege/group/single/addPersons",
                         {"privilegeGroupId": privilegeGroupId, "type": 2, "list": [{"id": visitor_id}]})
            # print(json.dumps({"visitorId": visitor_id, "registerment": visitor, "addPersons": group, "appointRecordId": appointRecordId}, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"addPersons 予期しないエラーが発生しました: {e}")
            """
            # 将访客从指定门禁权限组中移除。
            # type=2 表示访客，id 是 visitorId。
            result = post("/artemis/api/acs/v1/privilege/group/single/deletePersons",
                          {"privilegeGroupId": privilegeGroupId, "type": 2, "list": [{"id": visitor_id}]})
            if result.get("code") == "0":
                # 海康平台中的权限组关联已解除
                print("海康平台中的权限组关联已解除！！！")
            else:
                print("海康平台中的权限组关联解除失败×××")
            """
            time.sleep(20)
            continue
        time.sleep(20)
        try:
            detail = post("/artemis/api/visitor/v1/person/ID/elementDownloadDetail", {"id": visitor_id})
            status = detail["data"]["ElementDetailList"]["ElementDetail"][0]["ElementStatus"][0]["elementStatus"]
            if str(status) == "0":
                print("●下发成功●")
                print(detail)
                retry_result = True
                break
            else:
                print("×下发失败×")
                # 删除预约记录
                post("/artemis/api/visitor/v1/visitor/out", {"appointRecordId": appointRecordId})
                # 将访客从指定门禁权限组中移除。
                # type=2 表示访客，id 是 visitorId。
                result = post("/artemis/api/acs/v1/privilege/group/single/deletePersons",
                             {"privilegeGroupId": privilegeGroupId, "type": 2, "list": [{"id": visitor_id}]})
                if result.get("code") == "0":
                    # 海康平台中的权限组关联已解除
                    print("海康平台中的权限组关联已解除！！！")
                else:
                    print("海康平台中的权限组关联解除失败×××")
                time.sleep(20)
                continue
        except Exception as e:
            print(f"予期しないエラーが発生しました: {e}")
            time.sleep(20)
            continue

    print("■■■END　N回RETRY■■■")
    if not retry_result:
        try:
            # 从海康人员资源中删除访客人员。
            # personId 使用访客登记接口返回的 visitorId。
            # 这是删除人员数据，不只是访客签退。
            post("/artemis/api/resource/v1/person/single/delete", {"personId": visitor_id})
            print(f"删除visitor: {visitorGivenName}")
        except Exception as e:
            print(f"删除visitor 予期しないエラーが発生しました: {e}")