(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.StickDecision = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var CATEGORIES = ["build", "publish", "sell", "buy", "research", "review", "apply", "learn", "other"];
  var STAGE_AFFINITY = {
    concept: {research:1.3, review:1.2, learn:1.15, build:.85, publish:.7, sell:.65, buy:.75},
    build: {build:1.3, review:1.15, learn:1.08, buy:1.02, publish:.9, research:.85, sell:.7},
    publish: {publish:1.35, build:1.12, review:1.08, buy:.95, research:.85, sell:.9},
    acquire: {sell:1.3, publish:1.2, research:1.12, apply:1.1, review:1.02, build:.8},
    revenue: {sell:1.35, publish:1.15, review:1.08, buy:1.05, research:.9, build:.85}
  };
  var OUTCOME_AFFINITY = {
    complete: {build:1.18, publish:1.1, review:1.05},
    validate: {research:1.2, review:1.18, learn:1.08},
    publish: {publish:1.25, build:1.08, review:1.05},
    sell: {sell:1.25, apply:1.15, publish:1.1, research:1.05}
  };
  var BOTTLENECK_AFFINITY = {
    technical: {review:1.14, learn:1.12, buy:1.08, build:.92},
    trust: {publish:1.18, review:1.08, sell:.9},
    customer: {sell:1.22, research:1.15, publish:1.08, build:.85},
    time: {},
    cost: {}
  };
  var FIT = {none:0, low:.65, normal:1, high:1.35};

  function num(v, d) { v = +v; return isFinite(v) ? v : d; }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function normalizeState(raw) {
    var s = raw && typeof raw === "object" ? raw : {};
    return {
      stage: ["concept","build","publish","acquire","revenue"].indexOf(s.stage) >= 0 ? s.stage : "build",
      bottleneck: ["technical","trust","customer","time","cost"].indexOf(s.bottleneck) >= 0 ? s.bottleneck : "time",
      outcome: ["complete","validate","publish","sell"].indexOf(s.outcome) >= 0 ? s.outcome : "complete",
      minutes: clamp(Math.round(num(s.minutes, 30)), 10, 60),
      risk: ["cautious","balanced","aggressive"].indexOf(s.risk) >= 0 ? s.risk : "balanced",
      configured: !!s.configured
    };
  }

  function normalizeMove(raw) {
    var m = raw && typeof raw === "object" ? raw : {};
    var lo = Math.max(0, Math.round(num(m.cost_min, 0)));
    var hi = Math.max(lo, Math.round(num(m.cost_max, lo)));
    var p = clamp(num(m.success_p, .5), .05, .95);
    var pLo = clamp(num(m.success_p_min, p - .15), .05, p);
    var pHi = clamp(num(m.success_p_max, p + .15), p, .95);
    var iLo = clamp(num(m.impact_min, 0), 0, 1);
    var iHi = clamp(num(m.impact_max, iLo), iLo, 1);
    var category = String(m.category || "other").toLowerCase();
    if (CATEGORIES.indexOf(category) < 0) category = "other";
    return {
      t:String(m.t || ""), why:String(m.why || ""), category:category, terminal:!!m.terminal,
      cost_min:lo, cost_max:hi, loss_max:Math.max(0, Math.round(num(m.loss_max, hi))),
      success_p:p, success_p_min:pLo, success_p_max:pHi,
      success_why:String(m.success_why || "根拠未記入"),
      value_score:clamp(Math.round(num(m.value_score, 3)), 1, 5),
      learning_value:clamp(Math.round(num(m.learning_value, 2)), 0, 5),
      time_minutes:clamp(Math.round(num(m.time_minutes, 30)), 1, 180),
      payback_days:clamp(Math.round(num(m.payback_days, 0)), 0, 3650),
      impact_min:iLo, impact_max:iHi, impact_why:String(m.impact_why || ""),
      evidence_ids:Array.isArray(m.evidence_ids) ? m.evidence_ids.slice(0, 5).map(String) : [],
      assumptions:Array.isArray(m.assumptions) ? m.assumptions.slice(0, 4).map(String) : [],
      disconfirm:String(m.disconfirm || ""), outcome:String(m.outcome || ""),
      continue_if:String(m.continue_if || ""), stop:String(m.stop || ""),
      // 既定案(参謀が今日考えた案ではない)の印。ホワイトリストなので明示しないと落ちる
      fallback:!!m.fallback
    };
  }

  function stateAffinity(move, state, fit) {
    var m = normalizeMove(move), s = normalizeState(state);
    var stage = (STAGE_AFFINITY[s.stage] || {})[m.category] || 1;
    var outcome = (OUTCOME_AFFINITY[s.outcome] || {})[m.category] || 1;
    var bottleneck = (BOTTLENECK_AFFINITY[s.bottleneck] || {})[m.category] || 1;
    if (s.bottleneck === "cost") {
      bottleneck = m.cost_max === 0 ? 1.18 : m.cost_max <= 1000 ? 1.05 : .75;
    }
    if (s.bottleneck === "time") {
      bottleneck = clamp(s.minutes / Math.max(1, m.time_minutes), .65, 1.18);
    }
    return clamp(stage * outcome * bottleneck * (FIT[fit] == null ? 1 : FIT[fit]), 0, 1.65);
  }

  function localStats(days, category, stage) {
    var exact = {n:0, positive:0}, broad = {n:0, positive:0};
    Object.keys(days || {}).sort().slice(-60).forEach(function (date) {
      var d = days[date] || {}, meta = d.move_meta || {};
      if (meta.category !== category || (d.goal_advanced !== "yes" && d.goal_advanced !== "no")) return;
      broad.n++; if (d.goal_advanced === "yes") broad.positive++;
      if ((meta.state_snapshot || {}).stage === stage) {
        exact.n++; if (d.goal_advanced === "yes") exact.positive++;
      }
    });
    var g = exact.n ? exact : broad;
    return {n:g.n, positive:g.positive, posterior:(g.positive + 1) / (g.n + 2), exact:!!exact.n};
  }

  function adjustMove(raw, state, days, fit) {
    var m = normalizeMove(raw), s = normalizeState(state);
    var affinity = stateAffinity(m, s, fit);
    var stats = localStats(days || {}, m.category, s.stage);
    var weight = Math.min(.5, stats.n / 10);
    var p = (1 - weight) * m.success_p + weight * stats.posterior;
    var width = Math.max(.1, (m.success_p_max - m.success_p_min) * (1 - weight / 2));
    m.success_p = clamp(p, .05, .95);
    m.success_p_min = clamp(m.success_p - width / 2, .05, m.success_p);
    m.success_p_max = clamp(m.success_p + width / 2, m.success_p, .95);
    m.impact_min = clamp(m.impact_min * affinity, 0, m.terminal ? 1 : .3);
    m.impact_max = clamp(m.impact_max * affinity, m.impact_min, m.terminal ? 1 : .3);
    m.state_affinity = affinity;
    m.local_calibration = stats;
    return m;
  }

  function expectedValue(move, goalValue) {
    var m = normalizeMove(move), goal = Math.max(0, Math.round(num(goalValue, 0)));
    var extra = Math.max(0, m.loss_max - m.cost_max);
    var low = m.success_p_min * m.impact_min * goal - m.cost_max - (1 - m.success_p_min) * extra;
    var high = m.success_p_max * m.impact_max * goal - m.cost_min;
    return {min:Math.round(low), max:Math.round(high), mid:Math.round((low + high) / 2)};
  }

  function budgetProblem(move, budget, remaining, state) {
    var m = normalizeMove(move), b = budget || {}, s = normalizeState(state);
    if (m.time_minutes > s.minutes) return "今日の時間" + s.minutes + "分を超える";
    if (m.cost_max > Math.max(0, num(remaining, 0))) return "残額を超える";
    if (m.cost_max > Math.max(0, num(b.per_action_yen, 0))) return "1回の上限を超える";
    if (m.loss_max > Math.max(0, num(b.risk_limit_yen, 0))) return "許容損失を超える";
    // 費用が残額内でも、失敗時の最大損失が残額を超えるなら払い切れない。
    // 「最悪の場合いくら消えるか」で見ないと、残額ちょうどの案が無傷に見える。
    // 許容損失より後に置く: 両方に触れる案は、利用者が明示した上限の方を理由として返す。
    if (m.loss_max > Math.max(0, num(remaining, 0))) return "最大損失が残額を超える";
    if (m.cost_max > 0 && !m.stop) return "撤退条件がない";
    if (m.cost_max > 0 && !m.continue_if) return "続行条件がない";
    return "";
  }

  function fallbackScore(m, budget, remaining, riskMode) {
    var rem = Math.max(1, num(remaining, 1));
    var risk = Math.max(1, num((budget || {}).risk_limit_yen, num((budget || {}).total_yen, 1)));
    var horizon = Math.max(30, num((budget || {}).period_months, 6) * 30);
    var costWeight = riskMode === "cautious" ? 2.6 : riskMode === "aggressive" ? 1.2 : 2;
    var lossWeight = riskMode === "cautious" ? 1.2 : riskMode === "aggressive" ? .4 : .8;
    return (m.value_score * m.success_p + .35 * m.learning_value) * m.state_affinity
      - costWeight * (m.cost_max / rem) - lossWeight * (m.loss_max / risk)
      - .2 * (m.time_minutes / 30) - .4 * Math.min(2, m.payback_days / horizon);
  }

  function riskScore(ev, risk) {
    if (risk === "cautious") return ev.min;
    if (risk === "aggressive") return Math.round(.7 * ev.max + .3 * ev.mid);
    return ev.mid;
  }

  function rankMoves(moves, opts) {
    opts = opts || {};
    var s = normalizeState(opts.state), goal = Math.max(0, num(opts.goalValue, 0));
    var fits = opts.fitOverrides || {}, rem = num(opts.remaining, 0), budget = opts.budget || {};
    var ranked = (moves || []).map(function (raw, index) {
      var title = String((raw || {}).t || "");
      var m = adjustMove(raw, s, opts.days || {}, fits[title] || "normal");
      var ev = expectedValue(m, goal);
      var problem = String(opts.globalProblem || "") || budgetProblem(m, budget, rem, s);
      var score = goal > 0 ? riskScore(ev, s.risk) : fallbackScore(m, budget, rem, s.risk);
      return {move:m, index:index, ev:ev, problem:problem, score:score, fit:fits[title] || "normal"};
    });
    ranked.sort(function (a, b) {
      if (!!a.problem !== !!b.problem) return a.problem ? 1 : -1;
      if (b.score !== a.score) return b.score - a.score;
      return a.index - b.index;
    });
    return ranked;
  }

  function daysBetween(a, b) {
    var x = new Date(String(a) + "T00:00:00Z"), y = new Date(String(b) + "T00:00:00Z");
    return isFinite(x.getTime()) && isFinite(y.getTime()) ? Math.floor((y - x) / 86400000) : 0;
  }

  // 材料がこの日数以上古ければ「今日の判断材料」として扱わない。推薦の見送り判定と
  // 画面の鮮度表示が別々の値を持つとズレるので、ここを唯一の定義にする。
  var STALE_DAYS = 4;

  function recommendation(ranked, opts) {
    opts = opts || {};
    var valid = (ranked || []).filter(function (x) { return !x.problem; });
    if (opts.dataDate && opts.today && daysBetween(opts.dataDate, opts.today) >= STALE_DAYS) {
      return {abstain:true, reason:"材料が" + STALE_DAYS + "日以上古い。参謀を更新してから決める", top:valid[0] || null};
    }
    if (!valid.length) return {abstain:true, reason:"今日の時間・予算・損失条件を満たす案がない", top:null};
    var top = valid[0], goal = Math.max(0, num(opts.goalValue, 0));
    if (goal > 0 && top.ev.max <= 0) {
      return {abstain:true, reason:"最良案でも期待値の上限が0円以下。今日は投資しない", top:top};
    }
    if (goal > 0 && top.score < 0 && normalizeState(opts.state).risk !== "aggressive") {
      return {abstain:true, reason:"現在のリスク方針では最良案も割に合わない。条件を見直す", top:top};
    }
    return {abstain:false, reason:"", top:top};
  }

  /* ---- 実演算: この選び方を続けたらどこへ着地するか ----------------------
     作戦履歴が空の間、画面は「まだ記録がありません」しか言えなかった。だが参謀は
     既に数字を持っている(成功確率の幅・目標寄与の幅・費用・最大損失・残額・目標価値)。
     それを実際に回して着地の分布を出す。占いではなく、参謀自身の見積りの帰結。
     見積りが甘ければ結果も甘く出る——それも含めて「参謀の読みの姿」を見せる。 */

  // 決定的な擬似乱数(mulberry32)。テストで再現でき、同じ入力なら毎回同じ絵になる。
  function rng(seed) {
    var a = (seed >>> 0) || 1;
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      var t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  function quantile(sorted, q) {
    if (!sorted.length) return 0;
    var i = (sorted.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i);
    return lo === hi ? sorted[lo] : sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
  }

  // 同じ手を繰り返すほど1回あたりの寄与は落ちる(2回目以降の逓減)。
  // これが無いと「同じ一手を90日続ければ必ず目標達成」という嘘の絵になる。
  var REPEAT_DECAY = 0.62;

  function simulate(moves, opts) {
    var o = opts || {}, days = clamp(Math.round(num(o.days, 90)), 1, 365);
    var trials = clamp(Math.round(num(o.trials, 600)), 50, 5000);
    var budget = o.budget || {}, random = o.random || rng(num(o.seed, 20260806));
    var state = normalizeState(o.state);
    // 憲法2章「シミュレーションにも、実際の残額、1回上限、許容損失、今日の時間を適用する」。
    // 推薦では弾くのに試算では通す、という二枚舌をやめる。ここで落ちる候補は
    // **そもそも実行できない**ので、最初から母集団に入れない。
    var riskLimit = Math.max(0, num(budget.risk_limit_yen, 0));
    var perAction = Math.max(0, num(budget.per_action_yen, 0));
    var minutes = Math.max(1, num(state.minutes, 30));
    var rest = Math.max(0, num(budget.total_yen, 0)) - Math.max(0, num(budget.spent_yen, 0));
    var given = (moves || []).map(normalizeMove).filter(function (m) { return !!m.t; });
    // そもそも材料が無い場合と、材料はあるが方針で全部落ちた場合は別物として扱う。
    // 前者は「試算する対象がない」= null。後者は「今日は1つも実行できない」という**結果**
    // なので、0円・0進捗として返し、画面が理由を言えるようにする。
    if (!given.length) return null;
    var pool = given.filter(function (m) {
      // 上限0は「無制限」ではなく「1円も出せない」。riskLimit && / !perAction || のような
      // 短絡を書くと 0 が素通りし、**利用者が最も強く締めた設定が最も緩く効く**という
      // 逆転が起きる。0 を素直に比較する。
      if (m.loss_max > riskLimit) return false;                // 許容損失を超える賭けはしない
      if (m.loss_max > Math.max(0, rest)) return false;        // 最大損失が残額を食い潰す
      // 1回上限は日ごとに変わらない方針なので、母集団の時点で落とす。日ごとの絞り込みに
      // だけ置くと「実行できないのに候補として数えられる」(eligible が水増しされる)。
      if (m.cost_max > perAction) return false;                // 1回上限0円なら有料案は選べない
      if (m.time_minutes > minutes) return false;              // 今日の可処分時間で終わらない
      return true;
    });
    if (!pool.length) {
      return {days: days, trials: trials, eligible: 0, excluded: given.length,
              progress: {p10: 0, p50: 0, p90: 0}, spend: {p50: 0, p90: 0},
              reach_p: 0, reach_day_p50: null};
    }
    var total = Math.max(0, num(budget.total_yen, 0)) - Math.max(0, num(budget.spent_yen, 0));
    var perAction = Math.max(0, num(budget.per_action_yen, 0));
    var progress = [], spend = [], reachDay = [], reached = 0;

    for (var t = 0; t < trials; t++) {
      var done = 0, spent = 0, used = {}, hitDay = 0;
      for (var d = 1; d <= days; d++) {
        // 残額は日々減る。初日の残額で一度判定して終わりにすると、失敗が重なった後も
        // 同じ賭けを打ち続け、**支出が残額を超える**(実測: 残額1000円に対し1100円)。
        // 毎日「今いくら残っているか」で費用と最大損失の両方を見直す。
        var left = Math.max(0, total - spent);
        var afford = pool.filter(function (m) {
          return m.cost_max <= left && m.loss_max <= left;
        });
        if (!afford.length) break;
        var m = afford[Math.floor(random() * afford.length) % afford.length];
        var cost = m.cost_min + random() * Math.max(0, m.cost_max - m.cost_min);
        spent += cost;
        var p = m.success_p_min + random() * Math.max(0, m.success_p_max - m.success_p_min);
        if (random() < p) {
          var impact = m.impact_min + random() * Math.max(0, m.impact_max - m.impact_min);
          done += impact * Math.pow(REPEAT_DECAY, used[m.t] || 0);
          used[m.t] = (used[m.t] || 0) + 1;
        } else if (m.loss_max > m.cost_max) {
          spent += (m.loss_max - m.cost_max) * 0.5;   // 失敗時の追加損失(期待値ぶん)
        }
        if (done >= 1 && !hitDay) { hitDay = d; break; }
      }
      progress.push(Math.min(1, done));
      spend.push(Math.round(spent));
      if (hitDay) { reached++; reachDay.push(hitDay); }
    }
    progress.sort(function (a, b) { return a - b; });
    spend.sort(function (a, b) { return a - b; });
    reachDay.sort(function (a, b) { return a - b; });
    return {
      days: days, trials: trials, eligible: pool.length, excluded: given.length - pool.length,
      progress: {p10: quantile(progress, .1), p50: quantile(progress, .5), p90: quantile(progress, .9)},
      spend: {p50: Math.round(quantile(spend, .5)), p90: Math.round(quantile(spend, .9))},
      reach_p: reached / trials,
      reach_day_p50: reachDay.length ? Math.round(quantile(reachDay, .5)) : null
    };
  }

  return {
    normalizeState:normalizeState, normalizeMove:normalizeMove, stateAffinity:stateAffinity,
    localStats:localStats, adjustMove:adjustMove, expectedValue:expectedValue,
    budgetProblem:budgetProblem, rankMoves:rankMoves, recommendation:recommendation,
    simulate:simulate, rng:rng, STALE_DAYS:STALE_DAYS
  };
});
