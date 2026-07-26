"""index の「追跡中の予測」「答え合わせ」ブロックを組む純関数。

sanbo.py にインラインで埋まっていたものを、**挙動を変えずに**切り出した。
狙いは2つ:
- 決着表示(○×・的中率・Brier・シグナル点灯)を**テストできる**ようにする。
  8/5の初決着まで本番で一度も動かない、という状態を無くす。
- preview.py と本番で**同一コード**を使い、表示がズレないようにする。

依存は memory と標準ライブラリのみ(LLM もネットワークも使わない)。
"""
import html
from datetime import datetime


def tracking_html(hunches, today, limit=3):
    """追跡中の予測(判定待ち)。指標が点灯していれば期日を待たずに生死バッジを出す。
    hunches: 台帳配列 / today: date / 戻り: HTML文字列(該当なしなら空文字)。"""
    tracking = [h for h in reversed(hunches) if h.get("status") == "pending"][:limit]
    if not tracking:
        return ""
    rows = ""
    for h in tracking:
        dl = h.get("deadline", "")
        drem = ""
        try:
            delta = (datetime.strptime(dl, "%Y-%m-%d").date() - today).days
            drem = ("・残り%d日" % delta) if delta >= 0 else ("・期限超過%d日" % (-delta))
        except Exception:
            pass
        sg = ""
        for s in (h.get("signals") or [])[-2:]:
            cls = "b-miss" if s.get("dir") == "kill" else "b-hit"
            lab = "⚠ 死亡シグナル" if s.get("dir") == "kill" else "🔥 確認シグナル"
            sg += ('<br><span class="badge ' + cls + '">' + lab + '</span> <span class="m">'
                   + html.escape(str(s.get("date", "")) + " " + str(s.get("why") or s.get("sign", ""))[:70])
                   + '</span>')
        rows += ('<li>' + html.escape((h.get("claim", "") or "")[:64])
                 + ' <span class="m">期限' + html.escape(dl) + drem + '</span>' + sg + '</li>')
    return ('<section class="reveal"><h2>追跡中の予測</h2>'
            '<p class="m">期日が来たら○×が付く。加えて毎日、生死を示す指標が点灯していないかを見張っている。</p>'
            '<ul>' + rows + '</ul>'
            '<p class="m"><a href="hunches.html">すべての予測と答え合わせ →</a></p></section>')


def answers_html(hunches, today, memory):
    """答え合わせ節(的中率・Brier・次の決着・直近の○×)。memory モジュールを受け取る
    (import の向きを一方向に保ち、テストから差し替えやすくするため)。"""
    if not hunches:
        return ""
    st = memory.hit_stats(hunches)
    if st["total"]:
        br = memory.brier(hunches)
        head = ('<span class="hitrate">的中率 ' + str(round(st["rate"] * 100)) + '%</span> '
                '<span class="m">(的中' + str(st["hit"]) + '/外し' + str(st["miss"])
                + '・判定待ち' + str(st["pending"]) + ')</span>'
                + (('<br><span class="m">Brier ' + ("%.3f" % br["score"])
                    + '（低いほど良い。常に50%と答えるだけなら0.250）</span>') if br["score"] is not None else ""))
    else:
        head = '<span class="m">まだ答え合わせ前。判定待ち ' + str(st["pending"]) + ' 件(期日が来たら○×が付く)</span>'
    nd = memory.next_due(hunches, today)
    due_line = ""
    if nd:
        dd, rem = nd
        try:
            mdt = datetime.strptime(dd, "%Y-%m-%d")
            mds = str(mdt.month) + "/" + str(mdt.day)
        except Exception:
            mds = dd
        due_line = '<p class="m">次の決着: ' + html.escape(mds) + '(あと' + str(rem) + '日)</p>'
    decided = [h for h in reversed(hunches) if h.get("status") == "resolved"][:3]
    rows = "".join('<li>' + {"hit": "○", "miss": "×"}.get(h.get("result"), "—") + " "
                   + html.escape((h.get("claim", "") or "")[:48]) + "</li>" for h in decided)
    return ('<section class="reveal"><h2>答え合わせ</h2><p>' + head + "</p>"
            + due_line
            + ("<ul>" + rows + "</ul>" if rows else "")
            + '<p class="m"><a href="hunches.html">勘の台帳(全予測と○×)→</a></p></section>')
