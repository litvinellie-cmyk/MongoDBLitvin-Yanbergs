from datetime import datetime, timedelta, timezone

from rules import decide, merged_settings

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
S = merged_settings({"target_cpl": 40, "daily_spend_cap": 300})


def camp(**kw):
    base = {"campaign_id": "c1", "status": "ACTIVE", "spend": 700, "conversions": 10,
            "clicks": 900, "daily_budget": 100}
    base.update(kw)
    return base


def run(c, s=S, edits=0, last=None, total=200):
    return decide(c, s, edits_today=edits, last_edit_at=last, now=NOW, account_budget_total=total)


def test_expensive_campaign_cut_needs_approval():
    a = run(camp())  # CPL 70 vs 40
    assert a.type == "set_budget" and a.new == 85.0 and a.size_pct == 15.0
    assert a.auto is False  # 15% > 10% auto limit
    assert "$70.00" in a.reason


def test_small_move_runs_automatically():
    a = run(camp(), s={**S, "max_change_pct": 10})
    assert a.new == 90.0 and a.auto is True


def test_cheap_campaign_scaled_up_within_cap():
    a = run(camp(spend=200, conversions=10), total=290)  # CPL 20, only $10 headroom
    assert a.new == 110.0


def test_no_headroom_no_increase():
    assert run(camp(spend=200, conversions=10), total=300) is None


def test_inside_band_does_nothing():
    assert run(camp(spend=400, conversions=10)) is None


def test_small_sample_does_nothing():
    assert run(camp(spend=90, conversions=1)) is None
    assert run(camp(spend=500, conversions=4)) is None


def test_zero_leads_proposes_pause():
    a = run(camp(spend=150, conversions=0))
    assert a.type == "pause" and a.auto is False


def test_frequency_cap_blocks():
    assert run(camp(), edits=1) is None
    assert run(camp(), last=NOW - timedelta(hours=5)) is None


def test_paused_campaign_ignored():
    assert run(camp(status="PAUSED")) is None


def test_manual_mode_never_auto():
    a = run(camp(), s={**S, "max_change_pct": 5, "auto_mode": False})
    assert a.auto is False
