"""較正層のテスト。

要点は「判別力が無いデータでは基準率へ潰れ、あるデータでは生の確度が効き始める」
という自己修正性と、**out-of-sample で評価している**ことの2点。
"""
import math
import calibration as C


def _h(conf, hit):
    return {"confidence": conf, "result": "hit" if hit else "miss"}


def test_no_discrimination_collapses_to_base_rate():
    """確度と的中に関係が無いなら slope→0 で出力は基準率へ潰れる。"""
    obs = [(0.8, 1.0), (0.8, 0.0), (0.2, 1.0), (0.2, 0.0)] * 5
    p = C.fit(obs)
    assert abs(p["a"]) < 0.2                      # 判別力を主張しない
    lo, hi = C.apply(0.05, p), C.apply(0.95, p)
    assert abs(lo - hi) < 0.10                    # 生の確度でほとんど動かない


def test_shrinkage_tames_small_sample_separation():
    """4件で完全分離していても、縮小が効いて極端な確信へ飛ばない。

    L2縮小を外すと最尤推定は slope→∞ へ発散し、0.9 の予測がほぼ1.0になる。
    小標本の「たまたま揃った」を実力と誤認しないための防波堤。
    """
    obs = [(0.9, 1.0), (0.8, 1.0), (0.2, 0.0), (0.1, 0.0)]
    p = C.fit(obs)
    assert p["a"] < 3.0                           # 発散していない
    assert C.apply(0.9, p) < 0.95                 # 極端な確信を出さない


def test_real_discrimination_grows_slope():
    """本物の判別力があれば slope が正に伸びる(自己修正)。"""
    obs = [(0.9, 1.0)] * 40 + [(0.1, 0.0)] * 40
    p = C.fit(obs)
    assert p["a"] > 0.5
    assert C.apply(0.9, p) > C.apply(0.1, p) + 0.3


def test_base_rate_is_shrunk_toward_half():
    """1/22 をそのまま信じない。縮小基準率は素の実測より0.5側。"""
    obs = [(0.7, 1.0)] + [(0.7, 0.0)] * 21
    base = C.shrunk_base_rate(obs)
    assert base > 1.0 / 22
    assert base < 0.5


def test_loo_is_out_of_sample():
    """LOOは自分を含めずに較正する。過適合するデータでは in-sample より明確に悪い。

    in-sample で採点すると「自分を当てるために学習した較正」で自分を採点するので
    必ず良く見える。ここが同値になったら out-of-sample になっていない。
    """
    obs = [(0.9, 1.0), (0.8, 1.0), (0.2, 0.0), (0.1, 0.0)]
    p_all = C.fit(obs)
    in_sample = C.brier_of([(C.apply(c, p_all), y) for c, y in obs])
    loo = C.loo_brier(obs)
    assert loo > in_sample + 0.05                 # 甘さが剥がれる


def test_lower_bound_is_below_point_estimate():
    """選択に使うのは点推定ではなく下側限界。必ず下に来る。"""
    obs = [(0.7, 1.0)] + [(0.7, 0.0)] * 21
    p = C.fit(obs)
    sigma, lower = C.interval(p)
    cal = C.apply(0.7, p)
    assert sigma > 0
    assert lower(cal) < cal


def test_bss_sign():
    """基準線より良ければ正、悪ければ負。"""
    assert C.bss(0.10, 0.25) > 0
    assert C.bss(0.40, 0.25) < 0


def test_empty_and_single_are_safe():
    """観測が無くても落ちない。無情報較正へ退避する。"""
    p = C.fit([])
    assert p["n"] == 0 and abs(C.apply(0.5, p) - 0.5) < 1e-6
    assert C.loo_brier([]) is None
    assert C.report([])["n"] == 0


def test_report_on_broken_calibration_improves():
    """参謀の実測に似た形(高確度・低的中)では較正でBrierが大きく下がる。"""
    hunches = [_h(0.8, False)] * 21 + [_h(0.8, True)]
    r = C.report(hunches)
    assert r["brier_raw"] > 0.5
    assert r["brier_calibrated_loo"] < 0.10
    assert r["bss_calibrated"] > 0
