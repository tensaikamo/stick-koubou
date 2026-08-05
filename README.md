# Stick工房 — 個人参謀

iPhoneだけで使う、1人用の意思決定アプリです。毎朝AI業界の一次情報と変化を収集しますが、
目的はニュースを増やすことではありません。今日30分で終えられる一手を一つ決め、結果を
次回の提案へ戻すことが中心です。

## 不変条件

- トップに出す予測は `data/hunches.json` の採点対象と同一にする
- 新しい事実や明確なedgeが無い日は、予測を0件にする
- iPhone、30分、原則無料という制約に反する一手を公開しない
- 同一URLの事実と意味が近い予測を重複させない
- 行動結果は構造化イベントとして記憶し、会話全文を無差別に保存しない

## 処理の流れ

1. `common.py` が一次情報、リリース、求人、規制、実勢、二次情報を同じ形式で収集
2. `recorder.py` が新しい出来事だけを記録し、採点可能な予測を最大2件作成
3. `resolver.py` が期限到来後に公開証拠で答え合わせ
4. `sanbo.py` が同じ出来事・同じ予測を使ってブリーフィングと一手を生成
5. `decision.py` が端末制約、重複、予測一本化、行動結果の記憶を管理

## ローカル検証

```bash
python -m py_compile common.py memory.py recorder.py resolver.py sanbo.py brief.py panels.py preview.py decision.py
pytest -q tests/
```

本番更新は `.github/workflows/sanbo.yml` が毎日実行します。生成物は `docs/`、記録は
`data/` に保存されます。
