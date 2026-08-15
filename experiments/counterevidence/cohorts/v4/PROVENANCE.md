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

60件を一括作成した後、`cohort.py` を新規の空出力先へ1回実行して
calibration / formal / reserveへ20件ずつ固定した。個別ケースの移動・交換・再分割はしていない。

## SHA-256

```text
316a59d1cb82fb0e1d11588aafaaa06d2099b5933168645bd891be2b35ab5e32  pool.dataset.jsonl
ffbb5c3661f9829831897a62b9e03c4a0b1ac82ef325502d8f3f4700548a5dc4  pool.specs.jsonl
71cb8995bd6ae5886ddd6b5c00606066960111853ffccaaaade4ce71cd7ef53f  cohort.py
71c0f4df876109750aaf054a9b6b57debc77b4a6a15dfa6bcd32e5dc956e221d  manifest.json
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
