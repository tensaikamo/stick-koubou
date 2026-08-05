"""決着表示(追跡中の予測 / 答え合わせ)の検証。
最初の期日は8/5で、それまで本番では一度も動かない表示なので、ここで先に守る。"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import memory
import panels

JST = ZoneInfo("Asia/Tokyo")


def _h(hid, claim, days, conf=0.7, status="pending", result=None, signals=None):
    now = datetime.now(JST)
    return {"id": hid, "claim": claim, "confidence": conf, "status": status, "result": result,
            "deadline": (now + timedelta(days=days)).strftime("%Y-%m-%d"),
            "signals": signals or [], "resolved_at": (now.isoformat() if result else None)}


def test_answers_before_any_resolution():
    # 決着ゼロでも壊れず、「まだ答え合わせ前」と次の決着が出る
    h = panels.answers_html([_h("a", "未決着", 12)], datetime.now(JST).date(), memory)
    assert "まだ答え合わせ前" in h and "判定待ち 1 件" in h
    assert "次の決着" in h and "あと12日" in h
    assert "的中率" not in h and "Brier" not in h


def test_answers_after_resolution_shows_provisional_score_and_brier():
    huns = [_h("a", "当てた予測", -5, 0.9, "resolved", "hit"),
            _h("b", "外した予測", -4, 0.8, "resolved", "miss"),
            _h("c", "判定待ち", 9)]
    h = panels.answers_html(huns, datetime.now(JST).date(), memory)
    assert "暫定成績" in h and "的中1/外し1" in h and "n=2" in h
    assert "参考Brier 0.325" in h                        # 少標本では参考値と明記する
    assert "的中率 50%" not in h                         # n=2の派手な率表示を避ける
    assert "○ 当てた予測" in h and "× 外した予測" in h   # 直近の○×が並ぶ


def test_tracking_shows_signals_with_correct_badges():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    huns = [_h("a", "確認が点灯した予測", 7, signals=[
                {"date": today, "sign": "料金表に載る", "dir": "confirm", "why": "一覧に追加された"}]),
            _h("b", "死亡が点灯した予測", 9, signals=[
                {"date": today, "sign": "求人が消える", "dir": "kill", "why": "職種が取り下げられた"}])]
    h = panels.tracking_html(huns, datetime.now(JST).date())
    assert "🔥 確認シグナル" in h and "一覧に追加された" in h
    assert "⚠ 死亡シグナル" in h and "職種が取り下げられた" in h
    assert "b-hit" in h and "b-miss" in h
    assert "残り7日" in h


def test_empty_and_resolved_only_are_safe():
    today = datetime.now(JST).date()
    assert panels.tracking_html([], today) == ""      # 判定待ちが無ければ節ごと出さない
    assert panels.answers_html([], today, memory) == ""
    # 決着済みだけ(判定待ちゼロ)でも落ちない
    h = panels.answers_html([_h("a", "済", -3, 0.6, "resolved", "hit")], today, memory)
    assert "暫定成績" in h and "的中1/外し0" in h and "次の決着" not in h


def test_escapes_user_content():
    # モデル出力がHTMLを壊さない(タグはエスケープされる)
    h = panels.tracking_html([_h("a", "<script>alert(1)</script>", 3)], datetime.now(JST).date())
    assert "<script>" not in h and "&lt;script&gt;" in h
