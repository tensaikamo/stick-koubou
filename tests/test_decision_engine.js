"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..");
const E = require(path.join(root, "docs", "decision-engine.js"));

function move(title, category, extra) {
  return Object.assign({
    t:title, why:"test", category, cost_min:0, cost_max:0, loss_max:0,
    success_p:.6, success_p_min:.5, success_p_max:.7,
    success_why:"observable", value_score:3, learning_value:2, time_minutes:20,
    payback_days:0, impact_min:.05, impact_max:.1, impact_why:"goal link",
    evidence_ids:["r1"], assumptions:["a"], disconfirm:"fails", outcome:"artifact",
    continue_if:"works", stop:"done"
  }, extra || {});
}

const budget = {total_yen:10000, period_months:6, per_action_yen:5000, risk_limit_yen:5000};
function opts(state, extra) {
  return Object.assign({state:Object.assign({stage:"build", bottleneck:"technical", outcome:"complete",
    minutes:30, risk:"balanced", configured:true}, state || {}), days:{}, fitOverrides:{},
    goalValue:100000, budget, remaining:10000}, extra || {});
}

// 入力が飾りではなく、段階に応じて実際に順位を変える。
let moves = [move("実装する", "build"), move("販売する", "sell")];
let buildFirst = E.rankMoves(moves, opts({stage:"build"}));
let sellFirst = E.rankMoves(moves, opts({stage:"revenue", outcome:"sell", bottleneck:"customer"}));
assert.equal(buildFirst[0].move.t, "実装する");
assert.equal(sellFirst[0].move.t, "販売する");

// exactな段階×行動履歴を優先し、失敗が重なると成功率を慎重に下げる。
let days = {};
for (let i=1; i<=3; i++) days["2026-08-0"+i] = {goal_advanced:"no", move_meta:{category:"build", state_snapshot:{stage:"build"}}};
let adjusted = E.adjustMove(move("実装する", "build", {success_p:.8, success_p_min:.7, success_p_max:.9}),
  opts().state, days, "normal");
assert.equal(adjusted.local_calibration.n, 3);
assert(adjusted.success_p < .8 && adjusted.success_p > .5);

// 利用者が「この目標には合わない」と直せば、寄与と順位が本当に落ちる。
let fitted = E.rankMoves(moves, opts({stage:"build"}, {fitOverrides:{"実装する":"none"}}));
assert.equal(fitted[0].move.t, "販売する");
assert.equal(fitted.find(x => x.move.t === "実装する").move.impact_max, 0);

// 今日使える時間を超える案は、良さそうでも選択不可。
let tooLong = E.rankMoves([move("長い作業", "build", {time_minutes:30})], opts({minutes:15}));
assert.match(tooLong[0].problem, /15分/);
let cycleEnded = E.rankMoves(moves, opts({}, {globalProblem:"予算期間終了"}));
assert(cycleEnded.every(x => x.problem === "予算期間終了"));

// 目標金額が未設定でも、リスク方針は費用・最大損失の重みを変える。
let riskMoves = [
  move("有料ツールを試す", "buy", {value_score:4, success_p:.7, learning_value:2,
    cost_min:5000, cost_max:5000, loss_max:5000}),
  move("無料で小さく実装", "build", {value_score:2, success_p:.6, learning_value:1})
];
let cautious = E.rankMoves(riskMoves, opts({risk:"cautious"}, {goalValue:0}));
let aggressive = E.rankMoves(riskMoves, opts({risk:"aggressive"}, {goalValue:0}));
assert.equal(cautious[0].move.t, "無料で小さく実装");
assert.equal(aggressive[0].move.t, "有料ツールを試す");

// 全案が負なら無理に勧めず、古い材料でも保留する。
let bad = E.rankMoves([move("損する案", "buy", {cost_min:5000, cost_max:5000, loss_max:5000,
  impact_min:0, impact_max:0})], opts());
assert.equal(E.recommendation(bad, {state:opts().state, goalValue:100000}).abstain, true);
let fresh = E.rankMoves(moves, opts());
assert.match(E.recommendation(fresh, {state:opts().state, goalValue:100000,
  dataDate:"2026-08-01", today:"2026-08-06"}).reason, /古い/);

