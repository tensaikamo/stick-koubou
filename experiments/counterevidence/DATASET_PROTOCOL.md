# Counterevidence v4 cohort protocol

## 目的

2026-08-15 pilot は反証可能4件で A/B/C が全て正答し、C は no-refutation
2件中1件を壊した。6件は役割を終えた観測済みpilotであり、正式評価へ再利用しない。

v4は、回答を見る前に60件を固定し、次の3組へ20件ずつ分ける。

- `calibration`: 問題群の難易度だけを確認する
- `formal`: Cの採否を一度だけ判定する未観測本番
- `reserve`: bundle破損など、事前登録した技術的無効時だけ使う予備

各組の構成は false_coherence 6 / absence 4 / common_cause 4 /
no_refutation 6。分割は `cohort.py` がケース内容と固定cohort IDのSHA-256で決める。
人間やモデルが成績を見て個別ケースを移動・交換してはならない。

## ケース作成の最低条件

自動検査に加え、意味を独立監査する。

### 全ケース

- 6〜10文書。質問・文体・文書順だけで正答が漏れない
- `correct_conclusion` は資料から言える範囲を超えない
- `why_decisive` に、なぜその文書が反証として成立するかを書く
- 同じ事例の数値・固有名詞だけを変えた複製を作らない
- 既存6 ID（FC-01/02、AB-01、CC-01、NR-01/02）を使わない

### 反証可能ケース

- trapを自然に支持する文書を2件以上置き、`design.trap_support_document_ids` に記録
- 正答を支持する文書を1件以上置き、`design.correct_support_document_ids` に記録
- trap支持文書と決定的反証文書を重複させない
- `ANY` は単独でtrapを否定できる文書だけ、複数必要なら `ALL`
- correct文書を目立つタイトルや「反証」「真因」等のラベルで露出しない

### no-refutationケース

- 正しい主因を支持する独立文書を複数置く
- 一見もっともらしい代替原因を2件以上置き、
  `design.plausible_alternative_document_ids` に記録
- 代替原因がなぜ成立しないかを `design.why_alternatives_fail` に記録
- 「疑えば必ず別原因が見つかる」構造にしない

自動検査は意味の真偽を保証しない。第三者は、決定的反証の単独成立性、正答の過剰断定、
trap/正答の情報量差、ケース間重複を必ず読む。

## 凍結と分割

```bash
python cohort.py \
  --pool cohorts/v4/pool.dataset.jsonl \
  --specs cohorts/v4/pool.specs.jsonl \
  --cohort-id counterevidence-v4-cohort-01 \
  --out results/cohorts/counterevidence-v4-cohort-01
```

出力先の上書きは禁止。manifestは入力・`cohort.py`・各split・全assignmentのSHAを持つ。
分割前にA/B/C応答を1件でも生成したpoolは無効。

## calibration gate

calibrationは `score.py --mode calibration` で実行する。cohort受理に使うのはBだけ。
Cの成績を見て「Cが勝ちやすいcohort」を選ばない。

- B Refutable Accuracy: 5/14以上、10/14以下
- B Correct Destruction: 0.25以下
- B/C malformed: 0（難易度選択ではなく実行の完全性gate）
- B/C anchor mismatch: 0（同上）
- 分母: 14 × trials

範囲外なら `COHORT_REJECT`。個別ケースを修理・交換せず、60件全体を破棄し、
新しいcohort ID・新しい事前登録で最初から作る。formalとreserveは実行しない。

## formal gate

calibrationが `COHORT_ACCEPT` の場合だけ、封印していたformal 20件を一度だけ実行する。
結果はv4の性能・安全・workload gateで判定する。途中解析、欠損行の除外、ケース交換、
reserveとの入替は禁止。

manual UIではtokenを推測しない。提示したpromptと保存したresponseの実ファイルから
UTF-8 byteを数え、C/B workload比を計算する。これはtokenの代用品ではなく、固定料金UIで
消費する入出力量の監査可能なproxyである。合格名は `SUBSCRIPTION_KEEP` とし、
特定APIモデルへの一般化はしない。

## reserveを使える唯一の条件

モデル性能や成績を理由にreserveへ交換してはならない。次の技術的無効だけを、結果を見る前の
ログで証明できる場合に限り、cohort全体のformalを捨てreserve 20件で新runを開始できる。

- ファイル破損またはSHA不一致
- UI障害で応答本文を回収できない
- run途中のモデル・設定変更が判明
- 会話分離違反
- malformedまたはanchor mismatchにより正式判定が無効

一部ケースだけの差替えは禁止。reserve使用理由と破棄したrun SHAを証跡PRへ残す。
