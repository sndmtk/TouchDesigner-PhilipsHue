# HueBridge — minimal TouchDesigner component

## 使い方

1. `HueBridge.tox` をTouchDesignerへドラッグします。既存の `HueBridge_demo.toe` は旧版です。最新機能にはTOXを使用してください。
2. Bridgeページの `Bridge IP` はStrMenuです。候補を選ぶか、メニュー欄へIPv4アドレスを直接入力できます。`Discover Bridges` を押すとmDNSで検索します。APIキーがない場合は自動でペアリングを開始します。
3. `out_status` がリンクボタン待ちになったら、Bridge本体のボタンを押します。60秒でタイムアウトした場合は `Connect / Retry Pairing` で再試行します。
4. 認証成功後、Bridgeページの読み取り専用 `API Key` と `Authenticated Bridge IP` に情報が入ります。通常どおり **TOEを保存（Ctrl+S）** すると、内部コンポーネントのCustom Parametersとして保持されます。専用TOXの自動書き出しは行いません。
5. 内部の `mapping` DATで `pixel`（0始まり）と `light_id` の対応を確認します。初回取得時に自動作成します。照明名は `lights` DATで確認できます。
6. 照明数に対応した **N×1 RGBA TOP** を入力へ接続します。Lightsページの `Enable Light Output` をオンにすると制御を開始します。
7. `Lights On` が明示的なON/OFFです。RGBが黒、Aが0でも自動消灯しません。

## 入出力

## ライトの追加・削除

認証後、Lightsページの `Search New Lights` を押すとBridgeが未登録のZigbee機器を検索します。対象ライトの電源を入れてください。API v2の `zigbee_device_discovery` を使用し、検索状態は `out_status` の `light_search` に出ます。2秒ごとに状態を取得し、最大120秒で監視を終了します（Bridge側の検索を強制停止するものではありません）。

新しいライトは `lights` と `Selected Light` メニューに反映し、`mapping` の最大ピクセル番号の次へ自動追加します。入力TOPの幅は必要に応じて増やしてください。

`Selected Light` から選択し `Delete Selected Device` を押すと、対象名と機器IDの確認画面が開きます。`Delete Device` を選ぶとBridgeから機器を削除します。同じ機器に属する複数のライトはまとめて削除されます。再利用には再登録が必要です。成功時は該当するmapping行だけを削除し、残ったピクセル番号は保持します。通信エラー時は自動再送せず、一覧を再取得します。

追加・削除の通信も非同期です。処理中の連打はキューに蓄積しません。Bridge変更や再接続で未送信の操作を破棄します。結果は `out_status` の `light_delete`、エラーは `out_errors` で確認できます。設定後はTOEを保存してください。

## 入力と出力の詳細

- 入力: N×1 TOP、左からpixel 0, 1, 2…。RGBとAは0〜1に制限します。
- Input Color Space: sRGB（初期値）またはLinear sRGB。
- RGBをXYZに変換してxy色度を送信します。明るさはAlphaをそのまま0〜100へ変換して送信します。
- 黒はxyを送らず、前の色を維持して明るさ0を要求します。Hueの最小調光値により完全な暗転にはなりません。
- `out_status`: TOXの現在メッセージ（`state`）。
- `out_lights`: 照明一覧・取得状態（5秒ごと）。明るさ、xy、色域種別、Zigbee通信状態を出します。実測光量ではなくBridgeが報告した状態です。
- `errors`: コンポーネント内部のFIFO DAT。最新10件だけを保持するエラーログで、キーは出力しません。
- `bridges`: コンポーネント内部DAT。検出したBridgeのID、IP、検出方法を保持します。
- `mapping`: コンポーネント内部の編集用DATです。更新後はTOEを保存してください。

## 通信

標準ライブラリのワーカースレッドでHTTPSを実行し、TouchDesignerのメインスレッドでは待機しません。ワーカーはTDオペレータに触れません。タイムアウトは5秒、同時通信数は1です。

照明ごとの更新間隔と遷移時間は初期値1秒。同じ入力値でも、各ライトへUpdate Intervalごとに再送してBridge状態を再同期します。各ライトは独立して非同期送信されるため、10台なら同じ周期に最大10件のリクエストを開始します。同じライトに未完了リクエストがある間だけ、そのライトの次の送信を待機します。古い入力をキューに蓄積せず、送信直前に取得可能な最新値を使います。TOP読み戻しは `numpyArray(delayed=True)` です。

Bridgeが特定ライトの `communication_error` を返した場合、そのライトだけを5秒間送信対象から外します。他のライトの出力は継続し、待機後に通信不良ライトへ再試行します。

`out_lights` のZigbee通信状態は、`zigbee_connectivity` を照明の所有デバイスIDで結合して表示します。通信状態は15秒ごとの非同期ポーリングです。

認証キーは読み取り専用Custom Parameterに保存されます。TOEを保存して再度開くと復元されます。キーを更新・削除した後はTOEを保存してください。ReadOnlyはUIからの編集を防ぐ設定です。認証済みTOE、または手動で書き出したTOXにはキーが含まれます。接続先IPが変わった場合は再ペアリングが必要です。

## 最小版の範囲

Bridge IPのStrMenuによる手動入力・候補選択、Discover Bridgesによる非同期mDNS検索、認証、個別照明、RGBA TOP、ON/OFF、DAT出力に対応します。検索は既定ネットワークのIPv4 mDNSを3秒間実行します。既存IPは検索で上書きしません。空欄で1台のみ見つかった場合は自動入力します。部屋・ゾーン、シーン、専用色温度モード、Engine COMPでの検証は今後の対象です。

## 開発

`src/` はTOXに埋め込むソースです。実行時にこれらの外部ファイルは不要です。`src/build_component.py` はTouchDesigner内で実行するビルダーです。

テスト: `python -m unittest discover -s tests`
