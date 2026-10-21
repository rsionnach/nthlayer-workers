import pytest
from nthlayer_common.manifest import (
    JudgmentThreshold,
    ThresholdMeasurement,
    threshold_from_v2_entry,
)

from nthlayer_workers.measure.thresholds.state import (
    REARM_INTERVALS,
    KeyState,
    advance,
    bounds_digest,
    threshold_key,
)


def _seg(values, limit=0.05):
    return JudgmentThreshold("segments", {"maximum_variance_from_overall": limit},
                             ThresholdMeasurement(metric="escalation", segment_dimension="region",
                                                  segment_values=list(values), window="30d"))


def test_key_is_service_type_and_hash():
    k = threshold_key("svc", _seg(["eu", "us"]))
    svc, typ, digest = k.split(":")
    assert (svc, typ, len(digest)) == ("svc", "segments", 12)


def test_segment_order_and_bounds_do_not_change_the_key():
    assert threshold_key("svc", _seg(["eu", "us"])) == threshold_key("svc", _seg(["us", "eu"], limit=0.2))


def test_identifying_fields_change_the_key():
    assert threshold_key("svc", _seg(["eu"])) != threshold_key("svc", _seg(["us"]))
    assert threshold_key("a", _seg(["eu"])) != threshold_key("b", _seg(["eu"]))


def test_identical_entries_share_a_key():
    assert threshold_key("svc", _seg(["eu"])) == threshold_key("svc", _seg(["eu"]))


def test_entries_bounding_different_quantiles_get_different_keys():
    # CORE §5.5: several entries of one type are allowed; a p50 entry and a
    # p90 entry must both be evaluated.
    p50 = JudgmentThreshold("feedback_latency", {"maximum_p50": "1h"})
    p90 = JudgmentThreshold("feedback_latency", {"maximum_p90": "1d"})
    assert threshold_key("svc", p50) != threshold_key("svc", p90)


def test_metric_parameters_change_the_key():
    a = JudgmentThreshold("segments", {"maximum_variance_from_overall": 0.05},
                          ThresholdMeasurement(metric="reversal_rate", source="lineage", segment_dimension="r",
                                               segment_values=["eu"], window="30d"))
    b = JudgmentThreshold("segments", {"maximum_variance_from_overall": 0.05},
                          ThresholdMeasurement(metric="reversal_rate", source="downstream_signal",
                                               segment_dimension="r", segment_values=["eu"], window="30d"))
    assert threshold_key("svc", a) != threshold_key("svc", b)


def test_bounds_digest_depends_on_values_not_order():
    a = JudgmentThreshold("calibration", {"maximum_brier_score": 0.1, "maximum_expected_calibration_error": 0.05})
    b = JudgmentThreshold("calibration", {"maximum_expected_calibration_error": 0.05, "maximum_brier_score": 0.1})
    c = JudgmentThreshold("calibration", {"maximum_brier_score": 0.2, "maximum_expected_calibration_error": 0.05})
    assert bounds_digest(a) == bounds_digest(b) != bounds_digest(c)
    assert len(bounds_digest(a)) == 8


INTERVAL = 900.0
REARM = REARM_INTERVALS * INTERVAL  # 2700s, what MeasureModule passes by default


def _run(statuses, interval=INTERVAL):
    """One status per threshold interval, starting at t=0."""
    st, fired = KeyState(), []
    for i, s in enumerate(statuses):
        st, f = advance(st, s, i * interval, REARM)
        fired.append(f)
    return fired


def test_first_breach_fires_once():
    assert _run(["breach", "breach"]) == [True, False]


def test_not_evaluable_gap_does_not_refire():
    assert _run(["breach", "not_evaluable", "breach"]) == [True, False, False]


def test_within_for_less_than_the_rearm_duration_does_not_rearm():
    # within at 900, 1800, 2700: the run is 1800s old at 2700 (< 2700).
    assert _run(["breach", "within", "within", "within", "breach"]) == [True, False, False, False, False]


def test_within_for_the_rearm_duration_rearms():
    # within from 900 to 3600: 2700s >= REARM, so the breach at 4500 fires.
    assert _run(["breach", "within", "within", "within", "within", "breach"]) == [
        True, False, False, False, False, True]
    assert REARM_INTERVALS == 3


