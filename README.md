# TouchDesigner × Philips Hue

TouchDesignerのTOP画像から、Philips Hueの照明を制御するコンポーネントです。横一列の画像の各ピクセルをライトに割り当て、**RGBで色、Alphaで明るさ**を指定します。

`HueBridge.tox`をプロジェクトに追加し、ローカルネットワーク上のHue Bridgeとペアリングして使用します。照明のON/OFF、Bridgeの検索、新しいライトの登録、機器の削除にも対応しています。

## 必要なもの

- TouchDesigner
- Hue API v2に対応したPhilips Hue Bridge
- Bridgeに登録した照明（色を変えるにはカラー対応ライトが必要）
- PCとBridgeが通信できるローカルネットワーク

通常の利用では、追加のPythonパッケージや`src/`の配置は不要です。必要なコードはTOXに埋め込まれています。TouchDesignerの最小対応バージョンとOS別の動作範囲は未確定です。

## クイックスタート

### 1. コンポーネントを追加する

このリポジトリの[HueBridge.tox](HueBridge.tox)をダウンロードし、TouchDesignerのネットワークエディタへドラッグ＆ドロップします。GitHubのファイル画面では **Download raw file** から取得できます。

`HueBridge_demo.toe`も同梱していますが、最新機能を使う場合は`HueBridge.tox`から始めてください。

### 2. Hue Bridgeと接続する

コンポーネントのカスタムパラメータで **Bridge** ページを開きます。

1. **Discover Bridges** を押してBridgeを検索します。
2. **Bridge IP** の候補を選択します。見つからない場合は、BridgeのIPv4アドレスを直接入力してください。
3. APIキーがない場合は、自動でペアリングが始まります。内部の`out_status`に `Press the physical Bridge link button` と表示されたら、Bridge本体のリンクボタンを押します。
4. 認証後、**API Key** と **Authenticated Bridge IP** が自動入力され、ライト一覧の取得が完了すると`state`が`Ready`になります。
5. **Ctrl+SでTOEを保存**し、接続情報を保持します。

ペアリングの待機時間は60秒です。タイムアウトした場合は **Connect / Retry Pairing** で再試行してください。

Bridge検索は約3秒間のIPv4 mDNS検索です。IP欄が空で1台だけ見つかった場合は自動入力されます。入力済みのIPは検索で上書きしません。

### 3. 入力TOPを用意する

まず1灯で試す場合は、**Constant TOP**を作り、解像度を**幅1 × 高さ1**に設定します。RGBを好きな色、Alphaを`1`にして、HueBridgeのTOP入力へ接続してください。

複数のライトを個別に制御する場合は、**幅N × 高さ1のRGBA TOP**を用意します。左端がピクセル`0`、その右が`1`、`2`…です。

```text
RGBA TOP（N × 1） → HueBridge → Hue Bridge → 照明
                     ↑
              mappingで割り当て
```

### 4. ピクセルとライトを対応付ける

HueBridgeの内部へ入り、`lights` DATでライトの名前とIDを確認し、`mapping` DATを編集します。初回取得時には対応表が自動作成されます。

| pixel | light_id | name |
| --- | --- | --- |
| 0 | lightsからコピーした1灯目のid | 左のライト |
| 1 | lightsからコピーした2灯目のid | 右のライト |

- `pixel`：入力画像のピクセル番号。**0から開始**します。
- `light_id`：`lights` DATの`id`列にある識別子です。
- `name`：人が対応を確認するための名前です。制御対象は`light_id`で決まります。

制御するライトの行を残し、使わない行は削除してください。ヘッダー行は残します。入力TOPの幅は、使用する最大の`pixel`値に1を足した値以上にしてください。

### 5. 出力を開始する

**Lights** ページで **Lights On** をオンにし、**Enable Light Output** をオンにします。入力TOPの色とAlphaが、割り当てたライトへ送られます。

消灯するときは、**Enable Light Outputをオンにしたまま、Lights Onをオフ**にします。Enable Light Outputをオフにすると送信が止まり、ライトはその時点の状態を維持します。

設定ができたら、TOEを保存してください。

## 色・明るさの指定

| 入力・設定 | 動作 |
| --- | --- |
| RGB | xy色度に変換して色を指定。ライトの色域に合わせて補正します |
| Alpha | `0〜1`を明るさ`0〜100%`に変換します |
| Input Color Space | 入力の色空間をsRGBまたはLinear sRGBから選択します |
| Lights On | mappingにあるライトのON/OFFを指定します |

