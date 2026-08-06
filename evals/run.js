"use strict";

const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const Engine = require(path.join(root, "docs", "decision-engine.js"));
const cases = JSON.parse(fs.readFileSync(path.join(__dirname, "cases.json"), "utf8"));
const budgetPolicy = JSON.parse(fs.readFileSync(path.join(__dirname, "budget-policy.json"), "utf8"));

function move(title, extra) {
  return Object.assign({
    t: title,
    why: "eval",
    category: "build",
    cost_min: 0,
    cost_max: 0,
    loss_max: 0,
    success_p: 0.7,
    success_p_min: 0.6,
    success_p_max: 0.8,
    success_why: "eval evidence",
    value_score: 3,
    learning_value: 2,
    time_minutes: 20,
    payback_days: 0,
    impact_min: 0.05,
    impact_max: 0.1,
    impact_why: "goal link",
    evidence_ids: ["user-goal"],
    assumptions: ["eval assumption"],
    disconfirm: "eval disconfirm",
    outcome: "artifact",
    continue_if: "works",
    stop: "stop"
  }, extra || {});
}

function options(extra) {
  return Object.assign({
    state: {stage: "build", bottleneck: "technical", outcome: "complete", minutes: 30, risk: "balanced", configured: true},
    days: {},
    fitOverrides: {},
    goalValue: 100000,
    budget: {total_yen: 10000, period_months: 6, per_action_yen: 5000, risk_limit_yen: 3000},
    remaining: 10000
  }, extra || {});
}

const fallbackTitles = new Set([
  "GitHubの改善候補を1件Issueに書く",
  "今日の一次情報を1本選び要点を3行残す",
  "予測を1件確認し確度メモを1行更新する",
  "独自ドメイン候補を決め購入画面まで進める",
  "有料AI機能を1か月だけ比較検証する"
]);

function check(condition, detail) {
  if (!condition) throw new Error(detail);
}