// HTML内のインラインJavaScriptも構文として実行可能であることをCIで守る。
const html = fs.readFileSync(path.join(root, "docs", "ichite.html"), "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(s => s.trim());
assert(scripts.length > 0);
scripts.forEach(s => new Function(s));
for (const marker of ["stateStage", "stateBottleneck", "recommended", "move_fit", "今日は決めない"]) {
  assert(html.includes(marker), "missing UI marker: " + marker);
}

// 既定案の印は normalizeMove のホワイトリストに載っていないと黙って落ち、
// 「5件とも既定案」でも画面上は普通の一手として並んでしまう。
assert.strictEqual(E.normalizeMove(move("既定案", "build", {fallback:true})).fallback, true);
assert.strictEqual(E.normalizeMove(move("通常案", "build")).fallback, false);
assert.strictEqual(
  E.adjustMove(move("既定案", "build", {fallback:true}), {}, {}, null).fallback, true,
  "adjustMove を通すと印が消える");
assert(html.includes("既定案"), "既定案バッジが画面に出ない");

// ラウンド1.5: 今日の材料・表示世代・成熟度を別々に検査し、数字があるだけで推薦しない。
{
  const daily = {date:"2026-08-06", build_id:"truthful-ui-v1",
    moves:[move("当日の一手", "build")]};
  assert.strictEqual(E.dataReadiness(daily, {today:"2026-08-06", buildId:"truthful-ui-v1"}).usable, true);
  assert.strictEqual(E.dataReadiness(daily, {today:"2026-08-07", buildId:"truthful-ui-v1"}).usable, false);
  assert.strictEqual(E.dataReadiness(daily, {today:"2026-08-06", buildId:"next-build"}).buildMismatch, true);

  assert.strictEqual(E.metricVisibility({total:2, brier_n:2}).showPerformance, false);
  assert.strictEqual(E.metricVisibility({total:30, brier_n:30}).showPerformance, true);
  assert.strictEqual(E.simulationReadiness([move("未校正", "build")], {
    goalConfigured:true, calibratedOutcomeCount:2, minCalibratedOutcomes:30
  }).ready, false);

  // 既定案の点数が高くても、当日の根拠を持つ候補より上へ自動昇格しない。
  const mixed = E.rankMoves([
    move("高得点の既定案", "build", {fallback:true, value_score:5, success_p:.95}),
    move("当日の根拠付き案", "build", {value_score:1, success_p:.1, success_p_min:.05, success_p_max:.15})
  ], opts({}, {goalValue:0}));
  assert.strictEqual(E.recommendation(mixed, {state:opts().state, goalValue:0}).top.move.t,
    "当日の根拠付き案");

  const payload = JSON.parse(fs.readFileSync(path.join(root, "docs", "ichite.json"), "utf8"));
  const appBuild = (html.match(/<meta name="app-build-id" content="([^"]+)"/) || [])[1];
  assert(appBuild && payload.build_id === appBuild, "画面と候補JSONのbuild_idが一致しない");
  const decisionPy = fs.readFileSync(path.join(root, "decision.py"), "utf8");
  const serverBuild = (decisionPy.match(/^BUILD_ID = "([^"]+)"/m) || [])[1];
  assert.strictEqual(serverBuild, appBuild, "生成側と画面のbuild_idが一致しない");
  assert(html.includes('var APP_BUILD_ID="'+appBuild+'"'), "画面の実行時build_idがmetaと一致しない");
  const generator = fs.readFileSync(path.join(root, "sanbo.py"), "utf8");
  assert(generator.includes('"build_id": decision.BUILD_ID'), "次回生成でbuild_idが消える");
  assert(/brief\.setAttribute\("href",material\.state==="ready"\?"#intel":"#mission"\)/.test(html),
    "当日材料が無い時に古い別ページへ誘導している");
  assert(/gridTemplateColumns=valid\?"repeat\(4,1fr\)":"repeat\(3,1fr\)"/.test(html),
    "情報タブを隠した後もドックが4列の空枠を残す");
}

// 2200msの第一描画のあとに本物の材料が届いた場合、既定案のまま固定してはいけない
// (iPhoneのモバイル回線では2.2秒超えは普通に起きる)。settleData を切り出して直接動かす。
{
  const src = html.match(/function settleData\(state\)\{[\s\S]*?render\(\);\}/);
  assert(src, "settleData を取り出せない");
  let renders = 0;
  const sandbox = {dataSettled:false, DATA_STATE:null, DATA_READY:false,
                   clearTimeout(){}, dataTimer:null, render(){renders++;}};
  const settleData = new Function("ctx", `
    with (ctx) { ${src[0]}
      return function(s){ settleData(s); }; }`)(sandbox);
  settleData("fallback");
  assert.strictEqual(sandbox.DATA_STATE, "fallback");
  settleData("daily");                        // 遅れて本物が届く
  assert.strictEqual(sandbox.DATA_STATE, "daily", "遅れて届いた材料が反映されない");
  assert.strictEqual(renders, 2, "差し替え時に描き直していない");
  settleData("fallback");                     // 逆方向の降格は無視する
  assert.strictEqual(sandbox.DATA_STATE, "daily");
  assert.strictEqual(renders, 2);
}
// 2200msのタイマーは描画のためのもので、取得を中断してはいけない
assert(!/dataTimer=setTimeout\(function\(\)\{if\(dataController\)dataController\.abort\(\)/.test(html),
  "第一描画のタイマーが取得を中断している");

// 起動画面の退場: 擬似要素(::before/::after)のアニメーション終了は originating element を
// target として発火するため、e.target===boot だけで見ると十字線 cross-a(0.57s)で起動画面を
// 消してしまい、boot-exit(0.92s)のフェードが始まる前に全開のまま消える(実測460ms)。
{
  const src = html.match(/boot\.addEventListener\("animationend",function\(e\)\{[\s\S]*?\}\);/);
  assert(src, "起動画面の animationend ハンドラを取り出せない");
  let cb = null, finished = 0;
  // ハンドラが閉じ込む boot と、イベントの target を同一オブジェクトにする
  const boot = {addEventListener:(n, f) => { cb = f; }};
  new Function("boot", "finish", src[0])(boot, () => { finished++; });
  assert(cb, "リスナーが登録されない");
  cb({target: boot, pseudoElement: "::before", animationName: "cross-a"});
  assert.strictEqual(finished, 0, "十字線の擬似要素アニメで起動画面を消している");
  cb({target: boot, pseudoElement: "", animationName: "mark-in"});
  assert.strictEqual(finished, 0, "退場以外のアニメで起動画面を消している");
  cb({target: boot, pseudoElement: "", animationName: "boot-exit"});
  assert.strictEqual(finished, 1, "退場アニメ終了で起動画面が消えない");
}
// 復帰導線は起動画面の有無と無関係。セッション2回目(起動画面を出さない経路)でも仕掛かること。
{
  const boot = html.indexOf('var boot=document.getElementById("boot")');
  const fault = html.indexOf('appReady!=="1"');
  assert(fault > -1 && boot > -1 && fault < boot,
    "復帰タイマーが起動画面の early return より後ろにあり、2回目以降で仕掛からない");
}

/* ---- 実演算(モンテカルロ) ---- */
{
  const strong = move("強い一手", "build", {impact_min:.2, impact_max:.3, success_p:.9,
    success_p_min:.85, success_p_max:.95, cost_min:0, cost_max:0});
  const weak = move("弱い一手", "research", {impact_min:0, impact_max:.01, success_p:.5,
    success_p_min:.4, success_p_max:.6, cost_min:0, cost_max:0});
  const budget = {total_yen:30000, per_action_yen:5000, spent_yen:0};

  // 同じ種でいつでも同じ絵になる(占いではなく再現可能な演算であること)
  const a = E.simulate([strong], {days:90, budget, seed:7});
  const b2 = E.simulate([strong], {days:90, budget, seed:7});
  assert.deepStrictEqual(a, b2, "同じ種で結果が揺れる");
  assert.notDeepStrictEqual(a, E.simulate([strong], {days:90, budget, seed:8}));

  // 強い一手のほうが遠くまで進む
  assert(E.simulate([strong], {days:90, budget, seed:1}).progress.p50 >
         E.simulate([weak], {days:90, budget, seed:1}).progress.p50, "強弱が結果に出ない");

  // 繰り返しの逓減が効く: 同じ一手だけを続けても必ず達成にはならない
  const only = E.simulate([weak], {days:365, budget, seed:3});
  assert(only.progress.p90 < 1, "同じ一手を続けるだけで目標達成になっている(逓減が効いていない)");

  // 分位は順序を保ち、確率は0..1に収まる
  const r = E.simulate([strong, weak], {days:90, budget, seed:5});
  assert(r.progress.p10 <= r.progress.p50 && r.progress.p50 <= r.progress.p90);
  assert(r.reach_p >= 0 && r.reach_p <= 1);
  assert(r.spend.p50 <= r.spend.p90);

  // 予算を超える有料案は選ばれない(残額を守る)
  const pricey = move("高い一手", "buy", {cost_min:99000, cost_max:99000, impact_min:.5, impact_max:.9});
  assert.strictEqual(E.simulate([pricey], {days:30, budget, seed:2}).spend.p50, 0,
    "残額を超える一手に支出している");

  // 材料が無ければ何も出さない(空の絵を作らない)
  assert.strictEqual(E.simulate([], {days:30, budget}), null);
  assert.strictEqual(E.simulate(null, {}), null);
}

// 統合: 参謀の知識(空気・勘・成績・記憶)が別ページではなく同じ画面の節として出ること
for (const marker of ["intelBody", "sanboScore", "memoryBody", "paintIntel", "paintMemory", "paintSim",
                      '["brief","ledger"]', "今日の空気"]) {
  assert(html.includes(marker), "統合された知識の部品が無い: " + marker);
}
// ドックは全部が面内遷移(別デザインの別ページへ飛ばさない)
{
  const dock = html.match(/<nav class="dock"[\s\S]*?<\/nav>/)[0];
  const hrefs = [...dock.matchAll(/href="([^"]+)"/g)].map(m => m[1]);
  assert(hrefs.length >= 4, "ドックの項目が足りない");
  hrefs.forEach(h => assert(h.startsWith("#"), "ドックが別ページへ飛ぶ: " + h));
}
// 参謀の的中率とあなたの遂行率が同じ画面に並ぶので、どちらの数字か必ず言うこと
assert(html.includes("あなたの遂行率"), "利用者側の数字にラベルが無い");
assert(html.includes("参謀の的中率"), "参謀側の数字にラベルが無い");

/* ---- ラウンド1: 試算にも実際の方針を適用する(憲法2章) ---- */
{
  const budget = {total_yen:30000, per_action_yen:5000, risk_limit_yen:1000, spent_yen:0};
  const state = {stage:"build", bottleneck:"technical", outcome:"complete", minutes:30,
                 risk:"balanced", configured:true};
  // 許容損失を超える賭けは試算でも選ばない(推薦では弾くのに試算では通す二枚舌を作らない)
  const risky = move("損失超過", "buy", {cost_min:500, cost_max:500, loss_max:5000});
  const r1 = E.simulate([risky], {days:10, trials:100, seed:41, state, budget});
  assert.strictEqual(r1.spend.p50, 0, "許容損失を超える案に試算が支出している");
  assert.strictEqual(r1.eligible, 0);
  assert.strictEqual(r1.excluded, 1, "除外件数を報告していない");

  // 今日の可処分時間で終わらない案も選ばない
  const tooLong = move("180分必要", "build", {time_minutes:180, cost_min:1000, cost_max:1000});
  assert.strictEqual(E.simulate([tooLong], {days:10, trials:100, seed:42, state, budget}).spend.p50, 0,
    "今日の時間を超える案に試算が支出している");

  // 「材料が無い」と「方針で全部落ちた」は別物。前者は null、後者は0の結果を返す
  assert.strictEqual(E.simulate([], {state, budget}), null);
  assert.notStrictEqual(E.simulate([risky], {days:10, state, budget}), null);

  // 方針を満たす案は通常どおり回る
  const ok = move("普通の一手", "build", {cost_min:0, cost_max:0, loss_max:0, time_minutes:25});
  assert(E.simulate([ok], {days:30, trials:200, seed:9, state, budget}).eligible === 1);
}
// 鮮度のしきい値は推薦と画面で同じ定義を使う(二重定義でズレさせない)
assert.strictEqual(typeof E.STALE_DAYS, "number");
assert(html.includes("StickDecision.STALE_DAYS"), "画面が独自のしきい値を持っている");
// 試算は残額と「今日実行できる候補」を使う(総予算と全候補で甘い数字を出さない)
{
  const block = html.match(/function paintSim\(\)\{[\s\S]*?\n\}/)[0];
  assert(/remaining\(/.test(block), "試算が実残額を使っていない");
  assert(/CURRENT_RANKED/.test(block), "試算が実行可能な候補に絞っていない");
  assert(/S\.state/.test(block), "試算に今日の時間条件を渡していない");
}
// 比較できないベンチマークを同じ物差しに並べない(憲法3章)
assert(!/最先端LLM|超予測者/.test(html), "測定条件の違うベンチマークを並べている");

// 描画順: startViewTransition(paint) は非同期なので、統合した節を render の外に置くと
// paintSim が CURRENT_RANKED の更新前に走り、実行可能な候補が0件に見えて試算が消える。
// paint と同じ関数の中で順番に呼ぶこと。
{
  const paintAll = html.match(/function paintAll\(\)\{[\s\S]*?\n\}/);
  assert(paintAll, "paintAll が無い");
  assert(/paint\(\);/.test(paintAll[0]), "paintAll が paint を呼んでいない");
  assert(/paintSim/.test(paintAll[0]), "paintAll が paintSim を呼んでいない");
  assert(/startViewTransition\(paintAll\)/.test(html),
    "view transition が paint 単体を呼んでおり、統合した節が古い状態で描かれる");
}
// 候補が全部落ちた時に黙って消えると「壊れている」と区別が付かない。理由を出すこと。
{
  const block = html.match(/function paintSim\(\)\{[\s\S]*?\n\}/)[0];
  assert(/ranked\.length&&!eligible\.length/.test(block), "候補ゼロの場合を区別していない");
  assert(/x\.problem/.test(block), "落ちた理由を集計していない");
}

/* ---- 予算の境界値: 0 は「無制限」ではない ---- */
{
  const state = {stage:"build", bottleneck:"technical", outcome:"complete", minutes:30,
                 risk:"balanced", configured:true};
  // 1回上限0円は「上限なし」ではなく「有料案は選べない」。!perAction || のような短絡を書くと
  // 0 が素通りし、利用者が最も強く締めた設定が最も緩く効くという逆転が起きる。
  const paid = move("上限ゼロで有料", "buy", {cost_min:100, cost_max:100, loss_max:100});
  assert.strictEqual(
    E.simulate([paid], {days:1, trials:100, seed:43, state,
      budget:{total_yen:1000, spent_yen:0, per_action_yen:0, risk_limit_yen:1000}}).spend.p90, 0,
    "1回上限0円を無制限として扱っている");
  // 許容損失0円も同じ。riskLimit && で短絡させない
  assert.strictEqual(
    E.simulate([paid], {days:1, trials:100, seed:44, state,
      budget:{total_yen:1000, spent_yen:0, per_action_yen:1000, risk_limit_yen:0}}).spend.p90, 0,
    "許容損失0円を無制限として扱っている");
  // 0 でも無料案(cost 0 / loss 0)は通る。締めすぎて何もできなくなるわけではない
  const free = move("無料の一手", "build", {cost_min:0, cost_max:0, loss_max:0});
  assert(E.simulate([free], {days:5, trials:50, seed:45, state,
    budget:{total_yen:1000, spent_yen:0, per_action_yen:0, risk_limit_yen:0}}).eligible === 1,
    "0円上限で無料案まで弾いている");

  // 最大損失が残額を超える案は、推薦と試算の両方から外す。
  // 費用が残額内でも、失敗した時に払い切れない賭けは打てない。
  const lossy = move("残額を超える最大損失", "buy",
    {cost_min:1000, cost_max:1000, loss_max:3000, success_p:.05, success_p_min:.05, success_p_max:.05});
  const budget = {total_yen:1000, spent_yen:0, per_action_yen:1000, risk_limit_yen:3000};
  const ranked = E.rankMoves([lossy], {state, days:{}, fitOverrides:{}, goalValue:100000,
                                       budget, remaining:1000});
  assert(/残額/.test(ranked[0].problem), "推薦が最大損失を残額と突き合わせていない: " + ranked[0].problem);
  assert(E.simulate([lossy], {days:1, trials:100, seed:46, state, budget}).spend.p90 <= 1000,
    "試算が残額を超える損失を許している");

  // 許容損失と残額の両方に触れる案は、利用者が明示した上限の方を理由として返す
  assert.strictEqual(
    E.budgetProblem(move("両方超過", "buy", {cost_max:0, loss_max:5000}),
      {per_action_yen:1000, risk_limit_yen:1000}, 1000, state), "許容損失を超える");
}

/* ---- 複数日試算: 残額は日々減る ---- */
{
  const state = {stage:"build", bottleneck:"technical", outcome:"complete", minutes:30,
                 risk:"balanced", configured:true};
  // 1回上限は日ごとに変わらない方針なので母集団の時点で落とす。日ごとの絞り込みにだけ
  // 置くと「実行できないのに候補として数えられる」= eligible が水増しされる。
  const paid = move("上限ゼロで有料候補", "buy", {cost_min:100, cost_max:100, loss_max:0});
  const r0 = E.simulate([paid], {days:1, trials:100, seed:47, state,
    budget:{total_yen:1000, spent_yen:0, per_action_yen:0, risk_limit_yen:1000}});
  assert.strictEqual(r0.eligible, 0, "1回上限0円の有料案が候補に数えられている");
  assert.strictEqual(r0.excluded, 1, "除外件数を報告していない");
  assert.strictEqual(r0.spend.p90, 0);

  // 初日の残額で一度判定して終わりにすると、失敗が重なった後も同じ賭けを打ち続け、
  // 支出が残額を超える(実測: 残額1000円に対し1100円)。毎日の残額で再判定すること。
  const lossy = move("残額減少後は再実行できない案", "buy",
    {cost_min:100, cost_max:100, loss_max:1000, success_p:.05, success_p_min:.05, success_p_max:.05});
  const r1 = E.simulate([lossy], {days:10, trials:100, random:() => 0.99, state,
    budget:{total_yen:1000, spent_yen:0, per_action_yen:1000, risk_limit_yen:1000}});
  assert(r1.spend.p90 <= 1000, "残額減少後に最大損失を再確認していない: " + r1.spend.p90);

  // 不変条件: どんな組み合わせでも支出は残額を超えない
  let over = 0, checked = 0;
  for (const cost of [0, 100, 500, 1000]) for (const loss of [0, 100, 1000, 3000])
  for (const total of [500, 1000, 10000]) for (const per of [0, 1000, 5000])
  for (const risk of [0, 1000, 5000]) for (const seed of [1, 7]) {
    const m = move("網羅", "build", {cost_min:cost, cost_max:cost, loss_max:loss,
      success_p:.3, success_p_min:.2, success_p_max:.4, time_minutes:20});
    const r = E.simulate([m], {days:30, trials:60, seed, state,
      budget:{total_yen:total, spent_yen:0, per_action_yen:per, risk_limit_yen:risk}});
    if (!r) continue;
    checked++;
    if (r.spend.p90 > total) over++;
  }
  assert(checked > 500, "網羅が足りない: " + checked);
  assert.strictEqual(over, 0, "残額を超える支出が出た組み合わせがある: " + over);
}

console.log("decision engine tests passed");
