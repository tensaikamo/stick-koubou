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

console.log("decision engine tests passed");
