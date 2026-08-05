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
    "budget": "利用者が設定する総予算・1回上限・許容損失の中で期待値を最大化する",
    "working_style": "朝か夜に、一度に一つだけ進める",
    "goal": "AIを使ってWebアプリや小さな収益実験を完成させる",
}

PROFILE_PROMPT = """【実行環境・絶対条件】
- 使える端末はiPhoneだけ。Safari、ChatGPT、GitHubのWeb画面で完了する行動に限る
- 1日の作業は30分以内。一度に一つだけ進める
- 無料を優先条件にするな。利用者の予算帯の中で、成果・学習価値・成功見込みが費用に勝る案を選べ
- 有料案には費用の下限/上限、得る成果、成功見込みと根拠、回収目安、最大損失、続行条件、撤退条件を必ず付ける
- 正確な予算が不明なら、無料・小額・標準・積極の複数案を作り、画面側の残額判定に委ねる
- PC、GPU、ローカル開発環境、CLI、Docker、Python実行を要求するな
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

ACTION_CATEGORIES = ("build", "publish", "sell", "buy", "research", "review", "apply", "learn", "other")
SPECIAL_EVIDENCE_IDS = {"user-goal", "action-history"}

SAFE_FALLBACK_MOVES = [
    {"t": "GitHubの改善候補を1件Issueに書く",
     "why": "iPhoneだけで次の実装内容を固定し、思いつきで終わらせないため。",
     "cost_min": 0, "cost_max": 0, "loss_max": 0, "success_p": 0.8,
     "value_score": 3, "learning_value": 2, "time_minutes": 15,
     "success_why": "作成画面までiPhoneだけで完結し、成果物が明確",
     "payback_days": 0, "outcome": "次に直す内容がIssueとして1件残る",
     "category": "build", "success_p_min": 0.7, "success_p_max": 0.9,
     "impact_min": 0.01, "impact_max": 0.04, "impact_why": "次の実装を固定して停滞を減らす",
     "evidence_ids": ["user-goal", "action-history"], "assumptions": ["Issueが次の実装に使われる"],
     "disconfirm": "Issueを作っても7日以内に実装へ進まない",
     "continue_if": "実装対象と完了条件を1文で書けた", "stop": "Issueを1件作成したら終了"},
    {"t": "今日の一次情報を1本選び要点を3行残す",
     "why": "読むだけで終えず、次の判断に再利用できる材料へ変えるため。",
     "cost_min": 0, "cost_max": 0, "loss_max": 0, "success_p": 0.75,
     "value_score": 2, "learning_value": 3, "time_minutes": 20,
     "success_why": "読む対象を1本に固定すれば30分内に収まる",
     "payback_days": 0, "outcome": "一次情報の要点メモが3行残る",
     "category": "research", "success_p_min": 0.6, "success_p_max": 0.85,
     "impact_min": 0.0, "impact_max": 0.02, "impact_why": "意思決定へ使えた時だけ価値になる",
     "evidence_ids": ["user-goal", "action-history"], "assumptions": ["読む情報が現在の目標に関係する"],
     "disconfirm": "要点が次の行動を一つも変えない",
     "continue_if": "次の判断に使える差分が1つ見つかった", "stop": "3行書いたら終了"},
    {"t": "予測を1件確認し確度メモを1行更新する",
     "why": "新しい予測を増やす前に、過去の読みを現実で更新するため。",
     "cost_min": 0, "cost_max": 0, "loss_max": 0, "success_p": 0.7,
     "value_score": 2, "learning_value": 4, "time_minutes": 20,
     "success_why": "既存予測の更新なので新規企画より着地しやすい",
     "payback_days": 0, "outcome": "追跡中予測の確度メモが更新される",
     "category": "review", "success_p_min": 0.55, "success_p_max": 0.8,
     "impact_min": 0.0, "impact_max": 0.02, "impact_why": "誤った前提で投資する損失を減らす",
     "evidence_ids": ["action-history"], "assumptions": ["予測が現在の投資判断に関係する"],
     "disconfirm": "確度更新が次の行動や支出を変えない",
     "continue_if": "確度を動かす一次根拠が見つかった", "stop": "根拠URLを1本確認したら終了"},
    {"t": "独自ドメイン候補を決め購入画面まで進める",
     "why": "公開先を固定し、他人へ渡せる形へ近づけるため。",
     "cost_min": 1000, "cost_max": 4000, "loss_max": 4000, "success_p": 0.65,
     "value_score": 4, "learning_value": 2, "time_minutes": 30,
     "success_why": "価格と空きを確認できるが、名前の決定で迷う余地がある", "payback_days": 180,
     "outcome": "候補ドメインと年間費用が確定する", "category": "buy",
     "success_p_min": 0.45, "success_p_max": 0.75, "impact_min": 0.02, "impact_max": 0.08,
     "impact_why": "公開と共有の摩擦を下げるが、ドメインだけでは成果にならない",
     "evidence_ids": ["user-goal", "action-history"], "assumptions": ["90日以内に公開する"],
     "disconfirm": "公開日や見せる相手が決まっていない",
     "continue_if": "公開予定日と最初に見せる相手が決まっている", "stop": "年間4000円を超えたら購入しない"},
    {"t": "有料AI機能を1か月だけ比較検証する",
     "why": "無料制約で実装速度を落とさず、支払う価値を測るため。",
     "cost_min": 3000, "cost_max": 5000, "loss_max": 5000, "success_p": 0.55,
     "value_score": 4, "learning_value": 5, "time_minutes": 30,
     "success_why": "短縮効果はあり得るが、成果との相関は未計測", "payback_days": 30,
     "outcome": "無料版との比較メモが1件残る", "category": "buy",
     "success_p_min": 0.35, "success_p_max": 0.7, "impact_min": 0.02, "impact_max": 0.12,
     "impact_why": "実装速度を上げる可能性はあるが成果増加は未確認",
     "evidence_ids": ["user-goal", "action-history"], "assumptions": ["週2回以上使う"],
     "disconfirm": "無料版と完成件数が変わらない",
     "continue_if": "無料版より月2件以上多く成果物が完成する", "stop": "30日で成果物が2件増えなければ解約する"},
]

DEFAULT_BUDGET = {
    "total_yen": 10000,
    "period_months": 6,
    "per_action_yen": 5000,
    "risk_limit_yen": 5000,
    "spent_yen": 0,
}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def normalize_budget(budget=None):
    """予算設定を安全な非負値へ正規化する。"""
    src = dict(DEFAULT_BUDGET)
    if isinstance(budget, dict):
        src.update(budget)
    total = max(0, round(_number(src.get("total_yen"))))
    return {
        "total_yen": total,
        "period_months": max(1, min(24, round(_number(src.get("period_months"), 6)))),
        "per_action_yen": max(0, round(_number(src.get("per_action_yen")))),
        "risk_limit_yen": max(0, round(_number(src.get("risk_limit_yen")))),
        "spent_yen": max(0, round(_number(src.get("spent_yen")))),
    }


def normalize_move(move):
    """LLMの行動案を、予算判定できる固定スキーマへ変換する。"""
    m = move if isinstance(move, dict) else {}
    lo = max(0, round(_number(m.get("cost_min"))))
    hi = max(lo, round(_number(m.get("cost_max"), lo)))
    success = max(0.05, min(0.95, _number(m.get("success_p"), 0.5)))
    success_lo = max(0.05, min(success, _number(m.get("success_p_min"), success - 0.15)))
    success_hi = max(success, min(0.95, _number(m.get("success_p_max"), success + 0.15)))
    impact_lo = max(0.0, min(1.0, _number(m.get("impact_min"), 0.0)))
    impact_hi = max(impact_lo, min(1.0, _number(m.get("impact_max"), impact_lo)))
    evidence = m.get("evidence_ids") if isinstance(m.get("evidence_ids"), list) else []
    assumptions = m.get("assumptions") if isinstance(m.get("assumptions"), list) else []
    category = str(m.get("category") or "other").strip().lower()
    return {
        "t": str(m.get("t") or "").strip(),
        "why": str(m.get("why") or "").strip(),
        "cost_min": lo,
        "cost_max": hi,
        "loss_max": max(0, round(_number(m.get("loss_max"), hi))),
        "success_p": success,
        "success_p_min": success_lo,
        "success_p_max": success_hi,
        "success_why": str(m.get("success_why") or "根拠未記入").strip(),
        "value_score": max(1, min(5, round(_number(m.get("value_score"), 3)))),
        "learning_value": max(0, min(5, round(_number(m.get("learning_value"), 2)))),
        "time_minutes": max(1, min(180, round(_number(m.get("time_minutes"), 30)))),
        "payback_days": max(0, min(3650, round(_number(m.get("payback_days"), 0)))),
        "outcome": str(m.get("outcome") or "").strip(),
        "category": category if category in ACTION_CATEGORIES else "other",
        "terminal": bool(m.get("terminal", False)),
        "impact_min": impact_lo,
        "impact_max": impact_hi,
        "impact_why": str(m.get("impact_why") or "").strip(),
        "evidence_ids": list(dict.fromkeys(str(x).strip() for x in evidence if str(x).strip()))[:5],
        "assumptions": [str(x).strip()[:160] for x in assumptions if str(x).strip()][:4],
        "disconfirm": str(m.get("disconfirm") or "").strip(),
        "continue_if": str(m.get("continue_if") or "").strip(),
        "stop": str(m.get("stop") or "").strip(),
    }


def move_quality_problem(move, valid_evidence_ids=None, trusted_evidence_ids=None):
    """もっともらしいだけの案を、証拠・反証・価値仮説の固定条件で落とす。"""
    m = normalize_move(move)
    if not m["outcome"]:
        return "観測可能な成果がない"
    if not m["impact_why"]:
        return "目標への寄与根拠がない"
    if not m["disconfirm"]:
        return "価値仮説の反証条件がない"
    if m["impact_max"] > 0.3 and not m["terminal"]:
        return "30分の一手として目標寄与を過大評価している"
    if m["cost_max"] > 0 and not m["assumptions"]:
        return "有料案の前提条件がない"
    if (m["cost_max"] > 0 or m["success_p"] >= 0.75) and not m["evidence_ids"]:
        return "有料・高確度案に証拠IDがない"
    if valid_evidence_ids is not None:
        allowed = set(valid_evidence_ids) | SPECIAL_EVIDENCE_IDS
        if any(e not in allowed for e in m["evidence_ids"]):
            return "存在しない証拠IDを参照している"
    if m["cost_max"] > 0 or m["success_p"] >= 0.75:
        informative = set(m["evidence_ids"]) - {"user-goal"}
        # action-history は呼び出し側が実際に履歴を取得できた時だけ trusted に入れる。
        # 名前だけ書けば経験済みに見える抜け道を作らない。
        trusted = set(trusted_evidence_ids) if trusted_evidence_ids is not None else informative
        if not informative & trusted:
            return "有料・高確度案に確認済み証拠がない"
    return None


def expected_value_range(move, goal_value_yen):
    """成功確率幅×目標寄与幅から、費用と失敗損失を引いた円の期待値幅を返す。"""
    m = normalize_move(move)
    goal = max(0, round(_number(goal_value_yen)))
    extra_failure_loss = max(0, m["loss_max"] - m["cost_max"])
    low = (m["success_p_min"] * m["impact_min"] * goal - m["cost_max"]
           - (1 - m["success_p_min"]) * extra_failure_loss)
    high = m["success_p_max"] * m["impact_max"] * goal - m["cost_min"]
    midpoint = (low + high) / 2
    return {"min_yen": round(low), "max_yen": round(high), "mid_yen": round(midpoint)}


def budget_problem(move, budget=None):
    """残額・1回上限・許容損失に反する案なら理由を返す。"""
    m, b = normalize_move(move), normalize_budget(budget)
    remaining = max(0, b["total_yen"] - b["spent_yen"])
    if m["cost_max"] > remaining:
        return "残額を超える"
    if m["cost_max"] > b["per_action_yen"]:
        return "1回の上限を超える"
    if m["loss_max"] > b["risk_limit_yen"]:
        return "許容損失を超える"
    if m["cost_max"] > 0 and not m["stop"]:
        return "有料案に撤退条件がない"
    if m["cost_max"] > 0 and not m["continue_if"]:
        return "有料案に続行条件がない"
    return None


def move_score(move, budget=None):
    """費用だけでなく成果見込みと学習価値を含む、説明可能な比較スコア。"""
    m, b = normalize_move(move), normalize_budget(budget)
    remaining = max(1, b["total_yen"] - b["spent_yen"])
    risk_base = max(1, b["risk_limit_yen"] or b["total_yen"] or 1)
    benefit = m["value_score"] * m["success_p"] + 0.35 * m["learning_value"]
    cost_pressure = 2.0 * (m["cost_max"] / remaining)
    risk_pressure = 0.8 * (m["loss_max"] / risk_base)
    time_pressure = 0.2 * (m["time_minutes"] / max(1, PROFILE["daily_minutes"]))
    horizon_days = max(30, b["period_months"] * 30)
    payback_pressure = 0.4 * min(2, m["payback_days"] / horizon_days)
    return round(benefit - cost_pressure - risk_pressure - time_pressure - payback_pressure, 3)


def rank_moves(moves, budget=None, limit=5, goal_value_yen=0, valid_evidence_ids=None,
               trusted_evidence_ids=None):
    """実行可能な案だけを、予算内の限界価値順に並べる。"""
    ranked = []
    for pos, raw in enumerate(moves or []):
        m = normalize_move(raw)
        if (action_problem(m["t"]) or budget_problem(m, budget)
                or move_quality_problem(m, valid_evidence_ids, trusted_evidence_ids)):
            continue
        ev = expected_value_range(m, goal_value_yen)
        score = ev["mid_yen"] if _number(goal_value_yen) > 0 else move_score(m, budget)
        ranked.append((score, -pos, dict(m, expected_value=ev)))
    ranked.sort(reverse=True, key=lambda x: (x[0], x[1]))
    return [dict(m, decision_score=score) for score, _, m in ranked[:limit]]


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


def safe_moves(moves, limit=3, valid_evidence_ids=None, trusted_evidence_ids=None,
               strict_quality=False):
    """実行可能な候補だけを残し、足りなければ安全な既定案で補う。"""
    out, seen = [], set()
    for move in moves or []:
        if not isinstance(move, dict):
            continue
        item = normalize_move(move)
        title = item["t"]
        if (action_problem(title) or title in seen
                or (strict_quality and move_quality_problem(
                    item, valid_evidence_ids, trusted_evidence_ids))):
            continue
        seen.add(title)
        out.append(item)
        if len(out) >= limit:
            return out
    for move in SAFE_FALLBACK_MOVES:
        if move["t"] not in seen:
            out.append(normalize_move(move))
        if len(out) >= limit:
            break
    return out


def action_category_stats(events):
    """選んだ行動だけ観測できる少標本を、Beta(1,1)で過信せず集計する。"""
    groups = {}
    for e in events or []:
        if e.get("kind", "action") != "action":
            continue
        cat = str(e.get("category") or "other")
        if cat not in ACTION_CATEGORIES:
            cat = "other"
        g = groups.setdefault(cat, {"n": 0, "positive": 0, "done": 0, "blocked": 0})
        g["n"] += 1
        if e.get("result") == "done":
            g["done"] += 1
        if e.get("result") == "blocked":
            g["blocked"] += 1
        advanced = e.get("goal_advanced")
        if advanced == "yes" or (advanced not in ("yes", "no") and e.get("result") == "done"):
            g["positive"] += 1
    for g in groups.values():
        g["posterior_p"] = round((g["positive"] + 1) / (g["n"] + 2), 3)
    return groups


def calibrate_moves(moves, events):
    """行動履歴を最大50%だけ混ぜ、少数結果でモデル推定を反転させない。"""
    stats = action_category_stats(events)
    out = []
    for raw in moves or []:
        m = normalize_move(raw)
        g = stats.get(m["category"])
        if g and g["n"]:
            weight = min(0.5, g["n"] / 10)
            p = (1 - weight) * m["success_p"] + weight * g["posterior_p"]
            width = max(0.1, (m["success_p_max"] - m["success_p_min"]) * (1 - weight / 2))
            m["success_p"] = round(max(0.05, min(0.95, p)), 3)
            m["success_p_min"] = round(max(0.05, m["success_p"] - width / 2), 3)
            m["success_p_max"] = round(min(0.95, m["success_p"] + width / 2), 3)
            m["success_why"] += " / 行動履歴%s n=%dで弱く較正" % (m["category"], g["n"])
        out.append(m)
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
        m = re.match(r"^(date|action|result|note|category|goal_advanced|budget_band|planned_cost_band|actual_cost_band|actual_value_band):\s*(.*)$",
                     line.strip())
        if m:
            vals[m.group(1)] = m.group(2).strip()[:300]
    if vals.get("result") not in ("done", "blocked", "skipped") or not vals.get("action"):
        return None
    return {"kind": "action", "id": issue.get("number"), "date": vals.get("date", ""),
            "action": vals["action"], "result": vals["result"],
            "category": vals.get("category", "other") if vals.get("category") in ACTION_CATEGORIES else "other",
            "goal_advanced": vals.get("goal_advanced", "unknown")
            if vals.get("goal_advanced") in ("yes", "no", "unknown") else "unknown",
            "note": vals.get("note", ""), "budget_band": vals.get("budget_band", ""),
            "planned_cost_band": vals.get("planned_cost_band", ""),
            "actual_cost_band": vals.get("actual_cost_band", ""),
            "actual_value_band": vals.get("actual_value_band", ""),
            "url": issue.get("html_url", "")}


def parse_budget_issue(issue):
    """正確な金額を保存せず、利用者が共有した予算帯だけを読む。"""
    if not isinstance(issue, dict) or not str(issue.get("title") or "").startswith("[参謀設定] 予算帯"):
        return None
    vals = {}
    for line in str(issue.get("body") or "").splitlines():
        m = re.match(r"^(date|budget_band|period_months|per_action_band|risk_band|goal_value_band|goal_deadline_band):\s*(.*)$", line.strip())
        if m:
            vals[m.group(1)] = m.group(2).strip()[:100]
    allowed = ("small", "standard", "expanded", "active")
    if vals.get("budget_band") not in allowed:
        return None
    return {"kind": "budget", "id": issue.get("number"), "date": vals.get("date", ""),
            "budget_band": vals["budget_band"], "period_months": vals.get("period_months", ""),
            "per_action_band": vals.get("per_action_band", ""),
            "risk_band": vals.get("risk_band", ""), "goal_value_band": vals.get("goal_value_band", ""),
            "goal_deadline_band": vals.get("goal_deadline_band", ""), "url": issue.get("html_url", "")}


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
        event = parse_feedback_issue(row) or parse_budget_issue(row)
        if event:
            out.append(event)
    return sorted(out, key=lambda x: (x.get("date", ""), x.get("id") or 0))[-50:]


def feedback_digest(events):
    """生の会話ではなく、行動と結果の構造化イベントだけを次の判断へ渡す。"""
    if not events:
        return ""
    actions = [e for e in events if e.get("kind", "action") == "action"]
    settings = [e for e in events if e.get("kind") == "budget"]
    done = sum(1 for e in actions if e.get("result") == "done")
    judged = sum(1 for e in actions if e.get("result") in ("done", "blocked", "skipped"))
    lines = ["【利用者の行動結果】完了%d/%d件。提案の実行可能性をこの結果から更新せよ。" % (done, judged)]
    if settings:
        b = settings[-1]
        lines.append("【予算帯】%s / 期間%sか月 / 1回%s / 許容損失%s。正確な金額は非公開。"
                     % (b.get("budget_band", ""), b.get("period_months", ""),
                        b.get("per_action_band", ""), b.get("risk_band", "")))
        if b.get("goal_value_band"):
            lines.append("【目標価値帯】%s / 期限%s。正確な金額と目標文は端末内のみ。"
                         % (b.get("goal_value_band", ""), b.get("goal_deadline_band", "")))
    stats = action_category_stats(actions)
    for cat, g in sorted(stats.items(), key=lambda x: x[1]["n"], reverse=True)[:4]:
        lines.append("行動型%s: 目標前進の事後推定%d%%(n=%d%s)。n<5は参考値。"
                     % (cat, round(g["posterior_p"] * 100), g["n"],
                        " / blocked %d" % g["blocked"] if g["blocked"] else ""))
    for e in actions[-6:]:
        lines.append("- %s [%s] %s%s" % (e.get("date", ""), e.get("result", ""),
                                         e.get("action", ""),
                                         (" / " + e["note"]) if e.get("note") else ""))
    lines.append("blocked/skipped と同じ種類の提案を、理由を解消せずに繰り返すな。")
    return "\n".join(lines)
