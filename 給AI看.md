# TabbyBot：給 AI 助手的交接說明

這是遊戲 Tabby（維洛）的 Python 掛機程式。原本在 macOS 上開發，這份是要在 Windows 上跑的版本。

## 在 Windows 上跑
1. 先裝 Python 3.9 以上（python.org，安裝時要勾選 Add to PATH）。
2. 雙擊「啟動 Tabby (Windows).bat」。第一次開會自動建 .venv、裝套件，並從 config.example.yaml 複製出 config.yaml。
3. 瀏覽器打開 http://127.0.0.1:8765 就是控制介面，登入自己的帳號或錢包。
手動啟動：`.venv\Scripts\python -m tabbybot gui`（加 `--no-browser` 就不自動開瀏覽器）。

## 結構
- tabbybot/hunt.py：打怪主邏輯（選目標、怪堆風箏 cluster_kite、巡邏、卡住恢復）
- tabbybot/watchdog.py：卡住偵測與重登；macOS 的通知在 Windows 會自動略過
- tabbybot/keychain.py：存密碼；macOS 用系統鑰匙圈，其他系統用備用的存法
- tabbybot/constants.py：各種數值（距離、秒數、速度上限）
- tabbybot/protocol.py：跟伺服器的通訊；data/maps：地圖導航資料；docs/protocol.md：協議說明
- 使用者資料放在 %USERPROFILE%\.tabbybot\（settings.json、debug.log、wallet.vault）

## HTTP API（127.0.0.1:8765）
- GET /api/state：看狀態，hunt.status_line 是一行摘要
- POST /api/hunt/start，JSON 範例 {"map_id":9,"auto_combat":true,"auto_return":true}

## 注意
- 弓手建議在設定裡開 cluster_kite（怪堆風箏）。
- 改程式前先備份；檢查語法用 `python -m py_compile 檔名`；跑測試用 `python -m pytest tests`。
- 錢包或助記詞不要貼給任何人或任何 AI。

## 助記詞裡有好幾個錢包
首次設定貼上助記詞後，下方會列出前 10 個錢包地址，選你要用的那個就好（跟 MetaMask 的 Account 1、2… 同順序）。也可以直接貼那個錢包的私鑰。
