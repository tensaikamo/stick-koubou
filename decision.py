"""個人参謀の意思決定層。

ニュースを増やすのではなく、利用者の制約に合う一手と、後から採点される一つの予測を
選ぶ。ネットワークや LLM から切り離した純関数を中心にして、内容品質をテスト可能にする。
"""
import json
import re
import urllib.request


PROFILE = {
    "devices": ["iPhone"],
    "daily_minutes": 30,
    "budget": "原則無料。課金は明確な回収見込みがある場合だけ",
    "working_style": "朝か夜に、一度に一つだけ進める",
    "goal": "AIを使ってWebアプリや小さな収益実験を完成させる",
}

PROFILE_PROMPT = """【実行環境・絶対条件】
- 使える端末はiPhoneだけ。Safari、ChatGPT、GitHubのWeb画面で完了する行動に限る
- 1日の作業は30分以内。一度に一つだけ進める
- 原則無料。PC、GPU、ローカル開発環境、CLI、Docker、Python実行を要求するな
- 読むだけ・調べるだけで終わらせず、Webアプリ、文章、応募、検証結果など成果物を1つ残す
- 新しい案へ毎日乗り換えず、進行中のWebアプリ改善を優先する
この条件に反する提案は、内容が高度でも不合格である。"""


_BLOCKED_ACTIONS = (
    (re.compile(r"(?i)\b(?:vllm|docker|python|bash|npm|pip|terminal|cli)\b"), "PC用ツールが必要"),
    (re.compile(r"GPU|ローカルモデル|ローカル環境|自前運用|パソコン|\bPC\b|Mac|Windows"),
     "iPhoneだけでは完了できない"),
    (re.compile(r"サーバーを(?:立て|構築)|環境を構築|スクリプトを(?:書|実行)"),
     "開発環境が必要"),
)

_PASSIVE_END = re.compile(r"(?:読む|調べる|検討する|眺める|確認する)$")
_OUTCOME_WORDS = ("書く", "残す", "作る", "送る", "測る", "Issue", "応募", "公開", "投稿",
                  "更新", "修正", "解約", "申し込", "登録", "削除", "比較表")

SAFE_FALLBACK_MOVES = [
    {"t": "GitHubの改善候補を1件Issueに書く",
     "why": "iPhoneだけで次の実装内容を固定し、思いつきで終わらせないため。"},
    {"t": "今日の一次情報を1本選び要点を3行残す",
     "why": "読むだけで終えず、次の判断に再利用できる材料へ変えるため。"},
    {"t": "予測を1件確認し確度メモを1行更新する",
     "why": "新しい予測を増やす前に、過去の読みを現実で更新するため。"},
]


def action_problem(text):
    """iPhone・30分制約に反する行動なら理由、実行可能なら None。"""
    s = str(text or "").strip()
    if not s:
        return "行動が空"
    if len(s) > 80:
        return "一手として長すぎる"
    for pat, reason in _BLOCKED_ACTIONS:
        if pat.search(s):
            return reason
    if _PASSIVE_END.search(s) and not any(w in s for w in _OUTCOME_WORDS):
        return "成果物が残らない"
    return None


def safe_moves(moves, limit=3):
    """実行可能な候補だけを残し、足りなければ安全な既定案で補う。"""
    out, seen = [], set()
    for move in moves or []:
        if not isinstance(move, dict):
            continue
        title = str(move.get("t") or "").strip()
        if action_problem(title) or title in seen:
            continue
        seen.add(title)
        out.append({"t": title, "why": str(move.get("why") or "").strip()})
        if len(out) >= limit:
            return out
    for move in SAFE_FALLBACK_MOVES:
        if move["t"] not in seen:
            out.append(dict(move))
        if len(out) >= limit:
            break
    return out


def _claim_grams(text, n=3):
    s = re.sub(r"[\s、。,.・()（）「」『』\[\]【】]", "", str(text or "").lower())
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def claim_similarity(a, b):
    """日本語でも依存追加なしで使える文字3-gram Jaccard類似度。"""
    ga, gb = _claim_grams(a), _claim_grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def duplicate_claim(claim, hunches, threshold=0.58):
    """既存の判定待ち予測と実質同じなら、そのIDと類似度を返す。"""
    best = (None, 0.0)
    for h in hunches or []:
        if h.get("status") not in ("pending", "resolved"):
            continue
        sim = claim_similarity(claim, h.get("claim", ""))
        if sim > best[1]:
            best = (h.get("id"), sim)
    return best if best[1] >= threshold else (None, best[1])