def test_rearm_needs_both_the_duration_and_three_within_evaluations():
    # Far apart: two evaluations 2700s apart are not enough; the third is.
    st, _ = advance(KeyState(), "breach", 0.0, REARM)
    st, _ = advance(st, "within", 100.0, REARM)
    st, _ = advance(st, "within", 2800.0, REARM)
    assert not st.armed
    st, _ = advance(st, "within", 2900.0, REARM)
    assert st.armed
    # Close together: four evaluations within 300s are not enough.
    st, _ = advance(KeyState(), "breach", 0.0, REARM)
    for t in (100.0, 200.0, 300.0, 400.0):
        st, _ = advance(st, "within", t, REARM)
    assert not st.armed


def test_a_run_starting_in_the_future_restarts_now():
    # The clock went back: the run restarts instead of never re-arming.
    st, _ = advance(KeyState(), "breach", 10_000.0, REARM)
    st, _ = advance(st, "within", 10_900.0, REARM)
    for t in (100.0, 1000.0, 1900.0, 2800.0):
        st, _ = advance(st, "within", t, REARM)
    assert (st.within_since, st.within_count, st.armed) == (100.0, 4, True)


def test_flapping_does_not_refire_until_rearmed():
    # A breach ends the within run: flapping restarts the clock each time.
    assert _run(["breach", "within", "breach", "within", "within", "within", "within", "breach"]) == [
        True, False, False, False, False, False, False, True]


def test_not_evaluable_neither_starts_nor_ends_a_within_run():
    st, _ = advance(KeyState(), "breach", 0.0, REARM)
    st, _ = advance(st, "within", 900.0, REARM)
    st, _ = advance(st, "not_evaluable", 1800.0, REARM)
    assert (st.within_since, st.within_count) == (900.0, 1)
    st, _ = advance(st, "within", 2700.0, REARM)
    st, _ = advance(st, "within", 3600.0, REARM)
    assert st.armed  # 3600 - 900 = 2700, and 3 within evaluations
    st, _ = advance(KeyState(), "not_evaluable", 0.0, REARM)
    assert st.within_since is None


def test_not_evaluable_alone_never_fires():
    assert _run(["not_evaluable", "not_evaluable"]) == [False, False]


def test_within_then_breach_fires():
    assert _run(["within", "breach"]) == [False, True]


def test_restored_armed_breach_does_not_refire():
    assert advance(KeyState("breach", True, None), "breach", 0.0, REARM)[1] is False


def test_state_round_trip():
    st, _ = advance(KeyState(), "within", 5.0, REARM)
    assert KeyState.from_dict(st.to_dict()) == st


@pytest.mark.parametrize("raw,expected", [
    (None, KeyState()),
    ("garbage", KeyState()),
    ({"last_evaluable": 7, "armed": "no", "within_since": "x"}, KeyState()),
    ({"last_evaluable": "breach", "armed": 0, "within_since": True}, KeyState("breach", True, None)),
    ({"last_evaluable": "within", "armed": False, "within_since": 5, "within_count": True},
     KeyState("within", False, 5.0, 0)),
    ({"last_evaluable": "within", "armed": False, "within_since": 5, "within_count": -1},
     KeyState("within", False, 5.0, 0)),
    ({"last_evaluable": "within", "armed": False, "within_since": 5, "within_count": 2},
     KeyState("within", False, 5.0, 2)),
    ({"last_evaluable": "within", "armed": False, "within_since": float("nan")}, KeyState("within", False, None)),
    ({"last_evaluable": "within", "armed": False, "within_since": 12, "service": "svc"},
     KeyState("within", False, 12.0)),
    # An int too large for a float is not a timestamp; it must not raise.
    ({"last_evaluable": "within", "armed": False, "within_since": 10**400}, KeyState("within", False, None)),
    ({"last_evaluable": "within", "armed": False, "within_since": -(10**400)}, KeyState("within", False, None)),
    # A non-negative int is a valid count however large.
    ({"last_evaluable": "within", "armed": False, "within_since": 5, "within_count": 10**400},
     KeyState("within", False, 5.0, 10**400)),
    # State written before within_since existed.
    ({"last_evaluable": "within", "armed": False, "within_streak": 2}, KeyState("within", False, None)),
])
def test_from_dict_tolerates_a_corrupt_or_old_restored_state(raw, expected):
    assert KeyState.from_dict(raw) == expected


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError, match="bogus"):
        advance(KeyState(), "bogus", 0.0, REARM)


