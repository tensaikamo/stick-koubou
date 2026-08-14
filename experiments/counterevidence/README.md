# 実験1：counterevidence pipeline は残すべきか

## 検証する仮説（修正後）

> **明示的な falsifier を使って反証候補を再走査し、その後に再評価する counterevidence pipeline は、
> 一体型の long-think control B より誤った trap 結論を減らすか。**

**「falsifier を先に書くこと単独の効果」ではない。** Condition C は以下の一連の工程からなる。

1. provisional conclusion の生成
2. falsifier の生成
3. falsifier 由来の検索語の生成
4. 該当文書の再走査と再提示（re-ranking）
5. 再評価

**この実験だけでは、2〜5 のどの部品が因果的に効いたかは分離できない。**
効果が観測された場合、それはパイプライン全体の効果である。
どの部品が効いたかを知るには、部品ごとの追加アブレーションが必要になる。

`score.py` は、Cの固定反証文書の検索ヒットと最終正答を記述的に並べるだけで、
検索・認識・更新の因果的な工程分離は行わない。

## 成功条件

反証探索の価値を証明することではない。**残すか削るかを判定できること。**

---

## 識別する3仮説

| | 内容 | これが真なら見えるもの |
|---|---|---|
| H1 | pipeline は誤った整合への固定を減らす | 反証可能ケースの Trap Rate が C で B より明確に低い |
| H2 | 効果に見えるものは推論量の効果 | C と B に差がない、または token 比で説明できる |
| H3 | 疑いすぎて正解も壊す | C の Correct Destruction が B より高い |

**B vs C がprimary比較。** Aは通常回答の記述的baselineとして、Accuracy / Trap Rate /
Decisive Refutation Citation を同じ表に残す。

---

## 条件

| | 内容 | LLM呼び出し |
|---|---|---|
| **A** | 全文書 + 質問 → 結論 | 1 |
| **B** | 全文書 + 質問 + 対抗仮説2つ・各文書の照合・反証の検討・全文書の読み返しを明示要求 → 結論 | 1 |
| **C** | ①暫定結論 + falsifier 3つ + 検索語5つ → ②該当文書を再提示して最終結論 | 2 |

**全条件が全文書を見る。** C に情報上の優位を与えない。

**B と C は同一形式の JSON を返す。** `initial_conclusion` の定義文は
`prompts.py` の共有定数を両者が使う。出力依存の対象を作らないため、
`counterevidence_documents` は要求しない。決定的反証文書の引用は、全条件に共通する
`key_documents` から記述的に集計する。

C は C1 の暫定結論を C2 の `initial_conclusion` へそのままコピーする。
一致しない場合は `INVALID_ANCHOR` とし、効果判定を出さない。
全条件の `key_documents` から **Decisive Refutation Citation**（決定的反証文書の引用率）を
集計する。名称どおり「引用」を測り、検索到達とは解釈しない。C の `retrieved_docs` は別に
**Fixed-refutation Retrieval Hit**（固定反証文書の検索ヒット率）として集計する。
どちらも全反証可能ケースを分母にした記述的指標で、primary判定には使わない。

`initial_conclusion` の意味を judge で分類して工程指標の分母に使うことはしない。
モデル出力で分母が変わり、B/C比較が成立しなくなるうえ、追加judgeコストが発生するためである。

B は1呼び出しなので、本当に反証検討前に考えた説明かを外部から完全には検証できない。
これは残る限界であり、完全に統制するにはBも二段階化する別実験が必要になる。

---

## データセット構成

| 種別 | full(20) | pilot(現在6) |
|---|---|---|
| False Coherence | 6 | 2 |
| Absence | 4 | 1 |
| Common Cause | 4 | 1 |
| No-refutation | 6 | 2 |

`refutation_document_ids` は配列。`refutation_match` が `ANY` なら1件でも引用すれば発見、
`ALL` なら全件必要。ケースごとに ground truth 側で指定する。

