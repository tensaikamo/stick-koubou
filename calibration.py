"""確度の較正層 — 運・無知・実力を切り分ける。

参謀は「確度0.77」と言って実測4.5%しか当てていない(Brier 0.569)。
Murphy分解すると内訳は 較正誤差0.524 / 判別力0.002 / 不確実性0.043 で、
**失敗の92%は運ではなく較正の壊れ**だった。ここはコードで直せる。

較正の形:
    logit(p_較正) = a * logit(p_生) + b

なぜこの形か。判別力が無い(=生の確度がどれが当たるか区別できていない)場合、
最尤推定は自然に a→0 へ向かい、出力は基準率へ潰れる。逆に本物の判別力が
育てば a が伸びて生の確度が効き始める。**自己修正する。**

小標本での過信を防ぐため、(a, b) はガウス事前分布へL2縮小する:
    a の事前平均 = 0            (判別力は「無い」から始める)
    b の事前平均 = logit(縮小基準率)  (Beta事前で基準率自体も縮小)

n=22 で実測基準率4.5%をそのまま信じるのは、運を実力と誤認する行為なので、
基準率にも Jeffreys 事前(Beta(0.5,0.5)) を噛ませる。

**評価は必ず out-of-sample。** 同じ標本で当てて同じ標本で採点すれば
どんな較正でも良く見える。loo_brier() は Leave-One-Out で採点する。
"""
import math

EPS = 1e-6
# 事前分布の強さ。ガウス事前の精度(=1/分散)。大きいほど強く縮小する。
# slope を強めに縮めるのは「判別力があるとは主張しない」という保守側の既定。
DEFAULT_L2_SLOPE = 2.0
DEFAULT_L2_INTERCEPT = 1.0
# 基準率の Jeffreys 事前 Beta(0.5, 0.5)
PRIOR_A, PRIOR_B = 0.5, 0.5


def _clamp(p):
    return max(EPS, min(1.0 - EPS, float(p)))


def logit(p):
    p = _clamp(p)
    return math.log(p / (1.0 - p))


def sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def observations(hunches, key="confidence"):
    """確定済みの (生の確度, 実現0/1) を作成日順に取り出す。"""
    out = []
    for h in hunches:
        if h.get("result") not in ("hit", "miss"):
            continue
        raw = h.get(key)
        if raw is None:
            raw = h.get("confidence")
        try:
            c = _clamp(raw)
        except (TypeError, ValueError):
            continue
        out.append((c, 1.0 if h["result"] == "hit" else 0.0))
    return out


def shrunk_base_rate(obs):
    """Beta(0.5,0.5) 事前で縮小した基準率。1/22 をそのまま信じない。"""
    n = len(obs)
    hits = sum(y for _, y in obs)
    return (hits + PRIOR_A) / (n + PRIOR_A + PRIOR_B) if n >= 0 else 0.5


def fit(obs, l2_slope=DEFAULT_L2_SLOPE, l2_intercept=DEFAULT_L2_INTERCEPT):
    """L2縮小つきロジスティック回帰の MAP 推定 (Newton法)。

    戻り: {"a","b","n","base"} / 観測が空なら a=0,b=logit(0.5) の無情報較正。
    """
    n = len(obs)
    base = shrunk_base_rate(obs) if n else 0.5
    b0 = logit(base)
    if n == 0:
        return {"a": 0.0, "b": b0, "n": 0, "base": base}

    xs = [logit(c) for c, _ in obs]
    ys = [y for _, y in obs]
    a, b = 0.0, b0
    for _ in range(100):
        ga = l2_slope * a
        gb = l2_intercept * (b - b0)
        haa, hab, hbb = l2_slope, 0.0, l2_intercept
        for x, y in zip(xs, ys):
            p = sigmoid(a * x + b)
            r = p - y
            w = max(p * (1.0 - p), 1e-9)
            ga += r * x
            gb += r
            haa += w * x * x
            hab += w * x
            hbb += w
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a -= da
        b -= db
        if abs(da) < 1e-10 and abs(db) < 1e-10:
            break
    return {"a": a, "b": b, "n": n, "base": base}


def apply(p_raw, params):
    """生の確度を較正済み確率へ写す。"""
    return sigmoid(params["a"] * logit(p_raw) + params["b"])


def interval(params, z=1.0):
    """較正後確率の不確かさ帯。Beta事後の標準偏差を基準率まわりで使う。

    賭けや意思決定では点推定 p̂ ではなく **下側 p̂ − z·σ̂** を使うべき。
    選択規則は推定誤差が正に振れた対象を狙って選び出すため(winner's curse)、
    点推定のまま閾値と比べると、選ばれるのは自分のノイズになる。
    戻り: (sigma, lower_fn) — lower_fn(p_cal) が下側限界を返す。
    """
    n = params["n"]
    base = params["base"]
    a_post = base * (n + PRIOR_A + PRIOR_B)
    b_post = (1.0 - base) * (n + PRIOR_A + PRIOR_B)
    tot = a_post + b_post
    sigma = math.sqrt(a_post * b_post / (tot * tot * (tot + 1.0))) if tot > 0 else 0.5

    def lower(p_cal):
        return max(0.0, p_cal - z * sigma)

    return sigma, lower


def brier_of(pairs):
    """(確率, 実現) の列から Brier を出す。"""
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def loo_brier(obs, **kw):
    """Leave-One-Out で較正後 Brier を out-of-sample 採点する。

    自分自身を含めて当てた較正で自分を採点すると必ず良く見える。
    1件ずつ抜いて残りで較正を学習し、抜いた1件を予測する。
    """
    n = len(obs)
    if n < 2:
        return None
    pairs = []
    for i in range(n):
        rest = obs[:i] + obs[i + 1:]
        params = fit(rest, **kw)
        pairs.append((apply(obs[i][0], params), obs[i][1]))
    return brier_of(pairs)


def bss(brier, baseline):
    """Brier Skill Score = 1 − Brier/基準線。正なら基準線より情報がある。"""
    if brier is None or not baseline:
        return None
    return 1.0 - brier / baseline


def report(hunches, key="confidence"):
    """較正前後を out-of-sample で比較した要約。判断用の一枚。"""
    obs = observations(hunches, key=key)
    n = len(obs)
    if n < 2:
        return {"n": n}
    raw = brier_of(obs)
    cal = loo_brier(obs)
    base = shrunk_base_rate(obs)
    always50 = brier_of([(0.5, y) for _, y in obs])
    baserate = brier_of([(base, y) for _, y in obs])
    params = fit(obs)
    sigma, _ = interval(params)
    return {"n": n, "brier_raw": raw, "brier_calibrated_loo": cal,
            "brier_always50": always50, "brier_baserate": baserate,
            "base_rate_shrunk": base, "a": params["a"], "b": params["b"],
            "sigma": sigma,
            "bss_raw": bss(raw, always50), "bss_calibrated": bss(cal, always50)}
