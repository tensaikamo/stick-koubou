"""参謀の記憶(learning loop)。

過去の記録(records)・予測(hunches)・その答え合わせ結果を読み返し、生成プロンプトへ
注入する「記憶ダイジェスト」を作る。これで参謀は毎朝ゼロから考えるのをやめ、
- 過去に外した型を繰り返さない(較正)
- 継続する話題を"続き"として追う(スレッド)
ようになる。resolver の hit/miss が較正に還流し、使うほど勘が鋭くなる閉ループを閉じる。

依存は標準ライブラリのみ(recorder/sanbo/resolver から安全に import できるよう疎結合に保つ)。
"""
import os, re, json, math
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
DATA_DIR = "data"
RECORDS_PATH = os.path.join(DATA_DIR, "records.json")
HUNCHES_PATH = os.path.join(DATA_DIR, "hunches.json")

RESULT_JA = {"hit": "的中", "miss": "外し", "unclear": "判定不能"}

# スレッド化のための主体(固有名)辞書。正準名 → 別名(小文字)。別名は単語境界一致で拾う
# (部分文字列一致だと "Meta"⊂"metadata" のような誤爆や、Google/Gemini・Moonshot/Kimi のような
#  別名の取りこぼしが起きるため)。
ENTITY_ALIASES = {
    "OpenAI": ["openai", "chatgpt", "gpt-4", "gpt-5", "sora"],
    "Anthropic": ["anthropic", "claude"],
    "Google": ["google", "gemini", "deepmind", "alphabet"],
    "Meta": ["meta", "llama"],
    "Mistral": ["mistral"],
    "xAI": ["xai", "grok"],
    "Microsoft": ["microsoft", "copilot"],
    "Apple": ["apple"],
    "Amazon": ["amazon", "bedrock"],
    "Nvidia": ["nvidia"],
    "DeepSeek": ["deepseek"],
    "Alibaba": ["alibaba", "qwen"],
    "Moonshot": ["moonshot", "kimi"],
    "Fireworks": ["fireworks"],
    "Hugging Face": ["hugging face", "huggingface"],
    "Perplexity": ["perplexity"],
    "Cohere": ["cohere"],
    "Stability": ["stability ai", "stable diffusion"],
    "Groq": ["groq"],
    "Cerebras": ["cerebras"],
    "Tencent": ["tencent"],
}
# 別名の前後が英数字でない=独立トークンの時だけ一致(metadata の meta を弾く)
_ENTITY_PATTERNS = [
    (canon, [re.compile(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])") for a in aliases])
    for canon, aliases in ENTITY_ALIASES.items()
]


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def load_ledger():
    return _load(RECORDS_PATH), _load(HUNCHES_PATH)


def entities_of(text):
    tl = (text or "").lower()
    return {canon for canon, pats in _ENTITY_PATTERNS if any(p.search(tl) for p in pats)}


def hit_stats(hunches):
    """答え合わせ済み(result=hit/miss)から的中率を出す。unclear/pending/unscorable は母数外。"""
    resolved = [h for h in hunches if h.get("result") in ("hit", "miss")]
    hit = sum(1 for h in resolved if h.get("result") == "hit")
    miss = sum(1 for h in resolved if h.get("result") == "miss")
    total = hit + miss
    pending = sum(1 for h in hunches if h.get("status") == "pending")
    review = sum(1 for h in hunches if h.get("needs_review"))
    return {"hit": hit, "miss": miss, "total": total,
            "rate": (hit / total) if total else None, "pending": pending, "review": review}


def brier(hunches):
    """Brier スコア = Σ(確度 − 実現)² / n。予測の世界の標準指標。
    的中率は「当たったか」しか見ないが、Brier は**確度の正しさ**まで測る(低いほど良い)。
    常に50%と答えるだけのベースラインは 0.25。これを下回れば「情報がある」証拠になる。
    戻り: {"score": float, "n": int} / 確定分が無ければ {"score": None, "n": 0}。"""
    s, n = 0.0, 0
    for h in hunches:
        r = h.get("result")
        if r not in ("hit", "miss"):
            continue
        try:
            c = float(h.get("confidence"))
        except Exception:
            continue
        c = max(0.0, min(1.0, c))
        s += (c - (1.0 if r == "hit" else 0.0)) ** 2
        n += 1
    return {"score": (s / n) if n else None, "n": n}


# 「起きない」方向の予測を見分ける語。短期では大半のことは起きないので、
# 否定型の予測は基準率に沿う=本来もっとあってよい。
_NEGATIVE_CLAIM = re.compile(r"しない|されない|見送|撤回|停止|中止|延期|公開されず|"
                             r"出ない|届かない|下回|割れ|落ち込|失速|達しない|至らない")


def _is_negative_claim(claim):
    return bool(_NEGATIVE_CLAIM.search(str(claim or "")))


def brier_parts(hunches, bins=5):
    """Brier をマーフィー分解する: Brier = 較正誤差 − 判別力 + 不確実性。

    合計の Brier だけ見ても「自信の付け方が下手(較正が悪い)」のか
    「そもそもどれが当たるか区別できていない(判別力が無い)」のか分からない。
    実測で確度が0.60〜0.85の狭い帯(標準偏差0.06)に潰れていたので、後者が疑わしい。
    分解すればどちらを直すべきかが数字で出る。

    - reliability(較正誤差): 小さいほど良い。「確度80%と言った群が実際に80%当たる」なら0
    - resolution(判別力): **大きいほど良い**。全部同じ確度なら0=何も予測していないのと同じ
    - uncertainty: 事象そのもののばらつき(打率p̄(1−p̄))。実力ではなく問題の難しさ
    戻り: {"reliability","resolution","uncertainty","brier","n"} / 確定分が無ければ n=0。
    """
    obs = []
    for h in hunches:
        r = h.get("result")
        if r not in ("hit", "miss"):
            continue
        try:
            c = min(max(float(h.get("confidence")), 0.0), 1.0)
        except (TypeError, ValueError):
            continue
        obs.append((c, 1.0 if r == "hit" else 0.0))
    n = len(obs)
    if not n:
        return {"reliability": None, "resolution": None, "uncertainty": None, "brier": None, "n": 0}
    base = sum(o for _, o in obs) / n
    groups = {}
    for c, o in obs:
        k = min(int(c * bins), bins - 1)      # 確度を bins 個の帯にまとめる
        groups.setdefault(k, []).append((c, o))
    rel = res = 0.0
    for g in groups.values():
        nk = len(g)
        ck = sum(c for c, _ in g) / nk        # その帯で言った確度の平均
        ok = sum(o for _, o in g) / nk        # その帯で実際に当たった割合
        rel += nk * (ck - ok) ** 2
        res += nk * (ok - base) ** 2
    return {"reliability": rel / n, "resolution": res / n,
            "uncertainty": base * (1 - base), "brier": brier(hunches)["score"], "n": n}


CONF_FLOOR, CONF_CEIL = 0.05, 0.95   # 確度は0/1に振り切らない(振り切ると Brier が壊滅的に効く)
LOGIT_STEP = 0.45                     # 1回の点灯で動かすログオッズ量(≒確度0.7で±0.08)


def update_confidence(conf, direction, step=LOGIT_STEP):
    """指標が点灯した時に確度を1段だけ動かす(逐次ベイズ更新の最小形)。

    ログオッズ空間で足し引きするので、0/1 に近いほど動きが鈍る=すでに確信している読みは
    小さな兆候で揺れない。確度は CONF_FLOOR〜CONF_CEIL でクリップする。
    従来は指標が点灯しても**表示するだけ**で確度は作成日のまま動かなかった。
    Brier は「確度の正しさ」を測るので、期日に近いほど確度が実態に寄る方がスコアも良くなる。

    conf: 現在の確度(0-1) / direction: "confirm"(上げる) or "kill"(下げる)
    戻り: 更新後の確度(float)。入力が不正、または direction が未知ならそのまま返す。
    """
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return conf
    if direction not in ("confirm", "kill"):
        return c
    c = min(max(c, CONF_FLOOR), CONF_CEIL)
    lo = math.log(c / (1.0 - c)) + (step if direction == "confirm" else -step)
    c2 = 1.0 / (1.0 + math.exp(-lo))
    return round(min(max(c2, CONF_FLOOR), CONF_CEIL), 3)


def _calibration(hunches):
    bands = {}  # band -> [hit, total]
    for h in hunches:
        if h.get("result") not in ("hit", "miss"):
            continue
        try:
            c = float(h.get("confidence"))
        except Exception:
            continue
        b = ("0.9+" if c >= 0.9 else "0.8-0.9" if c >= 0.8 else "0.7-0.8" if c >= 0.7
             else "0.6-0.7" if c >= 0.6 else "0.5-0.6")
        bands.setdefault(b, [0, 0])
        bands[b][1] += 1
        if h.get("result") == "hit":
            bands[b][0] += 1
    return bands


def threads(records, days=45, top=4, compact=False):
    """主体ごとに直近records(days日以内)を束ね、2件以上ある=スレッドを活発順に返す。
    戻り: [(主体, [(日付, 見出し), ...(最大3件)]), ...]"""
    today = datetime.now(JST).date()
    groups = {}
    for r in dedupe_by_url(records):      # 重複記事で「継続」を水増ししない
        d = (r.get("created_at", "") or "")[:10]
        try:
            if (today - datetime.strptime(d, "%Y-%m-%d").date()).days > days:
                continue
        except Exception:
            pass
        src = r.get("source") or {}
        for e in entities_of((r.get("headline", "") or "") + " " + (src.get("title", "") or "")):
            groups.setdefault(e, []).append((d, r.get("headline", "") or ""))
    tl = [(e, evs) for e, evs in groups.items() if len(evs) >= 2]
    tl.sort(key=lambda x: len(x[1]), reverse=True)
    out = []
    for e, evs in tl[:(2 if compact else top)]:
        out.append((e, sorted(set(evs))[-3:]))  # 日付順・末尾3件
    return out


def dedupe_by_url(records):
    """同一 source.url の record は最初の1件だけ残す。
    取り込み側でも重複は防いでいるが、**過去に溜まった重複**(実測54%)がスレッドを水増しし、
    『同じ話が4日続く重要テーマ』という偽の継続性を作るため、読む側でも必ず落とす。
    履歴そのものは改変しない(台帳には全件残る)。"""
    seen, out = set(), []
    for r in records:
        u = ((r.get("source") or {}).get("url") or "").strip()
        if u and u in seen:
            continue
        if u:
            seen.add(u)
        out.append(r)
    return out


def all_threads(records, days=90, min_len=2):
    """物語ページ用: 主体ごとに直近records(days日以内)を時系列で束ね、min_len件以上の
    スレッドを活発順(件数降順)に返す。戻り: [(主体, [(日付, 見出し, url), ...時系列昇順]), ...]。
    memory.threads は digest 用に top/compact で絞るが、こちらは全件を返して"線"を読者に見せる。"""
    today = datetime.now(JST).date()
    groups = {}
    for r in dedupe_by_url(records):      # 同じ記事を毎日拾い直した分を落とす
        d = (r.get("created_at", "") or "")[:10]
        try:
            if (today - datetime.strptime(d, "%Y-%m-%d").date()).days > days:
                continue
        except Exception:
            pass
        src = r.get("source") if isinstance(r.get("source"), dict) else {}
        headline = r.get("headline", "") or ""
        url = src.get("url", "") or ""
        for e in entities_of(headline + " " + (src.get("title", "") or "")):
            groups.setdefault(e, []).append((d, headline, url))
    out = []
    for e, evs in groups.items():
        uniq = sorted(set(evs))  # 日付昇順(古い→新しい)・重複除去
        if len(uniq) >= min_len:
            out.append((e, uniq))
    out.sort(key=lambda x: len(x[1]), reverse=True)
    return out


def next_due(hunches, today):
    """pending の最近接『未来(今日以降)』期日と残り日数を返す。無ければ None。
    戻り: (期日 'YYYY-MM-DD', 残り日数int)。判定待ち期間にも"次の決着"の張りを作るため。"""
    best = None
    for h in hunches:
        if h.get("status") != "pending" or h.get("resolved_at"):
            continue
        try:
            dl = datetime.strptime(str(h.get("deadline", "")), "%Y-%m-%d").date()
        except Exception:
            continue
        rem = (dl - today).days
        if rem < 0:
            continue
        if best is None or rem < best[1]:
            best = (dl.strftime("%Y-%m-%d"), rem)
    return best


def related_ids_for(headline, source_title, existing_records, days=45, limit=6):
    """新recordの主体と重なる直近recordのidを返す(related_ids 実働化=記憶を線にする)。"""
    ents = entities_of((headline or "") + " " + (source_title or ""))
    if not ents:
        return []
    today = datetime.now(JST).date()
    out = []
    for r in reversed(existing_records):  # 新しい順
        d = (r.get("created_at", "") or "")[:10]
        try:
            if (today - datetime.strptime(d, "%Y-%m-%d").date()).days > days:
                continue
        except Exception:
            pass
        src = r.get("source") or {}
        if entities_of((r.get("headline", "") or "") + " " + (src.get("title", "") or "")) & ents:
            rid = r.get("id")
            if rid:
                out.append(rid)
        if len(out) >= limit:
            break
    return out


def build_digest(records, hunches, compact=False):
    """生成プロンプトへ注入する記憶ダイジェスト。データが無ければ空文字。"""
    lines = []
    st = hit_stats(hunches)
    if st["total"] == 0:
        lines.append("較正: まだ答え合わせ前(較正データなし)。確度は根拠の強さだけで決め、過信するな。")
    else:
        cal = "通算 的中%d/外し%d(的中率%d%%)。" % (st["hit"], st["miss"], round(st["rate"] * 100))
        parts = []
        bands = _calibration(hunches)          # ループ外で1回だけ計算する
        for b in ["0.9+", "0.8-0.9", "0.7-0.8", "0.6-0.7", "0.5-0.6"]:
            bd = bands.get(b)
            if bd and bd[1] > 0:
                parts.append("確度%s→実際%d%%(n=%d)" % (b, round(bd[0] / bd[1] * 100), bd[1]))
        if parts:
            cal += " 較正実績: " + " / ".join(parts) + "。実績が確度を下回る帯は確度を下げよ。"
        lines.append("較正: " + cal)

        # Brier とその分解。合計だけだと「較正が悪い」のか「判別力が無い」のか区別できない。
        bp = brier_parts(hunches)
        if bp["n"]:
            b = brier(hunches)["score"]
            lines.append(
                "Brier %.3f(n=%d / 常に50%%と答えるだけのベースラインは0.250。これを上回っていたら情報が無い)。"
                "内訳: 較正誤差%.3f(小さいほど良い) / 判別力%.3f(**大きいほど良い**)。"
                % (b, bp["n"], bp["reliability"], bp["resolution"]))
            if bp["resolution"] < 0.01:
                lines.append("警告: 判別力がほぼゼロ。全部に似た確度を付けている=何も予測していないのと同じ。"
                             "確からしい読みと怪しい読みで確度をはっきり変えろ。")
            if b is not None and b > 0.25:
                lines.append("警告: ベースライン(0.250)より悪い。確度を付けすぎている。"
                             "基準率まで引き戻し、迷うものは0.5未満で出せ。")

    # 予測の向きの偏り。短期では大半のことは起きないので、「起きる」型ばかり作るのは
    # 体系的な過信になる(実測33件中33件が「起きる」型で、最初の決着は外れだった)。
    scored = [h for h in hunches if h.get("result") in ("hit", "miss")] or hunches
    if len(scored) >= 5:
        neg = sum(1 for h in scored if _is_negative_claim(h.get("claim", "")))
        if neg * 4 < len(scored):     # 「起きない」型が1/4未満
            lines.append("偏り: 直近の予測%d件中『起きない/見送られる』型は%d件しかない。"
                         "短期では大半のことは起きない。起きない方に賭ける勇気を持て。" % (len(scored), neg))

    recent = list(reversed(hunches))[:(5 if compact else 10)]
    if recent:
        lines.append("直近の自分の読みと結果(同じ型の失敗を繰り返すな):")
        for h in recent:
            claim = (h.get("claim", "") or "")[:44]
            date = (h.get("created_at", "") or "")[:10]
            r = h.get("result")
            if r in ("hit", "miss"):
                ev = h.get("evidence") or {}
                why = ""
                if r == "miss" and isinstance(ev, dict) and ev.get("summary"):
                    why = " 理由:" + str(ev["summary"])[:44]
                lines.append("- [%s] %s → %s%s" % (date, claim, RESULT_JA.get(r), why))
            else:
                lines.append("- [%s] %s → 判定待ち(期限%s)" % (date, claim, h.get("deadline", "")))

    th = threads(records, compact=compact)
    if th:
        lines.append("継続スレッド(新ネタ扱いせず「続き」として乗り換えず追え):")
        for subj, evs in th:
            lines.append("- %s: %s" % (subj, " → ".join("%s %s" % (d, hl[:26]) for d, hl in evs)))

    if not lines:
        return ""
    head = ("【参謀の記憶=過去の自分の観測・読み・答え合わせ結果。ゼロから考えるな。"
            "これを踏まえて確度を較正し、既存スレッドは続きとして書け】\n")
    return head + "\n".join(lines)
