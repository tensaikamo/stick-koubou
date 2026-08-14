"""
A/B/C 3条件のプロンプト定義。

設計上の固定事項:
- 全条件が全文書を見る。Cに情報上の優位を与えない。
- Bは対抗仮説・反証・再検討まで明示要求する強いcontrol。
- B と C は同一形式のJSONを返す。initial_conclusion の定義文は
  B/C で完全に同一でなければならない。
- 出力依存の対象を作らないため、counterevidence_documents は要求しない。
  決定的反証文書の引用は全条件共通の key_documents だけから記述的に集計する。
"""

_DOC_FMT = "[{doc_id}] {title}\n{text}"


def format_docs(documents):
    return "\n\n".join(_DOC_FMT.format(**d) for d in documents)


# ---------------------------------------------------------------- 出力形式
_TAIL_A = """
出力は次のJSONのみ。前置き・後置き・コードフェンス禁止。

{
  "conclusion": "結論を1〜2文で。原因を特定する形で書く",
  "key_documents": ["結論の根拠にした doc_id"],
  "confidence": "強く支持|暫定|保留"
}
"""

# ↓この定義文は B と C で完全に同一。片方だけ変更してはならない。
_INITIAL_CONCLUSION_DEF = (
    '  "initial_conclusion": '
    '"反証を検討する前に最初にもっともらしいと判断した説明。'
    '後から書き換えず1〜2文",'
)

_TAIL_BC = """
出力は次のJSONのみ。前置き・後置き・コードフェンス禁止。

{
%s
  "conclusion": "結論を1〜2文で。原因を特定する形で書く",
  "key_documents": ["結論の根拠にした doc_id"],
  "confidence": "強く支持|暫定|保留"
}
""" % _INITIAL_CONCLUSION_DEF


# ---------------------------------------------------------------- A: Normal
COND_A = """以下は、ある業務上の状況に関する社内文書です。

{docs}

質問: {question}
{tail}"""


# ---------------------------------------------------------------- B: Long-think control
COND_B = """以下は、ある業務上の状況に関する社内文書です。

{docs}

質問: {question}

回答する前に、頭の中で十分に検討してください。少なくとも次を行うこと。

1. まず思いついた説明を書き出す
2. それとは異なる対抗仮説を最低2つ考える
3. 各文書が、どの仮説を支持し、どの仮説と矛盾するかを一つずつ確認する
4. 最初の説明が間違っているとしたら何が根拠になるかを考える
5. 見落としている文書がないか全文書を読み返す
6. 以上を踏まえて結論を出す

検討過程は出力しなくてよい。結論だけを出してください。
ただし initial_conclusion には、手順1で最初に考えた説明を後から書き換えずに入れてください。
{tail}"""


# ---------------------------------------------------------------- C: Counterevidence pipeline
COND_C_STEP1 = """以下は、ある業務上の状況に関する社内文書です。

{docs}

質問: {question}

まず暫定的な結論を出し、次にその結論の falsifier を作ってください。

falsifier とは「これが観測されたら、この暫定結論を捨てなければならない」という具体的な事実のことです。
「証拠が不十分だったら」のような曖昧なものは falsifier ではありません。
暫定結論が主張している因果メカニズムを、直接否定する観測を書いてください。

出力は次のJSONのみ。前置き・後置き・コードフェンス禁止。

{
  "provisional_conclusion": "暫定結論を1〜2文で",
  "falsifiers": [
    "これが事実なら暫定結論は成立しない、という観測を具体的に。3つ",
    "",
    ""
  ],
  "queries": [
    "上の falsifier に該当する記述を文書群から探すための検索語。5つ。単語または短い句",
    "", "", "", ""
  ]
}"""

COND_C_STEP2 = """先ほどあなたは、次の暫定結論を出しました。

暫定結論: {provisional}

そして、この結論を捨てるべき条件（falsifier）として次を挙げました。

{falsifiers}

falsifier に対応する記述を探した結果、以下の文書が該当しました。

{retrieved}

参考のため、全文書を再掲します。

{docs}

質問: {question}

上で該当した文書を読んだうえで、最終結論を出してください。
暫定結論を維持すべきならそのまま維持してかまいません。変更すべきなら変更してください。
falsifier に該当する事実が見つからなかったこと自体は、暫定結論を支持する証拠ではありません。
全文書に基づいて、維持・変更・保留を判断してください。
出力の initial_conclusion には、冒頭に示した暫定結論を一字一句変更せず入れてください。
{tail}"""


# ---------------------------------------------------------------- 採点用
JUDGE = """2つの候補結論と、ある回答があります。回答がどちらの候補に一致するかを判定してください。

候補1: {cand1}

候補2: {cand2}

回答: {answer}

判定基準:
- 表現の違いや断定の強さは無視し、主張している原因が実質的に同じかで判定する
- どちらとも実質的に異なる場合は "neither"
- どちらとも判別できない曖昧な回答は "unclear"

出力は次のJSONのみ。

{"match": "1|2|neither|unclear", "reason": "1文"}"""


JUDGE_NR = """基準となる結論と、ある回答があります。
回答が基準結論と実質的に一致しているかだけを判定してください。

基準結論: {reference}

回答: {answer}

判定基準:
- 表現の違いや断定の強さは無視し、主因として挙げているものが実質的に同じかで判定する
- 異なるものを主因として挙げている場合は false
- 主因を特定していない、または判別できない場合は false

出力は次のJSONのみ。

{"matches_reference": true, "reason": "1文"}"""


UNSUPPORTED = """以下の文書群と、それに基づく回答があります。

文書群:
{docs}

回答: {answer}

回答の中に、文書群のどこにも記載がなく、文書から論理的にも導けない事実主張が含まれているかを判定してください。
一般常識や、文書からの妥当な推論は「含まれていない」と扱ってください。

出力は次のJSONのみ。

{"has_unsupported": true, "items": ["該当箇所"], "reason": "1文"}"""


def _fill(template, **kw):
    """str.format を使わない。テンプレート内のJSON波括弧をエスケープ不要にする。"""
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def build_a(case):
    return _fill(COND_A, docs=format_docs(case["documents"]),
                 question=case["question"], tail=_TAIL_A)


def build_b(case):
    return _fill(COND_B, docs=format_docs(case["documents"]),
                 question=case["question"], tail=_TAIL_BC)


def build_c1(case):
    return _fill(COND_C_STEP1, docs=format_docs(case["documents"]),
                 question=case["question"])


def build_c2(case, provisional, falsifiers, retrieved_docs):
    fals = "\n".join("- " + f for f in falsifiers)
    ret = (format_docs(retrieved_docs) if retrieved_docs
           else "（falsifier に該当する記述は見つかりませんでした）")
    return _fill(COND_C_STEP2, provisional=provisional, falsifiers=fals,
                 retrieved=ret, docs=format_docs(case["documents"]),
                 question=case["question"], tail=_TAIL_BC)


def build_judge(cand1, cand2, answer):
    return _fill(JUDGE, cand1=cand1, cand2=cand2, answer=answer)


def build_judge_nr(reference, answer):
    """no_refutation ケース専用。二択候補方式を使わない。"""
    return _fill(JUDGE_NR, reference=reference, answer=answer)


def build_unsupported(docs_text, answer):
    return _fill(UNSUPPORTED, docs=docs_text, answer=answer)
