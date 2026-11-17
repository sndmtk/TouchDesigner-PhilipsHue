# HueEntertainment（Entertainment API版）

TouchDesignerのTOP画像から、Philips Hueのライトを**Entertainment API**でリアルタイムに制御するコンポーネントです。全ライト分の色を1つのパケットにまとめて毎秒最大60回送るため、HTTPS版（`HueBridge.tox`）と違って**ライト同士のずれがなく、更新も高速**です。

`HueEntertainment.tox`をネットワークエディタにドラッグ＆ドロップして使います。追加のPythonパッケージは不要です（DTLS通信は標準ライブラリだけで実装しています）。

## HTTPS版との違い

| | HueBridge（HTTPS） | HueEntertainment |
| --- | --- | --- |
| 送り方 | 1台ずつ順番に送信 | 全台を1パケットで同時に送信 |
| フェード | Transition（Bridge側で補間） | Smoothing（コンポーネント内で補間）。初期値は0（なし） |
| 更新頻度 | Bridge全体で毎秒約10件を台数で分け合う | 全台が毎秒最大60回（ライトへの反映は約25回/秒） |
| ライト間のずれ | 台数÷送信レート秒 | なし |
| 対象 | 登録済みの全ライト | Hueアプリで作ったEntertainmentエリアのライト（1エリア最大10台程度） |
| 制限 | 部屋・ゾーン非対応 | ストリーミング中、エリア内のライトは他のアプリやHTTPS版から操作できません |

## 必要なもの

- Hue API v2（Entertainment）に対応したHue Bridge
- **Entertainmentエリア**。このコンポーネントの**Create Area**で作成できます（Hueアプリの「設定 → エンタテイメントエリア」で作ったものも使えます）。カラー対応ライトのみ、1エリア最大10台
- PCとBridgeの間で**UDPポート2100**が通ること

## クイックスタート

1. **Bridge**ページで**Discover Bridges**を押すか、**Bridge IP**を入力します。
2. 自動でペアリングが始まります。`out_status`に`Press the physical Bridge link button`と出たら、60秒以内にBridgeのリンクボタンを押してください。
   - Entertainment APIには通常のAPIキーに加えて**Client Key（暗号化の鍵）**が必要です。HueBridge.toxのAPIキーは使えないため、このコンポーネントで改めてペアリングします。
3. 認証後、Entertainmentエリアの一覧を取得します。エリアが1つだけなら自動で選択されます。複数ある場合は**Stream**ページの**Entertainment Area**で選びます。
   - **エリアがない場合は、ここで作成できます。** 内部の`area_lights` DATに、エリアに追加できるライトが一覧で表示されます。使うライトの`use`を`1`、使わないライトを`0`にし、`order`列の数字で左から右への並び順を決めてから、**Create Area from area_lights**を押してください。ライトは並び順どおりに左端（x=-1）から右端（x=1）へ等間隔に配置され、作成したエリアが自動で選択されます。
4. **幅N×高さ1のRGBA TOP**を入力に接続します。`mapping` DATで、ピクセル番号とライトの対応を確認・編集できます。ピクセル番号は**常に0〜（ライトの数−1）の重複しない連番**です（詳しくは「mappingの採番」）。
5. **Enable Streaming**をオンにします。`out_status`が`Streaming N channel(s) at 50 fps`になれば送信中です。
6. **Ctrl+SでTOEを保存**し、キーを保持します。

ストリーミングをオフにすると、エリアを解放してライトが通常の操作に戻ります。TouchDesignerを終了したときも自動で解放します。

## パラメータ

### Bridgeページ

| パラメータ | 説明 |
| --- | --- |
| Bridge IP | 接続先のIPv4アドレス |
| Discover Bridges | ローカルネットワーク上のBridgeを検索（約3秒） |
| Connect / Retry Pairing | 接続・ペアリングをやり直す |
| Forget Key / Pair Again | 保存したキーを消去して再ペアリング |
| API Key / Client Key (PSK) / Authenticated Bridge IP | ペアリングで取得した値（読み取り専用） |

