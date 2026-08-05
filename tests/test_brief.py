"""ブリーフィング組み立ての純関数(brief.py)。

sanbo.py は上から下に流れる台本で import すら出来ず、テストが1件も無かった。
壊れると被害が大きい判断ロジックだけを brief.py に出したので、ここで守る。
"""
import brief


def test_render_rich_escapes_everything_except_proper_term_tags():
    # 正規形のtタグだけ再構築し、他のタグはすべてエスケープしてページ破壊を防ぐ
    out = brief.render_rich("<script>alert(1)</script>")
    assert "<script>" not in out and "&lt;script&gt;" in out
    ok = brief.render_rich("今日の<t data-d='大規模言語モデル'>LLM</t>は強い")
    assert '<t data-d="大規模言語モデル">LLM</t>' in ok
    # 壊れたtタグは通さない(閉じ忘れ)
    assert "<t" not in brief.render_rich("<t data-d='x'>用語")


def test_render_rich_escapes_quotes_inside_the_description():
    out = brief.render_rich("""<t data-d='彼は"強い"と言った'>用語</t>""")
    assert '"' not in out.split('data-d="')[1].split('"')[0] or "&quot;" in out


def test_unwrap_list_accepts_wrapped_arrays():
    assert brief.unwrap_list([1, 2]) == [1, 2]
    assert brief.unwrap_list({"selected": [3]}) == [3]
    assert brief.unwrap_list({"n": 1}) is None
    assert brief.unwrap_list("nope") is None


def _full(**over):
    b = {"kuki": {"omote": "表", "ura": "裏"}, "dousuru": "どうする", "kan": "勘",
         "ippan": "一般", "mijoriku": []}
    b.update(over)
    return b


def test_norm_final_requires_the_four_sections():
    assert brief.norm_final(_full()) is not None
    assert brief.norm_final(_full(kan="")) is None          # 必須が欠ける → 不採用
    assert brief.norm_final({"kuki": {}, "dousuru": "d", "kan": "k"}) is None
    assert brief.norm_final(None) is None
    assert brief.norm_final("文字列") is None
    # 配列で包まれていても最初のdictを拾う
    assert brief.norm_final([_full()])["kan"] == "勘"


def test_norm_final_caps_mijoriku_and_drops_incomplete_entries():
    mj = [{"title": "a", "desc": "b", "why": "c"},
          {"title": "x", "desc": "", "why": "z"},          # desc 欠け → 落とす
          {"title": "d", "desc": "e", "why": "f"},
          {"title": "g", "desc": "h", "why": "i"}]
    r = brief.norm_final(_full(mijoriku=mj))
    assert [m["title"] for m in r["mijoriku"]] == ["a", "d"]   # 2件で打ち止め


def test_vote_spread_measures_disagreement():
    assert brief.vote_spread([0.2, 0.38, 0.15]) == 0.38 - 0.15
    assert brief.vote_spread([0.12, 0.12, 0.12]) == 0.0
    assert brief.vote_spread([0.5]) is None                  # 1票では散らばりを測れない
    assert brief.vote_spread([]) is None
    assert brief.vote_spread(None) is None
    assert abs(brief.vote_spread(["x", 0.4, 0.1]) - 0.3) < 1e-9   # 非数は無視して残りで測る
    assert brief.vote_spread(["x", 0.4]) is None                  # 残り1票なら測れない


def test_consensus_note_refuses_to_brag_when_votes_agree():
    """accuracy-correlation effect: 構成員が似てくるほど合議の利得は消える。
    票が一致した日は「3人の合議の中央値」と誇らない。実際に [0.12,0.12,0.12] が出た日がある。"""
    sp, note = brief.consensus_note([0.12, 0.12, 0.12])
    assert sp == 0.0 and "上がったとは見なさない" in note
    sp, note = brief.consensus_note([0.2, 0.38, 0.15])
    assert sp > brief.SPREAD_MIN and "割れた" in note
    # 票が足りなければ何も言わない
    assert brief.consensus_note([0.5]) == (None, "")
