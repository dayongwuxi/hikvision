# Hikvision訪問者一括処理 Java版

このプロジェクトは、親ディレクトリにある `api_register_and_add_group.py` の処理フローを完全に再現しています。関連ファイルはコピーも名前変更もせず、次のファイルを直接使用します。

- `../config/config.json`
- `../registration_payload.example.json`
- `../successful_visitor_records.jsonl`

現在のバッチパラメーターもPythonファイルの設定値と同じです。`4000`～`4009` を処理し、権限グループは `34`、ドアは `56` です。

## ビルドとテスト

`JAVA` ディレクトリで次のコマンドを実行します。

```powershell
mvn test
mvn package
```

## 実行

プロジェクトのルートディレクトリまたは `JAVA` ディレクトリから実行できます。

```powershell
java -jar JAVA\target\hikvision-visitor-provisioning.jar
```

現在のディレクトリが `JAVA` の場合は、次のコマンドを実行します。

```powershell
java -jar target\hikvision-visitor-provisioning.jar
```

プログラムは上位ディレクトリから `api_register_and_add_group.py` を検索し、プロジェクトのルートディレクトリを特定します。Python版と同様に、現在のHTTPクライアントはHikCentralのプライベート証明書に対応するため、証明書チェーンを検証しません。信頼できる内部ネットワークでのみ実行してください。
