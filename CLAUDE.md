# Claude Code instructions

作業開始時に `AI_CONSTITUTION.md` と指定されたGitHub Issueを読み、それを唯一の仕様として扱う。

## Claude Codeの担当

- 参謀本体の実装、UI、リファクタリングを独立ブランチで行う。
- iPhone Safari、予算、残額、許容損失、情報鮮度を実装へ反映する。
- 変更後にPythonテスト、JavaScriptテスト、evolution evalを実行する。
- Pull Request本文へ変更点、検証、残課題、想定失敗条件を書く。

## 変更禁止

機能実装のPull Requestでは、次を変更しない。

- `AI_CONSTITUTION.md`
- `evals/`
- `.github/CODEOWNERS`
- `.github/workflows/evolution-evals.yml`
- 合格条件を定めるテスト

評価側に誤りを見つけた場合は、実装PRで直さずIssueへ報告する。

## Git運用

- `メイン`へ直接push・直接マージしない。
- `claude/<目的>`ブランチからドラフトPull Requestを作る。
- APIキーや秘密情報をコミットしない。
- テスト失敗を隠す変更、空の例外処理、無条件成功を追加しない。