> **`ANY` に入れてよいのは、その文書だけで trap を否定できる文書に限る。**
> 補強材料や前提情報を含めてはならない。含めると、実際には反証を発見していない回答を
> 「発見した」と誤って採点する。
> **単独では成立せず、複数文書が揃って初めて反証になる場合は `ALL` を使う。**
>
> 現在の4ケースはこの基準で監査済み：
>
> | ケース | 判定 | 理由 |
> |---|---|---|
> | FC-01 | `["d4"]` ANY | d4の材料単価横ばいのみがtrapを直接否定する。d6（原価構成比）は物流費説を支持するが、材料費の絶対額上昇を否定していない |
> | FC-02 | `["d4"]` ANY | d4は賃金帯同等かつ3課の退職者0名を含み単独で成立。d3（賃金テーブル同一）は結果を含まない |
> | AB-01 | `["d2","d3"]` **ALL** | d2で「毎年4月に発注が出る」期待を確定し、d3でその不成立を確認する。どちらか一方では反証にならない |
> | CC-01 | `["d4"]` ANY | d4は媒介変数への介入で結果が動かなかったことを示す。d5（受注減で両方低下）は残業→不良が真でも同じ観測になる |
>
> **A/B/C の反証可能ケースは、malformed・other・unclearを含めて全行を各指標の分母に残す。**
> 除外すると分母が縮み、難しいケースで失敗した側が有利になる。

**ground truth は資料から言える範囲を超えない。**
例：AB-01 は「定期発注の不在から取引継続とは判断できない」までであり、
「資金繰り悪化」とは断定しない。CC-01 は「残業増が主因という説明は支持されない」までであり、
「因果関係は一切ない」とは断定しない。過剰断定した正解は、**正しく慎重な回答を誤答扱いする。**

---

## pilot と full は完全に別物

> **6件パイロットの結果で部品を KEEP / DROP してはならない。**

`score.py --mode pilot` は KEEP/DROP を構造的に出さない（`verdict_full` を呼ばない）。
`score.py --mode full` は 20件・6/4/4/6 を code 側で強制し、満たさなければ採点自体を拒否する。

**パイロットの目的は4つだけ：**
1. プロンプトが機能するか
2. JSON が安定するか（malformed 率）
3. retrieval が壊れていないか
4. 明白な floor / ceiling effect がないか

---

## 実行

```bash
python selftest.py                          # 実験前に必ず通す
python run.py --validate                    # データセット検証

# 手動（各プロンプトは新しい会話で実行すること）
# provider/model は実際に使う値を必ず明示する。temperature不明なら省略し、unknownで残す。
python run.py --backend manual --stage 1 \
  --provider anthropic --model claude-haiku-4-5-20251001  # 18 prompts
python run.py --backend manual --stage 2    # C1応答から C2 生成
python run.py --backend manual --stage 3    # raw.jsonl へ集約

# judgeもAPIを使わない（ChatGPT / Claude / Grok等の契約済みUIを1つに固定）
python score.py --mode pilot --judge manual --manual-stage export \
  --judge-provider openai --judge-model '<UIに表示されたモデル名>'
# results/runs/<run_id>/manual_judge/HOW_TO.txt に従い、各JSONを保存
python score.py --mode pilot --judge manual --manual-stage score

# API（20件揃ってから）
python run.py --backend api --trials 3 --require-full
python score.py --mode full
```

### API課金なしの手動judge

`--judge manual` はAnthropic SDKをimportせず、外部APIを呼ばない。`export` は正式な
judge promptを `manual_judge/prompts/` へ書き出し、`score` は同番号の
`manual_judge/responses/` に保存されたJSONだけを取り込む。raw・dataset・specs・実コード・
各prompt・各responseをSHA-256で束縛し、欠損、形式不正、prompt改変、コード変更は
`INCOMPLETE_JUDGMENTS` としてfail-closedにする。同一promptは1件にまとめるが、採点時の
論理call数はmanifestに別記する。全promptが採点経路で消費された後だけmanifestを
`complete` にし、全response SHAを固定してprompt・response・manifestをread-onlyにする。
完了後に応答本文が変わった場合は再採点を拒否する。最終manifest SHAはGit追跡対象の
`results/run_attestations.json` の同一run entryにも追記する。実run後はこの台帳差分を
証跡PRとしてcommitするまで、ローカルファイルだけを改ざん不能とはみなさない。