### Streamページ

| パラメータ | 初期値 | 説明 |
| --- | --- | --- |
| Entertainment Area | — | ストリーミング先のエリア |
| Refresh Areas | — | エリア一覧を再取得し、`area_lights`も作り直します（アプリでエリアを編集した後に押す） |
| New Area Name | TouchDesigner | Create Areaで作るエリアの名前 |
| Create Area from area_lights | — | `area_lights`で`use`=1のライトを`order`順に左から右へ並べたエリアを新しく作成し、選択します |
| Update Selected Area from area_lights | — | 選択中のエリアのライトを`area_lights`の内容（`use`=1、`order`順）で置き換えます。エリアのIDは変わりません。ストリーミング中は停止してから更新し、Enable Streamingがオンなら自動で再開します |
| Delete Selected Area | — | 確認画面のあと、選択中のエリアをBridgeから削除します（ライト自体は削除されません）。ストリーミング中なら先に停止します |
| Enable Streaming | オフ | ストリーミングの開始・停止 |
| Send Rate (fps) | 50 | 1秒あたりの送信回数（10〜60）。Bridgeからライトへは約25回/秒で反映され、UDPの取りこぼし対策として多めに送ります |
| Smoothing (s) | 0 | 色・明るさの変化をなめらかにする時間（HTTPS版のTransitionに相当）。指定した秒数で変化の約95%に達します。補間はリニアな光の量で行うため、黒へのフェードでも色味を保ったまま暗くなります。0で無効（入力をそのまま送信）。TouchDesigner側ですでに補間している場合は0のままにしてください |
| Input Color Space | sRGB | 入力TOPの色空間（sRGB / Linear sRGB） |
| Brightness From | Alpha x RGB Value | 明るさの決め方。`Alpha x RGB Value`はRGBの最大値×Alphaで、暗いピクセルほど暗く光ります（映像向け）。`Alpha Only`はAlphaだけで明るさを決め、RGBは色だけに使います |

## 内部のDAT

| DAT | 内容 |
| --- | --- |
| out_status / status | 現在の状態メッセージ |
| out_channels / channels | 選択中のエリアのチャンネル（ID・ライト名・位置）。グラデーションライトは区画ごとに`#番号`付きで並びます |
| mapping | ピクセル→ライトの対応表（`pixel`、`channel_id`、`name`、`light_key`）。エリアの全ライトを必ず1回ずつ含み、`pixel`は常に0〜N−1の連番です |
| area_lights | エリアに追加できるライト（`use`、`order`、名前、entertainmentサービスID）。選択中のエリアのライトが`use`=1で先頭に、その他が`use`=0で続きます。編集内容は定期更新で消えず、Bridgeに新しく登録されたライトは`use`=0で末尾に自動追加されます。Refresh Areasで初期状態に戻ります |
| errors | 最新10件のエラー（キーは伏せ字） |
| bridges | 検出したBridge |

## mappingの採番

`mapping`はエリア内の**全ライトを1行ずつ**持ち、`pixel`は**常に0〜（ライトの数−1）の重複しない連番**になるよう、自動で採番し直されます。

- ライトは`light_key`（ライト固有のID）で識別します。Bridgeがエリアを編集してチャンネル番号を振り直しても、ピクセルは同じライトに対応したままです（`channel_id`列は自動で更新されます）。
- **ライトがエリアから外れたとき**：その行を除いて採番し直します。残ったライトの並び順は保たれます。
- **ライトがエリアに加わったとき**：末尾に追加して採番し直します。
- **並び順を変えたいとき**：`pixel`の数字を書き換えます。たとえば`1.5`と入力すると1番と2番の間に移動し、すぐに0、1、2…へ採番し直されます。同じ番号が重なった場合は、表の上にある行が先になります。
- **Create Area / Update Areaの後**は、`area_lights`で決めた左から右の順に作り直します。
- ライトを制御対象から外したいときは、`mapping`の行を消すのではなく（消しても自動で戻ります）、Update Areaでエリアから外してください。

## ライトを増やす・減らす

