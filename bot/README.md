# HL Bot — Hyperliquid 自動売買スターター

シグナルラボの「🤖 bot用設定を書き出す」で作った `hl_bot_config.json` を、そのまま24時間動かすための最小構成です。

## 仕組み

ツールで検証したロジック（条件・フィルタ・時間帯・待機・ATR損切り/利確・レバ）を同一実装で判定し、確定足ごとに評価 → シグナルで成行エントリー → **同時に取引所側のSL/TPトリガー注文を設置**（botが落ちても損切りが生きる）。サイズは「1トレードのリスク＝口座の0.5%」を損切り幅から逆算。日次損失3%で当日停止するキルスイッチ付き。SL=0の設定ではエントリーしません。

## セットアップ（VPS or 自宅PC / Python 3.10+）

```bash
cd bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 編集して各値を入れる
# シグナルラボから書き出した hl_bot_config.json をこのフォルダに置く
python3 hl_bot.py       # まずは MODE=paper のまま起動
```

### APIウォレット（エージェント）の作り方
Hyperliquidアプリ → **More → API** → エージェント名を付けて生成 → 表示された**秘密鍵**を `.env` の `HL_AGENT_PRIVATE_KEY` へ。このキーは**発注のみ可能で出金は不可能**な設計です。`HL_ACCOUNT_ADDRESS` には本体ウォレットの公開アドレスを入れます。**本体ウォレットの秘密鍵は絶対にどこにも書かない。**

## 稼働の4段階（順番を飛ばさない）

1. **paper（2週間目安）** — 発注せずDiscordに「仮エントリー」を報告。ツールのバックテストと発火が一致するか確認
2. **testnet**（`.env`で`TESTNET=1`）— 実際の発注フローとTP/SL設置を確認
3. **本番・最小サイズ** — `riskPctPerTrade`を0.1〜0.25%に下げて数週間。検証成績とのズレ（スリッページ・手数料）を測る
4. **段階増額** — 実成績が検証と整合してから

## 常駐化（systemd 例）

```ini
# /etc/systemd/system/hlbot.service
[Unit]
Description=HL Bot
After=network-online.target
[Service]
WorkingDirectory=/home/USER/hl-signal-lab/bot
ExecStart=/home/USER/hl-signal-lab/bot/venv/bin/python3 hl_bot.py
Restart=always
RestartSec=15
[Install]
WantedBy=multi-user.target
```
`sudo systemctl enable --now hlbot` で起動。ログは `hl_bot.log` とDiscordに出ます。

## 注意（重要）

- `.env` と `hl_bot_config.json` は `.gitignore` 済み。**公開リポジトリに絶対コミットしない**
- 発注系のSDK書式は `hyperliquid-python-sdk` の `examples/basic_tpsl.py` に準拠していますが、SDK更新で変わる可能性があるため、**live前に必ずtestnetで発注→TP/SL設置→約定までを目視確認**してください
- 本botは1銘柄1ポジションの最小構成です。複数戦略は「1戦略=1プロセス（config別）」で並べるのが安全です
- 自動売買は損失を保証なく発生させ得ます。必ず失っても許容できる資金で。