各promptは別の新規チャットへ貼り、全件で同じprovider/modelを使う。途中で利用上限に
達したら、回復後に同じモデルで続ける。別モデルへ切り替えた応答を同じpacketへ混ぜない。
provider/modelは操作者申告の `unverifiable_manual` であり、UIのサブスクリプション利用を
API応答の `reported_model` のように外部検証することはできない。manifestと採点結果には
`route: subscription_ui` / `api_used: false` を保存する。

無料APIや別モデルを、凍結済みjudgeの無言の代替にしてはならない。使う場合は別の
再現性・感度分析としてrunと事前登録を分離する。manual生成ではtoken情報が得られないため、
manual judgeを使っても正式な無条件 `KEEP` には到達せず、従来どおりAPI追試が必要である。

### 実行証跡と会話分離の hard gate

stage 1 は一意な `run_id` を発行し、以後のファイルを
`results/runs/<run_id>/` に分離する。過去runや回収証跡を上書きしない。
`results/active_run.txt` は操作対象を指すだけで、証跡本体ではない。

`run_manifest.json` と各 raw 行には次を保存する。

- backend / provider / model / temperature（不明は `unknown` と明記）
- run開始・API呼び出し完了・手動応答ファイル取込のUTC時刻（ファイルmtimeとは別）
- dataset / specs / prompts.py / run.py / score.py の SHA-256
- 実際に提示した各promptと保存した各responseの SHA-256
- C2を作ったC1応答、暫定結論、falsifier、query、retrieved doc IDの対応

各promptは**必ず新しい会話**で実行する。同一会話を使い回すと前条件の文脈が次条件へ
混入するため、そのrunは無効。手動UIでtemperature等が確認できない場合、推測値を入れず
`unknown` のまま残す。会話やモデルの利用上限に当たって別モデルへ切り替えた場合も、
同じrunへ混ぜず新しいrunを開始する。

manual backend のモデル名は操作者の申告であり、UIから貼り付けた本文だけでは真偽を
検証できない。manifest へ `model_verification: "unverifiable_manual"` を保存し、採点時にも
警告する。API backend は `provider_reported` とし、各応答の `reported_model` が要求モデルと
一致し、`stop_reason == "end_turn"` の場合だけ受理する。

stage 2/3 は入力・コード・prompt SHAのドリフトを拒否する。C1またはそこから作るC2 promptが
変わった場合、既存C2応答は `responses/invalidated/` へ退避して無効化し、新しいpromptへ
流用しない。stage 3 は全6ケースの A/B/C1/C2応答、C2生成証跡、response SHAが揃うまで
`raw.jsonl` を生成しない。C2応答はC2 prompt生成後に収集されたものだけを束縛する。

`raw.jsonl` が一度でも存在したrunは、manifestの `status` に関係なく stage 2/3 の再実行を
拒否する。完了時には raw とmanifestを read-onlyにし、両者のSHAを
`results/run_attestations.json` へ追記する。この台帳はGit追跡対象なので、実run後は台帳差分を
別PRでcommitして外部アンカーにする。commit前のローカル台帳や read-only 属性だけを、
改ざん不能の保証とはみなさない。

採点器は任意の単独rawを信用しない。同じrunのmanifest、台帳、実dataset/specs、実コード、
prompt/response SHAを照合し、provenance欠損、異なるrun/モデル/設定の混在、raw本文と
response SHAの不一致を `INCOMPLETE_RESULTS` として拒否する。壊れたJSON、不在ファイル、
不正run_idもtracebackではなく同じくfail-closedにする。

### 旧AB-01 C応答の扱い

`results/responses/AB-01__C1.txt` と `AB-01__C2.txt` は2026-08-13回収版の証跡として
保持する。ただし上記manifest導入前で、A/B未実行かつ実行条件証跡も不足するため、
新パイロットへ混ぜない。削除や上書きをせず、再開時は新しいrunで6ケースすべてを実行する。

`--trials` は**同一プロンプトの確率的反復番号**。Anthropic API へ実 seed は渡していない
（渡せない）ため、再現可能な seed ではない。`seed` とは呼ばない。

