# Counterevidence v4 cohort-01 provenance

- 作成日: 2026-08-15 JST
- 親実装: `メイン` merge commit `88a7a54b2a2f339c259e71d0d6311d7e63e885cc`（PR #22）
- cohort ID: `counterevidence-v4-cohort-01`
- ケース数: 60（false_coherence 18 / absence 12 / common_cause 12 / no_refutation 18）
- 文書数: 420（全ケース7文書）

## 凍結時点

このpoolを使ったA/B/C応答、manual judge応答、calibration結果、formal結果は、
凍結前にも凍結中にも1件も生成していない。既存pilotの応答や成績をケース選択へ使わず、
観測済みpilot 6 IDと既存spec案を再利用していない。

60件を一括作成した後、`cohort.py` を新規の空出力先へ実行して
calibration / formal / reserveへ20件ずつ固定した。個別ケースの成績による移動・交換はしていない。

### 第三者意味監査後の説明ID訂正

Draft PR #23の初回commit `4c5b4cb6e2f277ab874f3871200ca956b3aa3ad2` に対する
第三者意味監査で、doc_idをSHAで分散した後も `why_decisive` 内だけ旧ID表記が残る
系統的な不整合が発見された。反証の論理、質問、420文書、correct/trap conclusion、
`refutation_document_ids`、design metadataは正しく、監査ではHigh指摘なしだった。

回答・judge・成績が0件の状態で、41ケースの `why_decisive` のID表記だけを実文書へ同期した。
V4-AB-08は初版から一致していた。上記以外の意味内容が不変であることを比較検査し、
説明に現れる全IDがrefutation/correct supportへ一致することを確認した。

ケースcanonical JSONが変わるため、旧splitを上書きせず退避し、60件全体を空の出力先へ
同じcohort ID・同じ `cohort.py` で再凍結した。個別ケースを選んで移動しておらず、
性能結果による選別もない。type内のSHA順位再計算により29ケースのsplitが機械的に変わった。
初版のpool・split・manifestは上記commitのGit履歴に残る。

## SHA-256

```text
2eb654da56a1678580327b28a8ac0cc9e02e6443a933f901ff7738ee72c4be8e  pool.dataset.jsonl
f37c2968dbc3ef489885fa14f75e1ceceb626a37f185548fc28c05973370e650  pool.specs.jsonl
71cb8995bd6ae5886ddd6b5c00606066960111853ffccaaaade4ce71cd7ef53f  cohort.py
6baf9375ac1cca062c486ea1d6b19b719f4edbdb20ac01a5946c3d83a8b47d05  manifest.json
```

完全なsplitファイルSHAと全assignmentは
`results/cohorts/counterevidence-v4-cohort-01/manifest.json` を正本とする。

## 実行・費用

ケース作成、構造検査、凍結には外部生成API、judge API、Agent SDKを使用していない。
このPRでは回答生成・採点・KEEP/DROP判定を行わない。

## 既知の限界

自動検査は文書数、参照、構成、重複、分割SHAを検証するが、反証の意味的な成立性、
trapと正答の情報量差、過剰断定、ケース間の概念重複までは保証しない。
回答生成前に、実装者から独立した第三者が60件すべてを意味監査する。
