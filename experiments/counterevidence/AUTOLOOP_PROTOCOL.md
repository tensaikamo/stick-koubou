# Counterevidence AutoLoop v1 protocol

## 目的

生成、独立judge、採点、証跡Draft PRまでを、利用者が一件ずつアプリへ貼り付けずに
再開可能な一本のrunとして扱う。追加のAPI従量課金は使わない。最終mergeだけは利用者が行う。

このv1は安全契約を先に固定する公開control planeである。モデル実行や資格情報の保存は
行わず、現在進行中のcalibration run、事前登録、dataset/specs、採点閾値を変更しない。

## 二つのexecution plane

| plane | repository | 保持できるもの | 禁止 |
|---|---|---|---|
| `public_control` | 公開 `stick-koubou` | 状態、prompt/response SHA、receipt、テスト、Draft PR用証跡 | AI資格情報、モデル実行、課金API、自動merge |
| `private_trusted` | 別の非公開repository/runner | Claude subscription OAuth、ChatGPT管理のCodex認証、応答本文 | API key、PAYG、extra usage、auto top-up、公開ログへの秘密値出力 |

初回だけ、人間がprivate runnerへClaudeとChatGPTのsubscription認証を設定する。以後は
checkpointから自動再開できる。ただし利用上限、認証失効、課金経路の疑いがあれば停止する。

公式の認証仕様:

- Claude Code long-lived OAuth: <https://code.claude.com/docs/en/iam>
- Claude Code GitHub Actions: <https://code.claude.com/docs/en/github-actions>
- Codex CI/CD authentication: <https://learn.chatgpt.com/docs/auth/ci-cd-auth>
- Codex non-interactive mode: <https://learn.chatgpt.com/docs/non-interactive-mode>

CodexのChatGPT管理認証をCI/CDで使うのは、公式文書どおり信頼されたprivate repositoryに
限る。公開repositoryで認証ファイルやtokenを扱ってはならない。

## 状態機械

通常経路:

`CREATED → GENERATING → VALIDATING_GENERATION → JUDGING →`
`VALIDATING_JUDGMENTS → SCORING → ATTESTING → DRAFT_PR_READY`

停止状態:

- `PAUSED_QUOTA`: subscription利用上限。モデルやrouteを変えず同じrunを再開する
- `BLOCKED_AUTH`: subscription認証が無い、失効、または公開planeに置かれた
- `BLOCKED_BILLING_ROUTE`: API key、PAYG、extra usage等を検出
- `INVALID_OUTPUT`: 欠損・malformed。応答SHAを残し、都合のよい再生成をしない
- `INVALID_PROVENANCE`: SHA、C1/C2束縛、receipt、attestationの不一致
- `INVALID_INDEPENDENCE`: generatorとjudgeのprovider/sessionが分離されていない
- `FAILED_TESTS`: 回帰テスト不合格

停止状態からKEEP/DROP等の判定は出さない。利用上限では別モデル、API、別providerへ
fallbackせず、`PAUSED_QUOTA`で待つ。

## 追加課金のhard stop

次の経路が一つでも見つかれば実行前に停止する。

- `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`
- `OPENAI_API_KEY` / `CODEX_API_KEY`
- xAI、OpenRouter、Bedrock、Vertex、Foundry、Azure OpenAIの資格情報または選択flag
- extra usage、PAYG、auto top-up、auto reload
- Anthropic/OpenAIの標準host以外を指す`*_BASE_URL`

許可するのはprivate runner内のClaude subscription OAuthとChatGPT管理Codex認証だけ。
manifest/receiptには資格情報の値を保存せず、検出時のログも環境変数名だけを出す。

## 不変条件

1. stage別call plan、provider、model、route、sessionはrun開始後に変更しない
2. callはplan上限内で追加し、追加済みcallのID・prompt SHA・束縛は変更しない
3. 応答は一回だけ記録する。malformed、空欄、形式逸脱を再生成で隠さない
4. C2はC1 call IDとC1 response SHAへ束縛する
5. judge callは採点対象response SHAへ束縛する
6. checkpoint再開は完了済みcallを飛ばし、未完了callだけを続ける
7. plan数未満の欠損callを分母から除外せず、全件がcompleteになるまでscoreへ進まない
8. generatorとjudgeはproviderおよびsessionを分ける
9. response本文やreceiptの改変はSHA不一致で停止する
10. 既存`score.py`のperformance/safety/workload gateを弱めない
11. 成功時もDraft PRで止め、auto-mergeしない

## checkpointとreceipt

`autoloop.py`はmanifest全体のcanonical JSONから`checkpoint_sha256`を計算し、各更新を
`sequence`と`previous_checkpoint_sha256`で一つ前のcheckpointへ連結する。
receiptはprompt/response SHA、byte数、宣言provider/model/route/sessionだけを持ち、秘密値を
持たない。公開planeはreceiptと実体のSHAを照合してから次の状態へ進む。

SHA chainは電子署名ではないため、ローカルファイルだけなら全履歴を作り直せる。private
runnerは各checkpointをappend-onlyなGit履歴へ即時保存し、force-pushを禁止する。最終Draft
PRはchain終端とattestationを公開台帳へ固定する。この外部anchor前のローカル証跡を
「改ざん不能」とは呼ばない。

subscription UIのprovider/model名はrunnerが観測して申告する値であり、APIの
provider-reported identityではない。この限界は`unverifiable_manual`として証跡へ残し、
特定APIモデルの性能へ一般化しない。

CLIはネットワークやモデルを使わない。

```bash
python experiments/counterevidence/autoloop.py dry-run
python experiments/counterevidence/autoloop.py validate MANIFEST.json
python experiments/counterevidence/autoloop.py validate-resume BEFORE.json AFTER.json
python experiments/counterevidence/autoloop.py validate-receipt MANIFEST.json RECEIPT.json RESPONSE.txt
python experiments/counterevidence/autoloop.py validate-chain CHECKPOINT-000.json CHECKPOINT-001.json ...
```

## rollout

1. 本PR: 公開contract、schema、fail-closed controller、behavior test
2. 別private repository: subscription認証済みrunner。秘密値を公開repoへ渡さない
3. 未使用ケースでnon-formal smoke。課金経路0、停止/再開、独立性、SHAを監査
4. 現在のcalibration完了後に接続。既存runへ途中導入しない
5. formalは既存の事前登録と人間merge承認を維持する