> **評価単位は case であり、trial は case 内の確率的反復である。**
> `--trials 3` でも実験の N は 20（反証可能ケースは 14）のままであり、60 でも 42 でもない。
> trial を独立 N として扱ってはならない。事前登録の閾値 +0.20 は 14 件基準で設定されている。

**full 判定前に raw.jsonl の完全性を強制する。**
全 case × A/B/C × 全 trial が揃っていること、trial ID が連番であること、
`(case_id, condition, trial)` に重複がないことを検査し、1件でも欠ければ
`INCOMPLETE_RESULTS` として採点を拒否する（`--trials 3` なら 20×3×3 = 180 行）。
難しいケースだけが欠けた条件は、その条件を不当に有利にするため。

**manual backend では token 数を記録しない。** 0 で埋めると token 比が 0 になり、
「計算量差では説明できない」を誤ってクリアしてしまうため、フィールド自体を作らない。

---

## 事前登録v3の判定基準（2026-08-15凍結）

`score.py` の `PRE_REGISTRATION` に固定。**結果を見てから変更した場合、その実験は無効。**

v1のRefutation Discovery Rateは、promptが「モデル自身のinitial」を弱める文書を求める一方、
採点器が「固定trap」を弱める文書を数えており、同じ構成概念を測っていなかった。
v2のTrap Rate reductionは、trap→`other` / `unclear` / malformedをtrap→correctと同額で
評価でき、正答を1件も増やさずKEEPに到達できた。また工程指標を`initial=trap`に限定したため、
分母がモデル出力に依存してB/Cで食い違った。formal rowはまだ0件で、観測済みAB-01 smokeでも
B/Cの正答は同値だったため、C−Bの正答率差は未観測である。静的監査結果を全開示したうえで、
結果生成前にv3へ改訂した。

v2へのオフライン合成probeでは、正答増加0のままtrap→otherへ移したケースと、Cの反証可能
14行をすべてmalformedにしたケースの双方でKEEPに到達できた。これは実モデルの効果観測ではなく、
判定ロジックの反例である。

反証可能ケースは **14件**（false_coherence 6 + absence 4 + common_cause 4）。
no_refutation は **6件**。

| 判定 | 条件 |
|---|---|
| **KEEP** | 反証可能ケースの Refutable Accuracy gain (C−B) ≥ **+0.20**、Trap Rate(C) ≤ Trap Rate(B)、Accuracy(C) ≥ Accuracy(B)、B/C malformed=0、Correct Destruction(C) ≤ 0.25、token比 < 2.0 |
| **CONDITIONAL KEEP** | primary・Trap Rate・Accuracy・malformed gateを満たすが、破壊率超過 / token比 ≥ 2.0 / **token情報欠損** |
| **DROP** | Refutable Accuracy (C−B) < +0.20、Trap Rate悪化、または総合正答率で C が B を下回る |
| **INVALID_OUTPUT** | BまたはCにmalformed JSONが1件以上。効果判定を出さない |
| **INVALID_ANCHOR** | BまたはCで `initial_conclusion` のanchor不一致が1件以上。効果判定を出さない |

- **+0.20** は反証可能14件中およそ **2.8件 ≒ 3件** に相当
- **0.25** は no_refutation 6件中 **1.5件** に相当
- **token情報が欠損している場合、KEEP は出せない**（`COST_COMPARISON_UNAVAILABLE`）。
  manual 実行だけで KEEP に到達することは構造的に不可能
- full の表にも `Initial-anchor mismatch` を表示する。Cの逐語コピー失敗を「反証未発見」と
  混同したまま KEEP/DROP を出さない
- primaryの分母はB/Cそれぞれ `14 × trials` 行。trialはcase内反復であり、独立Nではない
- Decisive Refutation CitationはA/B/Cすべての `key_documents`、Fixed-refutation Retrieval Hitは
  Cの`retrieved_docs`を使う。どちらも全反証可能行を分母とする記述的指標
- `initial_conclusion`への追加judge呼び出しは行わない。1行あたりのjudge呼び出しは、
  最終結論判定とunsupported判定の2回

**+0.20 未満は検出力不足であり「判定不能」だが、事前登録の規定により DROP として扱う**
（不確かな部品を残さない）。

