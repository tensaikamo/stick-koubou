"""参謀の記憶(learning loop)。

過去の記録(records)・予測(hunches)・その答え合わせ結果を読み返し、生成プロンプトへ
注入する「記憶ダイジェスト」を作る。これで参謀は毎朝ゼロから考えるのをやめ、
- 過去に外した型を繰り返さない(較正)
- 継続する話題を"続き"として追う(スレッド)
ようになる。resolver の hit/miss が較正に還流し、使うほど勘が鋭くなる閉ループを閉じる。

依存は標準ライブラリのみ(recorder/sanbo/resolver から安全に import できるよう疎結合に保つ)。
"""
import os, re, json
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
    for r in records:
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


def all_threads(records, days=90, min_len=2):
    """物語ページ用: 主体ごとに直近records(days日以内)を時系列で束ね、min_len件以上の
    スレッドを活発順(件数降順)に返す。戻り: [(主体, [(日付, 見出し, url), ...時系列昇順]), ...]。
    memory.threads は digest 用に top/compact で絞るが、こちらは全件を返して"線"を読者に見せる。"""
    today = datetime.now(JST).date()
    groups = {}
    for r in records:
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
        for b in ["0.9+", "0.8-0.9", "0.7-0.8", "0.6-0.7", "0.5-0.6"]:
            bd = _calibration(hunches).get(b)
            if bd and bd[1] > 0:
                parts.append("確度%s→実際%d%%(n=%d)" % (b, round(bd[0] / bd[1] * 100), bd[1]))
        if parts:
            cal += " 較正実績: " + " / ".join(parts) + "。実績が確度を下回る帯は確度を下げよ。"
        lines.append("較正: " + cal)

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