RGBAの値は`0〜1`に制限されます。RGBの強さを下げるだけでは、明るさを同じ割合で下げる指定にはなりません。調光にはAlphaを使ってください。

**RGBが黒の場合は色を更新せず、直前の色を維持します。明るさは黒でもAlphaで決まります。** Alphaが`0`でも、送信するのは明るさ0の要求であり、OFFではありません。ライトの最小調光値によって完全には暗くならないため、消灯にはLights Onを使います。

色や調光に対応していないライトでは、その機能の送信を省略します。

## パラメータ一覧

### Bridgeページ

| パラメータ | 説明 |
| --- | --- |
| Bridge IP | 接続先のIPv4アドレス。候補選択と直接入力に対応 |
| Discover Bridges | ローカルネットワーク上のBridgeを検索 |
| Connect / Retry Pairing | 接続・ペアリングを再試行 |
| Forget Key / Pair Again | 保存したAPIキーを消去し、再ペアリング |
| API Key | 認証時に取得するキー。読み取り専用 |
| Authenticated Bridge IP | 保存したキーに対応するIP。読み取り専用 |

### Lightsページ

| パラメータ | 初期値 | 説明 |
| --- | --- | --- |
| Search New Lights | — | Bridgeから未登録のZigbee機器を検索 |
| Selected Light | Select a light | 削除対象のライトを選択 |
| Delete Selected Device | — | 確認画面を開き、選択したライトの機器を削除 |
| Enable Light Output | オフ | ライトへの制御値の送信を有効化 |
| Lights On | オン | mapping内のライトを点灯／消灯 |
| Update Interval (s) | 1 | 各ライトの送信間隔。最小0.1秒 |
| Transition (s) | 1 | 色・明るさなどの遷移時間。最小0秒 |
| Input Color Space | sRGB | sRGB / Linear sRGB |

## ライトの追加・削除

### 新しいライトを追加する

認証後、対象ライトの電源を入れて **Search New Lights** を押します。検索状態は`out_status`の`state`に表示されるメッセージで確認できます。

検出されたライトは`lights`とSelected Lightの候補に反映され、`mapping`の最大ピクセル番号の次に自動追加されます。必要に応じて入力TOPの幅を広げ、TOEを保存してください。

検索状態は2秒ごとに取得し、最大120秒で監視を終了します。監視終了はBridge側の検索を強制停止する操作ではありません。

### 登録済みの機器を削除する

**Selected Light**で対象を選び、**Delete Selected Device**を押します。確認画面の名前と機器IDを確認し、削除する場合だけ **Delete Device** を選択してください。

**これはmappingから外すだけではなく、Bridgeから機器を削除する操作です。** 同じ機器に属する複数のライトもまとめて削除され、再利用には再登録が必要です。制御対象から外したいだけの場合は、`mapping`の該当行を削除してください。

削除成功時は該当するmapping行を取り除き、残ったピクセル番号は維持します。結果は`out_status`の`state`、エラーは内部の`errors` DATで確認できます。通信エラー時は自動再送せず、一覧を再取得します。

## 状態の確認

コンポーネントはTOP入力を1つ、DAT出力を2つ持ちます。

| DAT | 場所 | 内容 |
| --- | --- | --- |
| out_status | 出力・内部 | 接続・検索・削除などの現在のメッセージを`state`に表示 |
| out_lights | 出力・内部 | ライトのID、名前、ON/OFF、明るさ、xy、色対応、色域、Zigbee通信状態 |
| lights | 内部 | out_lightsの元となるライト一覧 |
| mapping | 内部・編集用 | ピクセルとライトの対応表 |
| errors | 内部 | 最新10件を保持するFIFOエラーログ。APIキーは出力しません |
| bridges | 内部 | 検出したBridgeのID、IP、検出方法 |

ライトの状態は約5秒ごと、Zigbee通信状態は約15秒ごとに取得します。表示値はBridgeが報告する状態であり、実測した光量ではありません。

## うまく動かないとき

