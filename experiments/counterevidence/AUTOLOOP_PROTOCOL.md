# Counterevidence AutoLoop v1 protocol

## 目的

生成、独立judge、採点、証跡Draft PRまでを、利用者が一件ずつアプリへ貼り付けずに
再開可能な一本のrunとして扱う。追加のAPI従量課金は使わない。最終mergeだけは利用者が行う。

ここでいう「使わない」は実行契約であり、公開controllerだけで請求書やprovider側の課金記録を
検証できるという意味ではない。private runnerが申告するrouteと環境をfail-closedで検査し、
non-formal smokeの独立監査を通過してから接続する。

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
- `ANTHROPIC_CUSTOM_HEADERS`等のheader注入、`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`
- AWS credentials、Anthropic Bedrock/Vertex/Foundry設定、Google Cloud/Azure資格情報

許可するのはprivate runner内のClaude subscription OAuthとChatGPT管理Codex認証だけ。
manifest/receiptには資格情報の値を保存せず、検出時のログも環境変数名だけを出す。

## 不変条件

1. stage別call plan、plan source SHA、provider、model、route、sessionはrun開始後に変更しない
2. callはplan上限内で追加し、追加済みcallのID・prompt SHA・束縛は変更しない
3. 応答は一回だけ記録する。malformed、空欄、形式逸脱を再生成で隠さない
4. C2はC1 call IDとC1 response SHAへ束縛する
5. judge callは採点対象generator call IDと実在するcompleted response SHAへ束縛する
6. FINAL_VERDICT/UNSUPPORTEDはA/B/C2の各応答をそれぞれ一度ずつ覆い、重複・欠落を許さない。
   generator stageはA/B/C1/C2、judge stageはFINAL_VERDICT/UNSUPPORTEDだけを許可し、
   未知stageを作って被覆分母から逃れることを禁じる
7. checkpoint再開は完了済みcallを飛ばし、未完了callだけを続ける
8. plan数未満の欠損callを分母から除外せず、全件がcompleteになるまでscoreへ進まない
9. generatorとjudgeはproviderおよびsessionを分ける
10. response本文やreceiptの改変はSHA不一致で停止する
11. 既存`score.py`のperformance/safety/workload gateを弱めない
12. 成功時もDraft PRで止め、auto-mergeしない

## checkpointとreceipt

`autoloop.py`はmanifest全体のcanonical JSONから`checkpoint_sha256`を計算し、各更新を
`sequence`と`previous_checkpoint_sha256`で一つ前のcheckpointへ連結する。
receiptはprompt/response SHA、byte数、宣言provider/model/route/sessionだけを持ち、秘密値を
持たない。receiptは対象checkpoint SHAにも束縛する。公開planeはreceiptと実体のSHAを
照合してから次の状態へ進む。`collected_at_utc`はrunner時計の自己申告であり、
`time_verification: unverifiable_runner_clock`として保存し、順序やgateの根拠には使わない。

SHA chainは電子署名ではないため、ローカルファイルだけなら全履歴を作り直せる。private
runnerは各checkpointをappend-onlyなGit履歴へ即時保存し、force-pushを禁止する。最終Draft
PRはchain終端とattestationを公開台帳へ固定する。この外部anchor前のローカル証跡を
「改ざん不能」とは呼ばない。

subscription UIのprovider/model名はrunnerが観測して申告する値であり、APIの
provider-reported identityではない。この限界はAutoLoop manifest/receiptでは
`unverifiable_subscription`として証跡へ残し、特定APIモデルの性能へ一般化しない
（既存`score.py`側の名称は`unverifiable_manual`）。

同様に、`route`、repository visibility、資格情報の種類もrunner環境からの申告・検査結果で
あり、providerの請求台帳による証明ではない。manifest/receiptでは
`verification: unverifiable_subscription`を必須にする。公開controllerが保証するのは
「既知の課金経路を拒否し、許可route以外を宣言できないこと」であって、外部課金が絶対に
発生しないという暗号学的証明ではない。

### genesis planの外部固定

SHA chainはrun中のplan変更を拒否できるが、最初から小さい分母を宣言したgenesisの妥当性は
chain単体では判断できない。そこで`plan_source_sha256`を必須にし、回答生成前にGit追跡済みの
cohort/preregistration artifact実体と`validate-plan-source`で照合する。この外部artifactが
固定されていないrunは開始してはならない。

照合の実施漏れを運用任せにしないため、`plan_source_verified`をDraft PRの必須gateにする。
`validate-plan-source`が成功したrunだけがこのgateをtrueにでき、未確認のまま
`finalize_for_draft`を呼ぶと`INVALID_PROVENANCE`で停止する。ただしcontrollerが検証できるのは
「与えられた実体のSHAがmanifestと一致すること」までであり、その実体が正しい事前登録
artifactであることはGit履歴とreviewerが担保する。

JSON Schemaは外部consumer向けの構造契約、Python validatorはcross-call/SHA/stateを含む
正本である。CIはDraft 2020-12 validatorでschema自体と代表fixtureを実行検証する。
Schemaだけでは複数call間の1対1対応を表せないため、外部consumerもPython validatorを
省略してはならない。

CLIはネットワークやモデルを使わない。

```bash
python experiments/counterevidence/autoloop.py dry-run
python experiments/counterevidence/autoloop.py validate MANIFEST.json
python experiments/counterevidence/autoloop.py validate-resume BEFORE.json AFTER.json
python experiments/counterevidence/autoloop.py validate-receipt MANIFEST.json RECEIPT.json RESPONSE.txt
python experiments/counterevidence/autoloop.py validate-chain CHECKPOINT-000.json CHECKPOINT-001.json ...
python experiments/counterevidence/autoloop.py validate-plan-source MANIFEST.json TRACKED-PLAN.json
python experiments/counterevidence/autoloop.py preflight --execution-plane private_trusted --repository-visibility private
```

`validate*`は保存済み証跡をオフライン検査するため、hostの課金環境を読まない。モデル実行の
直前には必ず`preflight`を別途実行し、検出時は変数名だけを記録して停止する。

## rollout

1. 本PR: 公開contract、schema、fail-closed controller、behavior test
2. 別private repository: subscription認証済みrunner。秘密値を公開repoへ渡さない
3. 未使用ケースでnon-formal smoke。課金経路0、停止/再開、独立性、SHAを監査
4. 現在のcalibration完了後に接続。既存runへ途中導入しない
5. formalは既存の事前登録と人間merge承認を維持する
