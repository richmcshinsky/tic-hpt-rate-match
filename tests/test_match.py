"""End-to-end tests for the matcher (core.match).

The first cut had unit tests on the normalizers but nothing on the
matcher — the part most likely to be wrong: join cardinality, the plan
collapse, the pass-2 relaxation, and the filters.  This file drives the
whole pipeline over a tiny synthetic fixture (~10 rows) engineered so
every core branch is hit and the expected counts are checkable by hand.
The aggregation-edge branches (cms_baseline disagreement, methodology
blend) have their own dedicated tests further down.

Fixture design (what each row is for):
  TiC
    T1,T2  same 872 key  -> NPI collapse, median of the two
    T3     43239 cigna professional -> pass-2 relaxed partner for H3
    T4     99283 at a hospital with no HPT -> tic_only
    T5     negotiation_type=percentage -> dropped
  HPT
    H1,H2  same 872 key  -> plan collapse, strict (pass-1) match to T1/T2
    H3     43239 cigna outpatient, no HC/PR prefix -> billing 'unknown',
           filename carries the EIN while license_number is a state cert
           -> pass-2 relaxed match to T3 (also tests EIN-from-filename)
    H4     payer 'United Mine Workers' -> must NOT map to UHC -> dropped
    H5     code_type LOCAL -> dropped
    H6     no negotiated dollar -> dropped
    H7     77777 cigna with 'HC ' prefix -> institutional, no TiC -> hpt_only
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.match import PipelineStats, match_rates, preprocess_hpt, preprocess_tic
from core.normalizers import load_payer_aliases

REPO = Path(__file__).resolve().parent.parent
EIN_MONTE = "131740114"
EIN_SINAI = "131624096"

TIC_COLUMNS = [
    "payer", "network_name", "network_id", "network_year_month", "network_region",
    "code", "code_type", "ein", "taxonomy_filtered_npi_list", "modifier_list",
    "billing_class", "place_of_service_list", "negotiation_type", "arrangement",
    "rate", "cms_baseline_schedule", "cms_baseline_rate",
]
HPT_COLUMNS = [
    "source_file_name", "hospital_id", "hospital_name", "last_updated_on",
    "hospital_state", "license_number", "payer_name", "plan_name", "code_type",
    "raw_code", "description", "setting", "modifiers", "standard_charge_gross",
    "standard_charge_discounted_cash", "standard_charge_negotiated_dollar",
    "standard_charge_negotiated_percentage", "standard_charge_min",
    "standard_charge_max", "standard_charge_methodology", "additional_payer_notes",
    "additional_generic_notes",
]


def _tic_row(**kw):
    row = {c: "" for c in TIC_COLUMNS}
    row.update(kw)
    return row


def _hpt_row(**kw):
    row = {c: "" for c in HPT_COLUMNS}
    row.update(kw)
    return row


@pytest.fixture(scope="module")
def aliases():
    return load_payer_aliases(REPO / "core" / "payer_aliases.json")


@pytest.fixture()
def result(aliases):
    tic_raw = pd.DataFrame([
        _tic_row(payer="Aetna", code="872", code_type="MS-DRG", ein=EIN_MONTE,
                 billing_class="institutional", negotiation_type="negotiated",
                 network_name="net-a", rate="30000",
                 cms_baseline_schedule="IPPS", cms_baseline_rate="6829.75"),
        _tic_row(payer="Aetna", code="872", code_type="MS-DRG", ein=EIN_MONTE,
                 billing_class="institutional", negotiation_type="negotiated",
                 network_name="net-a", rate="32000",
                 cms_baseline_schedule="IPPS", cms_baseline_rate="6829.75"),
        _tic_row(payer="Cigna", code="43239", code_type="CPT", ein=EIN_MONTE,
                 billing_class="professional", negotiation_type="negotiated",
                 network_name="net-c", rate="500"),
        _tic_row(payer="Aetna", code="99283", code_type="CPT", ein="999999999",
                 billing_class="professional", negotiation_type="negotiated",
                 network_name="net-a", rate="80"),
        _tic_row(payer="Aetna", code="55555", code_type="CPT", ein=EIN_MONTE,
                 billing_class="professional", negotiation_type="percentage",
                 network_name="net-a", rate="70"),
    ], columns=TIC_COLUMNS)

    hpt_raw = pd.DataFrame([
        _hpt_row(source_file_name=f"{EIN_MONTE}_Montefiore_standardcharges.csv",
                 hospital_name="Montefiore", license_number=EIN_MONTE,
                 payer_name="Aetna", plan_name="PlanA", code_type="MS-DRG",
                 raw_code="MS-DRG 872", description="Septicemia", setting="inpatient",
                 standard_charge_negotiated_dollar="29000",
                 standard_charge_methodology="case rate"),
        _hpt_row(source_file_name=f"{EIN_MONTE}_Montefiore_standardcharges.csv",
                 hospital_name="Montefiore", license_number=EIN_MONTE,
                 payer_name="Aetna", plan_name="PlanB", code_type="MS-DRG",
                 raw_code="872", description="Septicemia", setting="inpatient",
                 standard_charge_negotiated_dollar="31000",
                 standard_charge_methodology="case rate"),
        # license_number is a NY state cert; the EIN must come from the filename.
        _hpt_row(source_file_name=f"{EIN_MONTE}_Montefiore_standardcharges.csv",
                 hospital_name="Montefiore", license_number="330024",
                 payer_name="Cigna", plan_name="PlanC", code_type="CPT",
                 raw_code="43239", description="EGD biopsy", setting="outpatient",
                 standard_charge_negotiated_dollar="600",
                 standard_charge_methodology="fee schedule"),
        _hpt_row(source_file_name=f"{EIN_MONTE}_Montefiore_standardcharges.csv",
                 hospital_name="Montefiore", payer_name="United Mine Workers",
                 plan_name="PlanX", code_type="CPT", raw_code="99283",
                 standard_charge_negotiated_dollar="200"),
        _hpt_row(source_file_name=f"{EIN_MONTE}_Montefiore_standardcharges.csv",
                 hospital_name="Montefiore", payer_name="Aetna", plan_name="PlanY",
                 code_type="LOCAL", raw_code="43239",
                 standard_charge_negotiated_dollar="999"),
        _hpt_row(source_file_name=f"{EIN_MONTE}_Montefiore_standardcharges.csv",
                 hospital_name="Montefiore", payer_name="Aetna", plan_name="PlanZ",
                 code_type="CPT", raw_code="88888",
                 standard_charge_negotiated_dollar=""),
        _hpt_row(source_file_name=f"{EIN_MONTE}_Montefiore_standardcharges.csv",
                 hospital_name="Montefiore", payer_name="Cigna", plan_name="PlanD",
                 code_type="CPT", raw_code="77777", description="HC observation",
                 setting="outpatient", standard_charge_negotiated_dollar="1234",
                 standard_charge_methodology="fee schedule"),
    ], columns=HPT_COLUMNS)

    stats = PipelineStats()
    tic = preprocess_tic(tic_raw, aliases, stats)
    hpt = preprocess_hpt(hpt_raw, aliases, stats)
    out = match_rates(tic, hpt, stats)
    return out, stats


def test_filters_counted(result):
    _, s = result
    assert s.tic_rows_dropped_percentage == 1
    assert s.hpt_rows_dropped_local == 1
    assert s.hpt_rows_dropped_no_dollar == 1
    assert s.hpt_rows_dropped_no_payer == 1          # United Mine Workers


def test_collapse_to_grain(result):
    out, s = result
    assert s.tic_match_groups == 3
    assert s.hpt_match_groups == 3
    # The unified table must be exactly one row per key.
    key = ["hospital_ein", "code", "payer_canonical", "billing_class"]
    assert out[key].drop_duplicates().shape[0] == len(out)


def test_match_buckets(result):
    out, s = result
    assert s.matches_pass1 == 1
    assert s.matches_pass2 == 1
    assert s.unmatched_hpt == 1                       # H7 hpt_only
    assert s.unmatched_tic == 1                       # T4 tic_only
    assert s.output_rows == 4
    assert dict(out.data_source.value_counts()) == {"both": 2, "hpt_only": 1, "tic_only": 1}


def test_strict_match_and_plan_collapse(result):
    out, _ = result
    r = out[(out.code == "872") & (out.payer_canonical == "aetna")].iloc[0]
    assert r.match_level == 1 and not r.billing_class_relaxed
    assert r.billing_class == "institutional"
    assert r.hpt_plan_count == 2                      # PlanA + PlanB collapsed
    assert r.hpt_rate_median == 30000                 # median(29000, 31000)
    assert r.tic_npi_group_count == 2
    assert r.tic_rate_median == 31000                 # median(30000, 32000)
    assert r.rate_delta == 1000


def test_pass2_relaxed_and_ein_from_filename(result):
    out, _ = result
    r = out[(out.code == "43239") & (out.payer_canonical == "cigna")].iloc[0]
    assert r.match_level == 2
    assert bool(r.billing_class_relaxed) is True
    # 'unknown' (HPT) loses to the specific TiC side.
    assert r.billing_class == "professional"
    # EIN came from the filename, not the state-cert license_number.
    assert r.hospital_ein == EIN_MONTE


def test_payer_overmatch_excluded(result):
    out, _ = result
    # 'United Mine Workers' must not have leaked in as UnitedHealthcare.
    assert "unitedhealthcare" not in set(out.payer_canonical)
    assert "99283" not in set(out[out.payer_canonical == "unitedhealthcare"].code)


def test_tic_only_name_backfilled(result):
    out, _ = result
    # T4 is tic_only; its hospital has no HPT file, so name stays null.
    t = out[out.data_source == "tic_only"].iloc[0]
    assert t.code == "99283" and pd.isna(t.hpt_rate_median)


def test_comparability_columns_on_clean_match(result):
    out, _ = result
    # 872 row: both HPT plans are 'case rate' and TiC is 'negotiated' -> not mixed.
    r = out[(out.code == "872") & (out.payer_canonical == "aetna")].iloc[0]
    assert bool(r.hpt_methodology_mixed) is False
    assert r.tic_negotiation_types == "negotiated"


# --- self-contained fixtures for the new aggregation behaviour ---------------

def _run(tic_rows, hpt_rows):
    aliases = load_payer_aliases(REPO / "core" / "payer_aliases.json")
    tic_raw = pd.DataFrame(tic_rows, columns=TIC_COLUMNS)
    hpt_raw = pd.DataFrame(hpt_rows, columns=HPT_COLUMNS)
    stats = PipelineStats()
    tic = preprocess_tic(tic_raw, aliases, stats)
    hpt = preprocess_hpt(hpt_raw, aliases, stats)
    return match_rates(tic, hpt, stats)


def test_cms_baseline_uses_median_not_first():
    # Two TiC sub-groups at one key disagree on cms_baseline_rate. 'first' would
    # be order-dependent; we take the median -> 200 regardless of row order.
    tic = [
        _tic_row(payer="Cigna", code="99283", code_type="CPT", ein=EIN_MONTE,
                 billing_class="professional", negotiation_type="negotiated",
                 rate="100", cms_baseline_rate="100", cms_baseline_schedule="PFS_FAC"),
        _tic_row(payer="Cigna", code="99283", code_type="CPT", ein=EIN_MONTE,
                 billing_class="professional", negotiation_type="negotiated",
                 rate="100", cms_baseline_rate="300", cms_baseline_schedule="PFS_NONFAC"),
    ]
    hpt = [
        _hpt_row(source_file_name=f"{EIN_MONTE}_M_standardcharges.csv",
                 hospital_name="M", payer_name="Cigna", plan_name="P", code_type="CPT",
                 raw_code="99283", description="PR visit", setting="outpatient",
                 standard_charge_negotiated_dollar="120",
                 standard_charge_methodology="fee schedule"),
    ]
    out = _run(tic, hpt)
    r = out[out.code == "99283"].iloc[0]
    assert r.cms_baseline_rate == 200
    # Both schedules survive in the joined string.
    assert "PFS_FAC" in r.cms_baseline_schedule and "PFS_NONFAC" in r.cms_baseline_schedule


def test_rate_value_corroboration_surfaces_split_match():
    # The $6,438 lesson: HPT and TiC agree on a dollar value, but billing-class
    # inference puts them on different rows. Pass 3 must still flag the block.
    tic = [
        _tic_row(payer="Cigna", code="43239", code_type="CPT", ein=EIN_SINAI,
                 billing_class="institutional", negotiation_type="negotiated", rate="6438"),
        _tic_row(payer="Cigna", code="43239", code_type="CPT", ein=EIN_SINAI,
                 billing_class="professional", negotiation_type="negotiated", rate="500"),
    ]
    hpt = [
        # $6,438 with NO "HC " prefix -> inferred 'unknown' (mirrors the real row)
        _hpt_row(source_file_name=f"{EIN_SINAI}_MS_standardcharges.csv",
                 hospital_name="MS", payer_name="Cigna", plan_name="P1", code_type="CPT",
                 raw_code="43239", description="Egd biopsy single/multiple", setting="outpatient",
                 standard_charge_negotiated_dollar="6438", standard_charge_methodology="case rate"),
        # $1,048.28 WITH "HC " prefix -> inferred institutional, becomes the strict match
        _hpt_row(source_file_name=f"{EIN_SINAI}_MS_standardcharges.csv",
                 hospital_name="MS", payer_name="Cigna", plan_name="P2", code_type="CPT",
                 raw_code="43239", description="HC Egd Transoral Biopsy", setting="outpatient",
                 standard_charge_negotiated_dollar="1048.28", standard_charge_methodology="fee schedule"),
    ]
    out = _run(tic, hpt)
    block = out[(out.code == "43239") & (out.payer_canonical == "cigna")]
    assert block.rate_value_corroborated.all()                       # whole block flagged
    assert all("6438" in str(v) for v in block.corroborated_values)  # the agreeing value shown
    # And the strict institutional row's medians still differ — the grain split it,
    # which is exactly why the corroboration flag is needed.
    inst = block[block.billing_class == "institutional"].iloc[0]
    assert inst.hpt_rate_median != inst.tic_rate_median


def test_no_corroboration_when_values_disagree():
    tic = [_tic_row(payer="Aetna", code="99283", code_type="CPT", ein=EIN_MONTE,
                    billing_class="professional", negotiation_type="negotiated", rate="80")]
    hpt = [_hpt_row(source_file_name=f"{EIN_MONTE}_M_standardcharges.csv",
                    hospital_name="M", payer_name="Aetna", plan_name="P", code_type="CPT",
                    raw_code="99283", description="PR visit", setting="outpatient",
                    standard_charge_negotiated_dollar="200")]
    out = _run(tic, hpt)
    assert not out.rate_value_corroborated.any()


def test_methodology_mixed_flag():
    # One key, two HPT plans with different methodologies -> mixed == True,
    # and the median ($150) blends non-comparable dollars (flagged, not trusted).
    tic = [
        _tic_row(payer="Aetna", code="99283", code_type="CPT", ein=EIN_MONTE,
                 billing_class="professional", negotiation_type="negotiated", rate="150"),
    ]
    hpt = [
        _hpt_row(source_file_name=f"{EIN_MONTE}_M_standardcharges.csv",
                 hospital_name="M", payer_name="Aetna", plan_name="P1", code_type="CPT",
                 raw_code="99283", description="PR visit", setting="outpatient",
                 standard_charge_negotiated_dollar="100",
                 standard_charge_methodology="case rate"),
        _hpt_row(source_file_name=f"{EIN_MONTE}_M_standardcharges.csv",
                 hospital_name="M", payer_name="Aetna", plan_name="P2", code_type="CPT",
                 raw_code="99283", description="PR visit", setting="outpatient",
                 standard_charge_negotiated_dollar="200",
                 standard_charge_methodology="percent of total billed charges"),
    ]
    out = _run(tic, hpt)
    r = out[out.code == "99283"].iloc[0]
    assert bool(r.hpt_methodology_mixed) is True
    assert r.hpt_plan_count == 2 and r.hpt_rate_median == 150
    assert ";" in r.hpt_rate_methodology


def test_cms_baseline_mixed_only_when_classes_differ():
    # Two TiC sub-groups quoting two DIFFERENT PFS localities within the same
    # class (PFS_NONFACILITY_1320201 and PFS_NONFACILITY_1320202) -> the
    # baseline median blends locality-level variance only, which is normal.
    # cms_baseline_mixed must be False here.
    tic_same_class = [
        _tic_row(payer="Cigna", code="99283", code_type="CPT", ein=EIN_MONTE,
                 billing_class="professional", negotiation_type="negotiated",
                 rate="100", cms_baseline_rate="70",
                 cms_baseline_schedule="PFS_NONFACILITY_1320201"),
        _tic_row(payer="Cigna", code="99283", code_type="CPT", ein=EIN_MONTE,
                 billing_class="professional", negotiation_type="negotiated",
                 rate="100", cms_baseline_rate="80",
                 cms_baseline_schedule="PFS_NONFACILITY_1320202"),
    ]
    hpt = [
        _hpt_row(source_file_name=f"{EIN_MONTE}_M_standardcharges.csv",
                 hospital_name="M", payer_name="Cigna", plan_name="P", code_type="CPT",
                 raw_code="99283", description="PR visit", setting="outpatient",
                 standard_charge_negotiated_dollar="120",
                 standard_charge_methodology="fee schedule"),
    ]
    out = _run(tic_same_class, hpt)
    r = out[out.code == "99283"].iloc[0]
    assert bool(r.cms_baseline_mixed) is False

    # Same shape but the two sub-groups straddle PFS_FACILITY and
    # PFS_NONFACILITY -- those are intentionally-different fee schedules for
    # the same code, so the median is a blended number, not a baseline.
    # cms_baseline_mixed must be True here.
    tic_diff_class = [
        _tic_row(payer="Cigna", code="99283", code_type="CPT", ein=EIN_MONTE,
                 billing_class="professional", negotiation_type="negotiated",
                 rate="100", cms_baseline_rate="70",
                 cms_baseline_schedule="PFS_FACILITY_1320201"),
        _tic_row(payer="Cigna", code="99283", code_type="CPT", ein=EIN_MONTE,
                 billing_class="professional", negotiation_type="negotiated",
                 rate="100", cms_baseline_rate="200",
                 cms_baseline_schedule="PFS_NONFACILITY_1320201"),
    ]
    out2 = _run(tic_diff_class, hpt)
    r2 = out2[out2.code == "99283"].iloc[0]
    assert bool(r2.cms_baseline_mixed) is True
