# 每週排程設定

目前排程範本會在每週日與每週三 09:00 執行 `weekly_businessweekly_update.sh`。

## Discord webhook

`weekly_businessweekly_update.sh` 會從本機 `.env` 讀取 `DISCORD_WEBHOOK_URL` 並推播到 Discord。

```sh
export DISCORD_WEBHOOK_URL="你的 Discord webhook URL"
```

如果保持空白，程式仍會爬取、更新 JSON、產生差異檔，只是不會送 Discord 訊息。

## GitHub Actions

`.github/workflows/businessweekly-concerts.yml` 會在每週日與每週三 09:00 台灣時間執行。

設定方式：

1. 把專案推到 GitHub repository。
2. 到 repository 的 Settings -> Secrets and variables -> Actions。
3. 新增 Repository secret，名稱設為 `DISCORD_WEBHOOK_URL`，值貼上 Discord webhook。
4. 到 Actions 頁面手動執行 `Business Weekly Concert Notifications` 測試一次。

GitHub Actions 不需要這台 Mac 開機；時間到會由 GitHub 的雲端 runner 執行。

## 安裝排程

```sh
chmod +x weekly_businessweekly_update.sh
cp com.harrison.businessweekly-concerts.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.harrison.businessweekly-concerts.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.harrison.businessweekly-concerts.plist
launchctl enable gui/$(id -u)/com.harrison.businessweekly-concerts
```

`launchd` 只會在這台 Mac 開機、登入使用者工作階段可用時執行；如果電腦關機，排程不會被觸發。

## 立即手動跑一次

```sh
./weekly_businessweekly_update.sh
```

## 解除排程

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.harrison.businessweekly-concerts.plist
rm ~/Library/LaunchAgents/com.harrison.businessweekly-concerts.plist
```

## 輸出檔案

- `businessweekly_concerts.json`: 最新完整爬取結果
- `businessweekly_concerts_changes.json`: 和上次結果相比的新增、變更、移除
- `businessweekly_concerts.log`: 爬蟲與通知 log
- `businessweekly_concerts_launchd.out.log`: 排程 stdout
- `businessweekly_concerts_launchd.err.log`: 排程 stderr
