from datetime import datetime
from zoneinfo import ZoneInfo

import recorder

JST = ZoneInfo("Asia/Tokyo")
RECS_CONFIRMED = [{"id": "r0", "certainty": "confirmed"}]


def _mk(**kw):
    base = {"subject": "OpenAI", "claim": "OpenAIが新機能をGA提供する",
            "resolution": {"decider": "公式サイトで新機能のGA開始が告知される"},
            "based_on": [0], "deadline_days": 10}
    base.update(kw)
    return base


def test_passes_verifiable():
    assert recorder.validate_hunch(_mk(), RECS_CONFIRMED, datetime.now(JST)) is None


def test_rejects_unverifiable_secret_prediction():
    h = _mk(claim="OpenAIが裏で特別枠をこっそり売る", resolution={"decider": "非公開で提携が結ばれる"})
    r = recorder.validate_hunch(h, RECS_CONFIRMED, datetime.now(JST))
    assert r is not None and "公開情報" in r


def test_rejects_vague_decider():
    h = _mk(resolution={"decider": "広く話題になる"})
    assert recorder.validate_hunch(h, RECS_CONFIRMED, datetime.now(JST)) is not None


def test_rejects_deadline_out_of_range():
    assert recorder.validate_hunch(_mk(deadline_days=100), RECS_CONFIRMED, datetime.now(JST)) is not None
    assert recorder.validate_hunch(_mk(deadline_days=1), RECS_CONFIRMED, datetime.now(JST)) is not None


def test_rejects_missing_subject():
    assert recorder.validate_hunch(_mk(subject=""), RECS_CONFIRMED, datetime.now(JST)) is not None


def test_rejects_rumor_only_basis():
    recs = [{"id": "r0", "certainty": "rumor"}]
    assert recorder.validate_hunch(_mk(), recs, datetime.now(JST)) is not None


def test_coerce_deadline_days():
    assert recorder.coerce_deadline_days({"deadline_days": 10}) == 10
    assert recorder.coerce_deadline_days({"deadline_days": "12"}) == 12
    assert recorder.coerce_deadline_days({"deadline_days": True}) is None
    assert recorder.coerce_deadline_days({"deadline_days": "x"}) is None
