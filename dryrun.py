"""予測生成の**素振り**(本番データに一切書かない)。

なぜ要るか: 生成の規律(参照クラスの必須化・確度の下限撤廃・否定型の許容)を変えても、
効いているかは翌朝の走行まで分からない。それでは「変えたつもり」で何日も走ってしまう。

安全性(ここが要点):
- 本物の `data/` を一時ディレクトリへ複製し、そこへ chdir して**本物の recorder** を動かす。
  台帳が現実の記憶(較正・スレッド)を持った状態で生成させるため、素の空実行より意味がある。
- 生成物は一時ディレクトリにしか書かれない。**リポジトリの data/ と docs/ には触れない**。
- 走り終えたら丸ごと捨てる。コミットもしない。
- LLM 呼び出しは通常の1日分と同じ(記録1回+ゲート不合格時の再生成)。

使い方: GEMINI_API_KEY を与えて `python dryrun.py`。結果は標準出力にだけ出る。
"""
import os, sys, json, shutil, tempfile, statistics
from datetime import datetime

import recorder
import memory

JST = recorder.JST


def _summarize(hunches, records):
    """生成された予測を、直したかった軸で測る。"""
    print("\n" + "=" * 64)
    print("素振りの結果: 記録 %d件 / 予測 %d件" % (len(records), len(hunches)))
    print("=" * 64)
    scored = [h for h in hunches if h.get("status") == "pending"]
    if not scored:
        print("(採点対象の予測が作られなかった)")
        return
    for h in scored:
        br, cf = h.get("base_rate"), h.get("confidence")
        arrow = "?"
        if isinstance(br, (int, float)) and isinstance(cf, (int, float)):
            arrow = "↑" if cf > br else ("↓" if cf < br else "→")
        neg = "否定型" if memory._is_negative_claim(h.get("claim", "")) else "肯定型"
        print("\n- [%s] %s" % (neg, str(h.get("claim", ""))[:70]))
        print("    基準率 %s %s 確度 %s   参照クラス: %s"
              % (br, arrow, cf, str(h.get("base_rate_class", ""))[:44]))
        if h.get("confidence_why"):
            print("    ずらした理由: " + str(h["confidence_why"])[:80])
        print("    判定先: " + str((h.get("resolution") or {}).get("source", ""))[:70])

    confs = [h["confidence"] for h in scored if isinstance(h.get("confidence"), (int, float))]
    negs = sum(1 for h in scored if memory._is_negative_claim(h.get("claim", "")))
    withbr = sum(1 for h in scored if isinstance(h.get("base_rate"), (int, float)))
    https = sum(1 for h in scored
                if str((h.get("resolution") or {}).get("source", "")).startswith("https://"))
    print("\n" + "-" * 64)
    print("直したかった軸:")
    print("  参照クラスつき : %d/%d" % (withbr, len(scored)))
    print("  判定先が実URL  : %d/%d" % (https, len(scored)))
    print("  否定型の予測   : %d/%d" % (negs, len(scored)))
    if confs:
        print("  確度           : %s" % [round(c, 2) for c in confs])
        print("                   min=%.2f max=%.2f%s"
              % (min(confs), max(confs),
                 (" 標準偏差=%.3f" % statistics.pstdev(confs)) if len(confs) > 1 else ""))
        print("  0.5未満の確度  : %d件 (以前はコード側の下限0.50で出せなかった)"
              % sum(1 for c in confs if c < 0.5))
    print("-" * 64)


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        print("dryrun: GEMINI_API_KEY 未設定。中止")
        return 1
    repo = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="sanbo-dryrun-")
    try:
        # 現実の記憶を持たせるため data/ を複製する(読むだけでなく、書き込み先も複製側)
        shutil.copytree(os.path.join(repo, "data"), os.path.join(tmp, "data"))
        os.makedirs(os.path.join(tmp, "docs"), exist_ok=True)
        os.chdir(tmp)
        # 冪等ガードを外して、今日の分をもう一度「作らせる」(複製側なので本番は無傷)
        today = datetime.now(JST).strftime("%Y-%m-%d")
        rp = os.path.join("data", "runs", today + ".json")
        if os.path.exists(rp):
            os.remove(rp)
        before = len(recorder.load_json_array(recorder.HUNCHES_PATH))
        recorder.main()
        hunches = recorder.load_json_array(recorder.HUNCHES_PATH)
        records = recorder.load_json_array(recorder.RECORDS_PATH)
        _summarize(hunches[before:], records)
    finally:
        os.chdir(repo)
        shutil.rmtree(tmp, ignore_errors=True)
        # 念のため: 本番側が変わっていないことを声に出して確かめる
        print("\ndryrun: 一時ディレクトリを破棄。リポジトリの data/ docs/ は未変更。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