### v3凍結前に判明していた観測（全開示）

- AB-01 A: `key_documents=["d2","d3","d6"]`
- AB-01 B/C: initialはcorrect側、`counterevidence_documents=["d1"]`、旧RDRは両方False
- retrievalはd2+d3へ到達、JSON 4/4、C anchor完全一致
- PR #18以前のAB-01 C2: `counterevidence_documents=["d2","d3"]`
- formal runの行は未生成。BとCが同値だったため効果量の符号も未観測
- smoke実物と報告書のSHA-256は `evidence/20260814/SMOKE_MANIFEST.sha256.txt` に記録し、
  実行経路と非formal用途を同ディレクトリの `PROVENANCE.txt` に固定

### runの無効化条件

1. 最初のformal row生成後に `PRE_REGISTRATION` を変更
2. 20件×全trialの完了前に中間解析または逐次打ち切り
3. run途中で生成モデル・判定モデル・設定を変更
4. `anchor_mismatch > 0`
5. 反証可能行数がB/Cそれぞれ `14 × trials` と不一致
6. B/Cいずれかにmalformed JSONが1件以上
7. 2026-08-14 smoke応答をformal runへ流用
8. 6件pilotでKEEP/DROPを判断

---

## C の記述的検索監査

Cについては、`retrieved_docs` に固定ground truthの反証文書が含まれたかと、最終結論が
正答だったかを並べて出す。これは検索ヒットの記録であり、検索・認識・更新のどこが
原因だったかという因果的な工程分離は行わない。その分離には、モデル出力に依存しない
別のprocess evalまたはアブレーションが必要である。

---

## 生き残っている弱点（隠さない）

| # | 内容 | 状態 |
|---|---|---|
| 1 | **N=20 は screening**。大きい効果しか検出できない | 生き残る |
| 2 | **人工課題であり、反証が少数文書に局在**。実世界では分散している | 生き残る。C に有利な可能性 |
| 3 | **同予算比較をしていない**（C は 2 呼び出し） | 部分的に対処（token比を判定条件に組込）。完全統制は追試 |
| 4 | **B の強さは主観的**。より強い B を書けば C の優位が消えるかもしれない | 生き残る |
| 5 | **judge も LLM**。判定自体が偏りうる | 部分的に対処（候補順を case ごとに固定、sha256で再現可能） |
| 6 | **C の効果はパイプライン全体の効果**。どの部品が効いたかは分離できない | 生き残る。設計上の限界として明記 |
| 7 | `key_documents` は自己申告。**反証を理解したが根拠文書に挙げない**場合を取りこぼす | 生き残る。引用率は記述指標に限定しprimaryから除外。検索到達とは呼ばない |
| 8 | 反証ありケースが14に対し反証なしが6。**「疑う」戦略がやや有利** | 部分的に対処（4→6件へ増）。完全な対称ではない |
| 9 | **manual応答の実モデル名は外部検証不能**。操作者が途中で切替えても本文だけでは判定できない | 生き残る。`unverifiable_manual` を証跡・採点警告へ明示し、正式KEEPにはAPI追試が必要 |

---

## この実験で分かること / 分からないこと

**分かること**
- counterevidence pipeline が一体型長考に対して固有の価値を持つか
- 疑いすぎによる正解破壊が実際に起きるか、どの程度か
- Cの検索結果が固定ground truthの反証文書を含んでいたか（記述的検索ヒット）

**分からないこと**
- pipeline のどの部品が効いたか（要追加アブレーション）
- 実 Web 検索を伴う場合の効果（本実験は文書内再走査のみ）
- 長期運用での効果
- 他の部品（competing models, rollout）の価値

---

## 通過した場合にだけ次に作るもの

1. **DISCOVERY / VALIDATION 分離**の実験
2. pipeline 内部のアブレーション（falsifier生成のみ / 検索のみ / 再提示のみ）
3. 実 Web 検索版の追試

**通過しなかった場合**：counterevidence search を第一世代から削除。
Search の一本化と Model Management の反証義務も根拠を失うため見直す。
残るのは MVP-0（予測台帳）と MVP-1 のみ。