| 症状 | 確認すること |
| --- | --- |
| Bridgeが見つからない | PCとBridgeのネットワーク接続を確認し、Bridge IPへIPv4アドレスを直接入力 |
| ペアリングがタイムアウトする | Connect / Retry Pairingを押し、60秒以内にBridge本体のリンクボタンを押す |
| APIキーが拒否される | Forget Key / Pair Againで再ペアリングし、TOEを保存 |
| 接続できるがライトが変わらない | Enable Light Output、Lights On、TOPの接続、mappingのlight_idを確認 |
| TOPのサイズに関するエラーが出る | 高さが1で、幅がmappingの最大pixel値＋1以上になっているか確認 |
| Alphaを0にしても消灯しない | Lights Onをオフにして明示的に消灯 |
| 一部のライトだけ反応しない | out_lightsのzigbee_statusと内部のerrorsを確認し、ライトの電源・通信状況を確認 |
| 再度開くと接続情報が戻らない | ペアリング後にTOEを保存したか確認。接続先IPが変わった場合は再ペアリング |

## 通信と制約

- Hue API v2の個別ライト制御を使用します。Entertainment APIによるストリーミングには対応していません。
- HTTPS通信はワーカースレッドで実行します。HTTPのタイムアウトは5秒です。
- 認証・一覧取得・機器管理は1ワーカー、ライト制御は最大16ワーカーで処理します。同じライトへの制御リクエストは重ねて送信しません。
- 入力が変わらない場合もUpdate Intervalごとに再送します。TOPは`numpyArray(delayed=True)`で読み取り、過去の入力を蓄積せず、送信時に取得可能な値を使います。
- Bridgeがライトの`communication_error`を返した場合は、そのライトへの送信を5秒間待って再試行します。他のライトへの出力は継続します。
- ライトの追加・削除も非同期で処理します。処理中の連打は蓄積せず、Bridgeの変更や再接続時には未送信の管理操作を破棄します。
- 部屋・ゾーン、シーン、専用の色温度モードには対応していません。Engine COMPでの動作は未検証です。
- 現在の実装ではBridgeへのHTTPS証明書検証を無効にしています。信頼できるローカルネットワークで使用してください。

## 保存と共有時の注意

APIキーはコンポーネントのカスタムパラメータに保存され、**TOEを保存すると認証情報も保存されます**。読み取り専用はUIからの編集を防ぐ設定であり、キーの暗号化ではありません。

**認証済みのTOEや、認証後に手動で書き出したTOXにはAPIキーが含まれます。GitHubなどへ公開するファイルには含めないでください。** 共有用には未ペアリングのコンポーネントを使用し、API KeyとAuthenticated Bridge IPが空であることを確認してください。

キーの更新・削除やmappingの編集後はTOEを保存してください。認証済みTOXの自動書き出しは行いません。

## 開発

| ファイル | 役割 |
| --- | --- |
| HueBridge.tox | 利用者向けコンポーネント |
| HueBridge_demo.toe | 同梱デモ。最新機能はTOXを使用 |
| src/build_component.py | TouchDesigner内でコンポーネントを構築・書き出し |
| src/update_component.py | 開いているプロジェクトのHueBridgeを更新 |
| src/runtime.py | 認証、通信、ライト制御、状態管理 |
| src/hue_core.py | 色変換とHTTP処理 |
| src/discovery.py | BridgeのmDNS検索 |
| src/controls.py | カスタムパラメータの操作コールバック |
| tests/ | 色変換・認証・検索・機器管理のユニットテスト |

### ソースからTOXを作成する

ビルダーはTouchDesigner内のPython環境で実行します。通常のPythonからは実行できません。

1. リポジトリをローカルに配置します。
2. `src/build_component.py`の`ROOT`を配置先の絶対パスに変更します。
3. リポジトリ直下に`build`フォルダを作成します。
4. TouchDesignerで`/project1`があるプロジェクトを開きます。
5. Text DATへ`src/build_component.py`を読み込み、実行します。

リポジトリ直下の`HueBridge.tox`へ書き出されます。ビルド結果は`build/verification.txt`、失敗時の詳細は`build/build_error.txt`を確認してください。

既存の`/project1/HueBridge`を更新する場合は、`src/update_component.py`の`ROOT`も設定してTouchDesigner内で実行します。更新前にプロジェクトを保存してください。このスクリプトは使用中のコンポーネントを更新し、配布用に別の未ペアリングTOXを書き出します。

### テスト

リポジトリ直下で実行します。

```sh
python -m unittest discover -s tests
```

ユニットテストに加えて、TOXを変更した場合はTouchDesignerと実機で接続・入力・点灯動作を確認してください。
