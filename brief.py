"""ブリーフィング組み立ての純関数。

sanbo.py は上から下に流れる台本(取得→選別→執筆→公開)で、モジュール読み込みだけで
ネットワークとAPIキーを要求する。そのため **sanbo.py だけテストが1件も無い** 状態だった。
台本の形は壊さず、壊れると被害が大きい判断ロジックだけをここへ出してテスト可能にする。

依存は標準ライブラリのみ。ネットワークもLLMも触らない。
"""
import re, html

# 正しい形の用語タグ <t data-d='解説'>用語</t> だけを通すパターン。
# 属性はシングルクォート指定(JSON文字列内の二重引用符エスケープ漏れでJSON全体が
# 壊れる事故が実際に起きたため)だが、二重引用符も後方互換で受ける
T_RE = re.compile(r"""<t\s+data-d=(?:'([^'<>]{1,160})'|"([^"<>]{1,160})")\s*>([^<>]{1,60})</t>""")


def unwrap_list(v):
    # {"selected": [..]} のようにオブジェクトで包まれた配列も許容する
    if isinstance(v, dict):
        v = next((x for x in v.values() if isinstance(x, list)), None)
    return v if isinstance(v, list) else None


def render_rich(text):
    # モデル出力をHTML化する唯一の経路。正規形のtタグのみ再構築し、
    # それ以外(他のタグ・壊れたtタグ)はすべてエスケープしてページ破壊を防ぐ
    text = str(text)
    out, pos = [], 0
    for m in T_RE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append('<t data-d="' + html.escape(m.group(1) or m.group(2), quote=True) + '">'
                   + html.escape(m.group(3)) + "</t>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def norm_final(b):
    # 配列ラップを剥がし、4セクションが揃っているかを検証する
    if isinstance(b, list):
        b = next((x for x in b if isinstance(x, dict)), None)
    if not isinstance(b, dict):
        if b is not None:
            print("final unexpected shape:", repr(b)[:200])
        return None
    k = b.get("kuki") if isinstance(b.get("kuki"), dict) else {}
    r = {"omote": str(k.get("omote") or "").strip(), "ura": str(k.get("ura") or "").strip(),
         "dousuru": str(b.get("dousuru") or "").strip(), "kan": str(b.get("kan") or "").strip(),
         "kan_konkyo": str(b.get("kan_konkyo") or "").strip(),  # 勘の根拠(任意)
         "kan_hantai": str(b.get("kan_hantai") or "").strip(),  # 反証条件(任意)
         "kan_conf": b.get("kan_conf"),                          # 確度(任意・後段で中央値に置換)
         "ura_taikou": str(b.get("ura_taikou") or "").strip(),   # 退けた対抗仮説(ACH・任意)
         "ippan": str(b.get("ippan") or "").strip(),  # 一般人の超参謀(任意・無くてもフォールバックは動く)
         "evidence_map": [],
         "mijoriku": []}
    if not (r["omote"] and r["ura"] and r["dousuru"] and r["kan"]):
        print("final missing sections:", repr(b)[:200])
        return None
    # 主張と資料を機械的に結ぶ。存在しない番号は公開直前にも picked の範囲で落とすが、
    # ここでは型・負数・過大な配列を止めて「根拠っぽい自由文」への退化を防ぐ。
    ev = b.get("evidence_map")
    for x in (ev if isinstance(ev, list) else []):
        if not isinstance(x, dict):
            continue
        claim = str(x.get("claim") or "").strip()
        disconfirm = str(x.get("disconfirm") or "").strip()
        ids = []
        raw_ids = x.get("source_indices")
        for i in (raw_ids if isinstance(raw_ids, list) else []):
            if isinstance(i, int) and not isinstance(i, bool) and i >= 0 and i not in ids:
                ids.append(i)
            if len(ids) == 4:
                break
        try:
            conf = max(0.05, min(0.95, float(x.get("confidence"))))
        except (TypeError, ValueError):
            continue
        if claim and disconfirm and ids:
            r["evidence_map"].append({"claim": claim, "source_indices": ids,
                                      "confidence": conf, "disconfirm": disconfirm})
        if len(r["evidence_map"]) == 3:
            break
    mj = b.get("mijoriku")
    for x in (mj if isinstance(mj, list) else []):
        if isinstance(x, dict) and all(str(x.get(f) or "").strip() for f in ("title", "desc", "why")):
            r["mijoriku"].append({f: str(x[f]) for f in ("title", "desc", "why")})
        if len(r["mijoriku"]) == 2:
            break
    return r


# 合議が「割れた」と認める最小の票差。これ未満なら3人が同じことを言っただけで、
# 中央値を取っても情報は増えていない。
SPREAD_MIN = 0.05


def vote_spread(votes):
    """アンサンブル票の散らばり(最大−最小)。票が2件未満なら None。

    accuracy–correlation effect (Phil. Trans. R. Soc. B 2026): 合議の構成員が似てくるほど
    集約の利得は消える。実際に票が [0.12, 0.12, 0.12] と完全一致した日があり、その時の
    「3人の合議の中央値」は**合議していない**。散らばりを測って、割れていない日は
    合議した事実を誇らないための指標。
    """
    vs = []
    for v in votes or []:
        try:
            vs.append(float(v))
        except (TypeError, ValueError):
            continue
    return (max(vs) - min(vs)) if len(vs) >= 2 else None


def consensus_note(votes):
    """票の散らばりから、読者に出す一言を返す。割れていなければ注意書きを付ける。
    戻り: (散らばり or None, 注記文字列)。注記が空なら何も表示しない。"""
    sp = vote_spread(votes)
    if sp is None:
        return None, ""
    if sp < SPREAD_MIN:
        return sp, "3つの立場が同じ結論に寄った(票の幅%.2f)。合議で確度が上がったとは見なさない。" % sp
    return sp, "3つの立場が割れた(票の幅%.2f)ので中央値を採った。" % sp