def select_canonical_hunch(hunches, date_str):
    """その日の採点対象から、トップ画面に出す一つを選ぶ。

    実URL・基準率・監視指標を持つほど優先する。表示用に別の予測は作らない。
    """
    candidates = []
    for pos, h in enumerate(hunches or []):
        if h.get("status") != "pending" or h.get("resolved_at"):
            continue
        if str(h.get("created_at") or "")[:10] != date_str:
            continue
        res = h.get("resolution") if isinstance(h.get("resolution"), dict) else {}
        score = 0
        score += 4 if str(res.get("source") or "").startswith("https://") else 0
        score += 3 if isinstance(h.get("base_rate"), (int, float)) else 0
        score += 2 if h.get("indicators") else 0
        score += 1 if h.get("based_on") else 0
        candidates.append((score, pos, h))
    return max(candidates, default=(None, None, None), key=lambda x: (x[0], x[1]))[2]


def apply_canonical_hunch(final, hunch):
    """トップページの勘を採点台帳の同一オブジェクトへ差し替える。"""
    if not final or not hunch:
        return final
    out = dict(final)
    out["kan"] = str(hunch.get("claim") or "").strip()
    out["kan_konkyo"] = str(hunch.get("confidence_why") or hunch.get("prose") or "").strip()
    out["kan_hantai"] = str(hunch.get("counter") or "").strip()
    out["kan_conf"] = hunch.get("confidence")
    out["kan_id"] = hunch.get("id")
    out["kan_base_rate"] = hunch.get("base_rate")
    return out


def parse_feedback_issue(issue):
    """GitHub Issueを安全な行動結果イベントへ正規化する。"""
    if not isinstance(issue, dict) or not str(issue.get("title") or "").startswith("[一手フィードバック]"):
        return None
    body = str(issue.get("body") or "")
    vals = {}
    for line in body.splitlines():
        m = re.match(r"^(date|action|result|note):\s*(.*)$", line.strip())
        if m:
            vals[m.group(1)] = m.group(2).strip()[:300]
    if vals.get("result") not in ("done", "blocked", "skipped") or not vals.get("action"):
        return None
    return {"id": issue.get("number"), "date": vals.get("date", ""),
            "action": vals["action"], "result": vals["result"],
            "note": vals.get("note", ""), "url": issue.get("html_url", "")}


def fetch_action_feedback(repository, token=""):
    """公開Issueから行動結果を読む。失敗時は空配列で本体を止めない。"""
    if not repository:
        return []
    url = "https://api.github.com/repos/" + repository + "/issues?state=all&per_page=50"
    headers = {"User-Agent": "stick-koubou", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=12) as r:
            rows = json.loads(r.read().decode())
    except Exception as e:
        print("action feedback", repr(e)[:100])
        return []
    out = []
    owner = repository.split("/", 1)[0].strip().lower()
    for row in rows if isinstance(rows, list) else []:
        # 公開Issueは誰でも作れる。所有者本人の記録だけを学習材料にし、
        # 第三者のスパムやプロンプト注入を行動記憶へ混ぜない。
        author = str(((row.get("user") or {}).get("login") if isinstance(row, dict) else "") or "").lower()
        if not owner or author != owner:
            continue
        event = parse_feedback_issue(row)
        if event:
            out.append(event)
    return sorted(out, key=lambda x: (x.get("date", ""), x.get("id") or 0))[-50:]


def feedback_digest(events):
    """生の会話ではなく、行動と結果の構造化イベントだけを次の判断へ渡す。"""
    if not events:
        return ""
    done = sum(1 for e in events if e.get("result") == "done")
    judged = sum(1 for e in events if e.get("result") in ("done", "blocked", "skipped"))
    lines = ["【利用者の行動結果】完了%d/%d件。提案の実行可能性をこの結果から更新せよ。" % (done, judged)]
    for e in events[-6:]:
        lines.append("- %s [%s] %s%s" % (e.get("date", ""), e.get("result", ""),
                                         e.get("action", ""),
                                         (" / " + e["note"]) if e.get("note") else ""))
    lines.append("blocked/skipped と同じ種類の提案を、理由を解消せずに繰り返すな。")
    return "\n".join(lines)