const checks = {
  "remaining-budget-ranking": () => {
    const ranked = Engine.rankMoves([move("高すぎる", {cost_min: 2500, cost_max: 2500})], options({remaining: 1000}));
    check(/残額/.test(ranked[0].problem), `problem=${ranked[0].problem}`);
  },
  "risk-limit-ranking": () => {
    const ranked = Engine.rankMoves([move("危険すぎる", {loss_max: 5000})], options({
      budget: {total_yen: 10000, period_months: 6, per_action_yen: 5000, risk_limit_yen: 1000}
    }));
    check(/許容損失/.test(ranked[0].problem), `problem=${ranked[0].problem}`);
  },
  "stale-data-abstention": () => {
    const ranked = Engine.rankMoves([move("安全な一手")], options());
    const rec = Engine.recommendation(ranked, {state: options().state, goalValue: 100000, dataDate: "2026-08-01", today: "2026-08-06"});
    check(rec.abstain && /古い/.test(rec.reason), JSON.stringify(rec));
  },
  "fallback-marker-roundtrip": () => {
    const marked = move("既定案", {fallback: true});
    check(Engine.normalizeMove(marked).fallback === true, "normalizeMove dropped fallback");
    check(Engine.adjustMove(marked, options().state, {}, "normal").fallback === true, "adjustMove dropped fallback");
  },
  "empty-candidates-abstention": () => {
    const rec = Engine.recommendation([], {state: options().state, goalValue: 100000});
    check(rec.abstain === true && rec.top === null, JSON.stringify(rec));
  },
  "evolution-budget-policy": () => {
    check(Number.isFinite(budgetPolicy.monthly_budget_yen) && budgetPolicy.monthly_budget_yen > 0, "monthly budget missing");
    check(budgetPolicy.per_round_budget_yen > 0 && budgetPolicy.per_round_budget_yen <= budgetPolicy.monthly_budget_yen, "per-round budget invalid");
    check(Number.isInteger(budgetPolicy.max_candidates_per_round) && budgetPolicy.max_candidates_per_round > 1, "candidate cap invalid");
    check(budgetPolicy.strong_model_share_max >= 0 && budgetPolicy.strong_model_share_max <= 1, "strong model share invalid");
    check(budgetPolicy.allow_budget_overrun === false, "budget overrun must be disabled");
  },
  "simulation-risk-limit": () => {
    const risky = move("損失超過", {cost_min: 500, cost_max: 500, loss_max: 5000});
    const r = Engine.simulate([risky], {
      days: 10,
      trials: 100,
      seed: 41,
      state: options().state,
      budget: {total_yen: 10000, spent_yen: 0, per_action_yen: 5000, risk_limit_yen: 1000}
    });
    check(r && r.spend.p50 === 0, `spend.p50=${r && r.spend.p50}`);
  },
  "simulation-zero-per-action-limit": () => {
    const paid = move("上限ゼロで有料", {cost_min: 100, cost_max: 100, loss_max: 100});
    const r = Engine.simulate([paid], {
      days: 1,
      trials: 100,
      seed: 43,
      state: options().state,
      budget: {total_yen: 1000, spent_yen: 0, per_action_yen: 0, risk_limit_yen: 1000}
    });
    check(r && r.spend.p90 === 0, `0円上限なのに spend.p90=${r && r.spend.p90}`);
  },
  "simulation-zero-per-action-excludes-paid": () => {
    const paid = move("上限ゼロで有料候補", {cost_min: 100, cost_max: 100, loss_max: 0});
    const r = Engine.simulate([paid], {
      days: 1,
      trials: 100,
      seed: 47,
      state: options().state,
      budget: {total_yen: 1000, spent_yen: 0, per_action_yen: 0, risk_limit_yen: 1000}
    });
    check(r && r.eligible === 0 && r.excluded === 1 && r.spend.p90 === 0,
      `0円上限の有料案が eligible=${r && r.eligible}, excluded=${r && r.excluded}`);
  },
  "simulation-zero-risk-limit": () => {
    const risky = move("損失許容ゼロ", {cost_min: 100, cost_max: 100, loss_max: 100});
    const r = Engine.simulate([risky], {
      days: 1,
      trials: 100,
      seed: 44,
      state: options().state,
      budget: {total_yen: 1000, spent_yen: 0, per_action_yen: 1000, risk_limit_yen: 0}
    });
    check(r && r.spend.p90 === 0, `0円損失許容なのに spend.p90=${r && r.spend.p90}`);
  },
  "maximum-loss-respects-remaining": () => {
    const lossy = move("残額を超える最大損失", {
      cost_min: 1000,
      cost_max: 1000,
      loss_max: 3000,
      success_p: 0.05,
      success_p_min: 0.05,
      success_p_max: 0.05
    });
    const budget = {total_yen: 1000, spent_yen: 0, per_action_yen: 1000, risk_limit_yen: 3000};
    const ranked = Engine.rankMoves([lossy], options({budget, remaining: 1000}));
    check(/残額/.test(ranked[0].problem), `ranking problem=${ranked[0].problem}`);
    const r = Engine.simulate([lossy], {
      days: 1,
      trials: 100,
      seed: 45,
      state: options().state,
      budget
    });
    check(r && r.spend.p90 <= 1000, `残額1000円なのに spend.p90=${r && r.spend.p90}`);
  },
  "simulation-rechecks-loss-after-spending": () => {
    const lossy = move("残額減少後は再実行できない案", {
      cost_min: 100,
      cost_max: 100,
      loss_max: 1000,
      success_p: 0.05,
      success_p_min: 0.05,
      success_p_max: 0.05
    });
    const r = Engine.simulate([lossy], {
      days: 10,
      trials: 100,
      random: () => 0.99,
      state: options().state,
      budget: {total_yen: 1000, spent_yen: 0, per_action_yen: 1000, risk_limit_yen: 1000}
    });
    check(r && r.spend.p90 <= 1000,
      `残額減少後も最大損失を再確認せず spend.p90=${r && r.spend.p90}`);
  },
  "simulation-time-limit": () => {
    const tooLong = move("180分必要", {time_minutes: 180, cost_min: 1000, cost_max: 1000});
    const r = Engine.simulate([tooLong], {
      days: 10,
      trials: 100,
      seed: 42,
      state: Object.assign({}, options().state, {minutes: 30}),
      budget: {total_yen: 10000, spent_yen: 0, per_action_yen: 5000, risk_limit_yen: 3000}
    });
    check(r && r.spend.p50 === 0, `spend.p50=${r && r.spend.p50}`);
  },
  "ui-simulation-actual-policy": () => {
    const html = fs.readFileSync(path.join(root, "docs", "ichite.html"), "utf8");
    const block = (html.match(/function paintSim\(\)\{[\s\S]*?\n\}/) || [""])[0];
    const usesRemaining = /remaining\s*\(/.test(block) || /spent_yen/.test(block);
    const usesEligible = /CURRENT_RANKED/.test(block) || /rankMoves/.test(block) || /filter\([^)]*problem/.test(block);
    check(usesRemaining && usesEligible, "paintSim uses total budget and unfiltered DATA.moves");
  },
  "fallback-payload-labelled": () => {
    const payload = JSON.parse(fs.readFileSync(path.join(root, "docs", "ichite.json"), "utf8"));
    const known = (payload.moves || []).filter(item => fallbackTitles.has(item.t));
    check(known.every(item => item.fallback === true), `${known.filter(item => item.fallback !== true).length} fallback moves are unlabelled`);
  },
  "benchmarks-have-methodology": () => {
    const recorder = fs.readFileSync(path.join(root, "recorder.py"), "utf8");
    const html = fs.readFileSync(path.join(root, "docs", "ichite.html"), "utf8");
    const hardCoded = /"sota_llm"\s*:\s*0\.13/.test(recorder) || /最先端LLM≒/.test(html);
    check(!hardCoded, "cross-dataset Brier baselines are shown without methodology/source");
  },
  "all-fallback-primary-abstention": () => {
    const fallbackMoves = [
      move("既定案A", {fallback:true}),
      move("既定案B", {fallback:true, learning_value:4})
    ];
    const ranked = Engine.rankMoves(fallbackMoves, options());
    const rec = Engine.recommendation(ranked, {
      state:options().state,
      goalValue:0,
      dataDate:"2026-08-06",
      today:"2026-08-06"
    });
    check(rec.abstain === true && rec.top === null,
      `全既定案を最重要指令に昇格した: ${JSON.stringify(rec)}`);
    check(/既定|材料|推薦/.test(String(rec.reason || "")),
      `見送り理由が材料不足を説明しない: ${JSON.stringify(rec)}`);
  },
  "daily-payload-exact-date-gate": () => {
    check(typeof Engine.dataReadiness === "function",
      "decision-engine.js に dataReadiness(payload, context) がない");
    const status = Engine.dataReadiness({
      date:"2026-08-05",
      build_id:"build-a",
      moves:[move("根拠付きの候補", {fallback:false, evidence_ids:["source-1"]})]
    }, {today:"2026-08-06", buildId:"build-a"});
    check(status && status.usable === false,
      `前日版payloadを今日の推薦へ使用した: ${JSON.stringify(status)}`);
    check(status.stale === true || /日付|当日|古/.test(String(status.reason || "")),
      `日付不一致の理由が分からない: ${JSON.stringify(status)}`);
  },
  "small-sample-performance-hidden": () => {
    check(typeof Engine.metricVisibility === "function",
      "decision-engine.js に metricVisibility(score) がない");
    const visibility = Engine.metricVisibility({
      hit:0,
      miss:2,
      total:2,
      rate:0,
      brier:0.6425,
      brier_n:2
    });
    check(visibility && visibility.showPerformance === false,
      `決着2件で成績を通常表示した: ${JSON.stringify(visibility)}`);
    check(/30|不足|収集/.test(String(visibility.reason || "")),
      `非表示理由に成熟条件がない: ${JSON.stringify(visibility)}`);
  },
  "uncalibrated-simulation-hidden": () => {
    check(typeof Engine.simulationReadiness === "function",
      "decision-engine.js に simulationReadiness(moves, context) がない");
    const readiness = Engine.simulationReadiness([
      move("既定の試算候補", {
        fallback:true,
        evidence_ids:["user-goal"],
        local_calibration:null
      })
    ], {
      goalConfigured:false,
      calibratedOutcomeCount:2,
      minCalibratedOutcomes:30
    });
    check(readiness && readiness.ready === false,
      `未校正の既定値から精密な試算を許可した: ${JSON.stringify(readiness)}`);
    check(Array.isArray(readiness.missing) && readiness.missing.length > 0,
      `足りない前提を説明していない: ${JSON.stringify(readiness)}`);
  },
  "missing-brief-section-hidden": () => {
    const html = fs.readFileSync(path.join(root, "docs", "ichite.html"), "utf8");
    const section = (html.match(/<section[^>]+id="intel"[^>]*>/) || [""])[0];
    const paint = (html.match(/function paintIntel\(\)\{[\s\S]*?\n\}/) || [""])[0];
    check(!/今朝の材料をまだ取得できていません/.test(html),
      "brief欠落時の空カード用メッセージが通常画面に残っている");
    check(!section || /\bhidden\b/.test(section),
      `情報節がbrief取得前から表示される: ${section}`);
    if (section) check(/\.hidden\s*=/.test(paint),
      "paintIntelが有効なbrief取得時だけ情報節を表示する制御を持たない");
  },
  "payload-build-generation-gate": () => {
    check(typeof Engine.dataReadiness === "function",
      "decision-engine.js に dataReadiness(payload, context) がない");
    const status = Engine.dataReadiness({
      date:"2026-08-06",
      build_id:"data-build",
      moves:[move("当日候補", {fallback:false, evidence_ids:["source-1"]})]
    }, {today:"2026-08-06", buildId:"app-build"});
    check(status && status.usable === false,
      `build_id不一致のpayloadを推薦へ使用した: ${JSON.stringify(status)}`);
    check(status.buildMismatch === true || /build|世代|版/.test(String(status.reason || "")),
      `世代不一致の理由が分からない: ${JSON.stringify(status)}`);
  },
  "manual-action-survives-abstention": () => {
    const html = fs.readFileSync(path.join(root, "docs", "ichite.html"), "utf8");
    const own = html.match(/<input[^>]+id="ownT"[^>]*>/);
    const lock = (html.match(/\$\("lock"\)\.addEventListener\("click",function\(\)\{[\s\S]*?\n\}\);/) || [""])[0];
    check(own && !/\bdisabled\b/.test(own[0]), "自分の一手入力が利用できない");
    check(/var own=\$\("ownT"\)\.value\.trim\(\)/.test(lock),
      "見送り時に自分の一手を確定する経路がない");
  }
};

const results = cases.map(item => {
  const started = Date.now();
  try {
    check(typeof checks[item.id] === "function", `missing check implementation: ${item.id}`);
    checks[item.id]();
    return Object.assign({}, item, {passed: true, duration_ms: Date.now() - started, detail: ""});
  } catch (error) {
    return Object.assign({}, item, {passed: false, duration_ms: Date.now() - started, detail: String(error && error.message || error)});
  }
});

function suite(name) {
  const rows = results.filter(row => row.suite === name);
  const passed = rows.filter(row => row.passed).length;
  return {passed, total: rows.length, rate: rows.length ? passed / rows.length : 1};
}

const regression = suite("regression");
const capability = suite("capability");
const dimensions = {};
for (const row of results) {
  const d = dimensions[row.dimension] || {passed: 0, total: 0};
  d.total += 1;
  if (row.passed) d.passed += 1;
  dimensions[row.dimension] = d;
}

const report = {
  schema_version: 1,
  generated_at: new Date().toISOString(),
  commit: process.env.GITHUB_SHA || "local",
  regression,
  capability,
  dimensions,
  hard_gate_passed: regression.passed === regression.total,
  results
};

console.log("\n参謀 Evolution Eval");
console.log(`回帰: ${regression.passed}/${regression.total}  能力: ${capability.passed}/${capability.total}`);
for (const row of results) console.log(`${row.passed ? "PASS" : row.suite === "capability" ? "GAP " : "FAIL"}  ${row.id}${row.detail ? ` — ${row.detail}` : ""}`);

const reportFlag = process.argv.indexOf("--report");
if (reportFlag >= 0 && process.argv[reportFlag + 1]) {
  fs.writeFileSync(path.resolve(process.argv[reportFlag + 1]), JSON.stringify(report, null, 2) + "\n");
}

const enforceCapability = process.argv.includes("--enforce-capability");
if (!report.hard_gate_passed || (enforceCapability && capability.passed !== capability.total)) process.exitCode = 1;