def test_duplicate_segment_values_do_not_change_the_key():
    assert threshold_key("svc", _seg(["eu", "eu"])) == threshold_key("svc", _seg(["eu"]))


# --- Identity key: every identifying field counts (design §7 item 3) --------
# Fixtures are v2 judgment_thresholds entries as the opensrm schema defines
# them (spec/v2/schema.json, CORE §5.5), parsed by nthlayer-common's own
# parser, so each pair is one a real manifest can declare. Each pair differs
# in exactly one identifying field.

def _parsed(entry):
    return threshold_from_v2_entry(entry)


def _segments(metric="escalation", dimension="region", window="30d", **extra):
    entry = {"type": "segments", "metric": metric, "segment_dimension": dimension,
             "segment_values": ["eu", "us"], "measurement": {"window": window},
             "bounds": {"maximum_variance_from_overall": 0.05}}
    for k, v in extra.items():
        if k in ("source", "confidence_threshold"):
            entry["measurement"][k] = v
        else:
            entry[k] = v
    return entry


def _calibration(bins=10, window="30d"):
    return {"type": "calibration", "measurement": {"bins": bins, "window": window},
            "bounds": {"maximum_expected_calibration_error": 0.05}}


def _stability(frozen_input_set="s3://probes/a", frequency="daily"):
    return {"type": "stability", "probe": {"frozen_input_set": frozen_input_set, "frequency": frequency},
            "bounds": {"maximum_drift": 0.02}}


@pytest.mark.parametrize("field,a,b", [
    ("confidence_threshold",
     _segments("high_confidence_failure", confidence_threshold=0.9),
     _segments("high_confidence_failure", confidence_threshold=0.8)),
    ("outcome_signal.source",
     _segments("outcomes", outcome_signal={"source": "refunds", "window": "7d"}),
     _segments("outcomes", outcome_signal={"source": "chargebacks", "window": "7d"})),
    ("outcome_signal.window",
     _segments("outcomes", outcome_signal={"source": "refunds", "window": "7d"}),
     _segments("outcomes", outcome_signal={"source": "refunds", "window": "14d"})),
    ("segment_dimension", _segments(dimension="region"), _segments(dimension="tier")),
    ("probe.frozen_input_set", _stability(frozen_input_set="s3://probes/a"),
     _stability(frozen_input_set="s3://probes/b")),
    ("probe.frequency", _stability(frequency="daily"), _stability(frequency="weekly")),
    ("segments window", _segments(window="30d"), _segments(window="7d")),
    ("calibration window", _calibration(window="30d"), _calibration(window="7d")),
    ("bins", _calibration(bins=10), _calibration(bins=15)),
])
def test_each_identifying_field_changes_the_key(field, a, b):
    ta, tb = _parsed(a), _parsed(b)
    assert ta.type == tb.type and ta.bounds == tb.bounds   # only `field` differs
    assert threshold_key("svc", ta) != threshold_key("svc", tb)


def test_bound_declaration_order_does_not_change_the_key():
    # A mapping's key order carries no meaning in YAML/JSON: the same entry
    # written with its bounds the other way round is the same entry.
    ece_first = _parsed({"type": "calibration", "measurement": {"bins": 10, "window": "30d"},
                         "bounds": {"maximum_expected_calibration_error": 0.05, "maximum_brier_score": 0.1}})
    brier_first = _parsed({"type": "calibration", "measurement": {"bins": 10, "window": "30d"},
                           "bounds": {"maximum_brier_score": 0.1, "maximum_expected_calibration_error": 0.05}})
    assert list(ece_first.bounds) != list(brier_first.bounds)   # non-vacuous: orders differ
    assert threshold_key("svc", ece_first) == threshold_key("svc", brier_first)


def test_a_breach_ends_the_within_run():
    # design §7: "a breach ends it". within, within, breach, within: the run
    # restarts at the last within, so it is one evaluation old, not three.
    st, _ = advance(KeyState(), "breach", 0.0, REARM)
    st, _ = advance(st, "within", 900.0, REARM)
    st, _ = advance(st, "within", 1800.0, REARM)
    st, _ = advance(st, "breach", 2700.0, REARM)
    assert (st.within_since, st.within_count) == (None, 0)
    st, _ = advance(st, "within", 3600.0, REARM)
    assert (st.within_since, st.within_count, st.armed) == (3600.0, 1, False)