| やりたいこと | 手順 |
| --- | --- |
| **新しいライトを追加する** | ①まずBridgeにライトを登録します。HueBridge.toxの**Search New Lights**か、Hueアプリの「ライトを追加」を使います。②登録されたライトは、約10秒以内（またはRefresh Areasを押したとき）に`area_lights`の末尾へ`use`=0で現れます。③`use`を1にし、`order`で位置を決めて**Update Selected Area**を押します |
| **エリアからライトを外す** | `area_lights`でそのライトの`use`を0にし、**Update Selected Area**を押します。ライトはBridgeに登録されたまま残ります |
| **Bridgeからライトを削除する** | HueBridge.toxの**Delete Selected Device**か、Hueアプリで削除します。エリアからは自動で外れ、`mapping`も採番し直されます |
| **並び順を変える** | `area_lights`の`order`を変えてUpdate Selected Areaを押すか、`mapping`の`pixel`を書き換えます |

1エリアに入れられるのは10台までです。

## うまく動かないとき

| 症状 | 確認すること |
| --- | --- |
| `No Entertainment areas found` | `area_lights`を確認してCreate Areaを押すか、Hueアプリでエリアを作成してRefresh Areasを押す |
| `area_lights`が空 | カラー対応ライトがBridgeに登録されているか確認し、Refresh Areasを押す |
| `No light has use=1` | `area_lights`で使うライトの`use`を1にする |
| `Could not create the area` / `Could not update the area` | `errors` DATの内容を確認。1エリアに追加できるのは10台までです |
| `Could not start the area` | 他のアプリ（Hue Sync、別のTouchDesignerなど）が同じBridgeでストリーミング中でないか。Bridgeごとに同時に1エリアだけ使えます |
| `Stream lost: Handshake timed out` | PCのファイアウォールでUDP 2100が止められていないか。Bridge IPが正しいか |
| `Stream lost: No Finished from Bridge` | Client Keyが合っていない可能性があります。Forget Keyで再ペアリング |
| 色が変わらないライトがある | `mapping`にそのチャンネルがあるか、入力TOPの幅がピクセル番号より大きいか |
| HTTPS版と併用している | ストリーミング中、エリア内のライトにはHTTPS版の命令が効きません。HueBridgeのEnable Light Outputをオフにしてください |

## 保存と共有時の注意

API KeyとClient Keyはカスタムパラメータに保存され、**TOEを保存するとキーも保存されます**。認証済みのTOEや、手動で書き出したTOXを公開しないでください。リポジトリの`HueEntertainment.tox`はビルダーが未ペアリングの状態で書き出したものです。

## 開発

| ファイル | 役割 |
| --- | --- |
| src/dtls_psk.py | DTLS 1.2クライアント（PSK、TLS_PSK_WITH_AES_128_GCM_SHA256のみ）。AES-128・GCM・TLS 1.2 PRFを標準ライブラリだけで実装 |
| src/ent_core.py | HueStream v2パケット、RGB→xy変換、エリア情報の整理 |
| src/ent_runtime.py | ペアリング、エリア取得、ストリームの開始・停止、送信スレッド |
| src/build_entertainment.py | TouchDesigner内でコンポーネントを作成・更新し、未ペアリングのTOXを書き出す |
| tests/test_ent.py | パケット・色・状態遷移のテスト。`openssl`コマンドがあれば実際のDTLSハンドシェイクも検証 |

### TOXの作成・更新

TouchDesignerのTextport（Alt+T）で実行します。`src/build_entertainment.py`の`ROOT`は配置先に合わせてください。

```python
exec(open(r'C:/Users/sndmt/Documents/Github Projects/TouchDesigner-PhilipsHue/src/build_entertainment.py', encoding='utf-8').read())
```

`/project1/HueEntertainment`がなければ作成し、あればキー・エリア・mappingを保ったままコードとパラメータを更新します。あわせて、リポジトリ直下に未ペアリングの`HueEntertainment.tox`を書き出します。結果は`build/entertainment_build.json`、失敗時は`build/entertainment_build_error.txt`に出力されます。
