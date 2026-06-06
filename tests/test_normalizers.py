"""Unit tests for core.normalizers.

These tests focus on the edge cases that actually matter for this extract;
they are not aiming for 100% branch coverage.  Each test references the
real-world fact it is anchored on so a reviewer can audit why it exists.

Run with:
    pytest tests/ -v
or:
    python -m unittest tests.test_normalizers -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.normalizers import (
    ein_from_hpt_source_file,
    infer_hpt_billing_class,
    load_payer_aliases,
    normalize_code,
    normalize_code_type,
    normalize_ein,
    normalize_payer,
)

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# EIN
# ---------------------------------------------------------------------------

class TestNormalizeEIN:
    def test_strips_dash(self):
        assert normalize_ein("13-1740114") == "131740114"

    def test_pads_short_digits(self):
        # 8 digits → zero-pad on the left to 9
        assert normalize_ein("31740114") == "031740114"

    def test_empty_and_none(self):
        assert normalize_ein(None) is None
        assert normalize_ein("") is None
        assert normalize_ein("   ") is None

    def test_passes_through_clean_9_digit(self):
        assert normalize_ein("131624096") == "131624096"


class TestEINFromHPTSourceFile:
    """In this extract only Montefiore exposes the EIN in `license_number`.
    Mount Sinai stores a NY state operating cert ('330024');
    NYU stores a NY DOH facility ID ('7002053H').  The filename prefix is
    therefore mandatory for cross-file joining."""

    def test_montefiore(self):
        assert (
            ein_from_hpt_source_file("131740114_MontefioreMedicalCenter_standardcharges.csv")
            == "131740114"
        )

    def test_mount_sinai(self):
        assert (
            ein_from_hpt_source_file("131624096_MountSinaiHospital_standardcharges.csv")
            == "131624096"
        )

    def test_nyu_langone_dashed(self):
        # NYU's filename in the wild has used both '13-3971298' and '133971298'
        assert (
            ein_from_hpt_source_file("13-3971298_NYULangone_standardcharges.csv")
            == "133971298"
        )

    def test_no_ein_prefix_returns_none(self):
        assert ein_from_hpt_source_file("standardcharges.csv") is None
        assert ein_from_hpt_source_file("") is None
        assert ein_from_hpt_source_file(None) is None


# ---------------------------------------------------------------------------
# Code & code_type
# ---------------------------------------------------------------------------

class TestNormalizeCodeType:
    def test_drg_variants(self):
        assert normalize_code_type("MS-DRG") == "MS-DRG"
        assert normalize_code_type("ms-drg") == "MS-DRG"
        assert normalize_code_type("MSDRG") == "MS-DRG"
        assert normalize_code_type("DRG") == "MS-DRG"

    def test_cpt_lowercase(self):
        assert normalize_code_type("cpt") == "CPT"
        assert normalize_code_type("CPT") == "CPT"

    def test_local_alias(self):
        # Some hospitals tag chargemaster items as 'CSTM'
        assert normalize_code_type("CSTM") == "LOCAL"
        assert normalize_code_type("local") == "LOCAL"

    def test_empty_inputs(self):
        assert normalize_code_type(None) is None
        assert normalize_code_type("") is None
        assert normalize_code_type("null") is None
        assert normalize_code_type("nan") is None

    def test_unknown_falls_through_uppercased(self):
        assert normalize_code_type("FOO") == "FOO"


class TestNormalizeCode:
    """Real values observed in the extract:
       NYU writes 'MS-DRG 872' (290 rows); other hospitals write '872'.
       Some payers (per Matt Robben's Serif blog) also post DRG codes with
       a leading zero ('0872')."""

    def test_drg_strips_prefix(self):
        assert normalize_code("MS-DRG 872", "MS-DRG") == "872"

    def test_drg_strips_leading_zero(self):
        assert normalize_code("0872", "MS-DRG") == "872"

    def test_drg_bare_number(self):
        assert normalize_code("872", "MS-DRG") == "872"

    def test_cpt_uppercases(self):
        assert normalize_code("43239", "CPT") == "43239"
        assert normalize_code("99213a", "CPT") == "99213A"

    def test_drg_with_letters_strips_them(self):
        # 'MS-DRG: 872' — punctuation gets stripped
        assert normalize_code("MS-DRG: 872", "MS-DRG") == "872"

    def test_empty(self):
        assert normalize_code(None) is None
        assert normalize_code("") is None
        assert normalize_code("nan") is None


# ---------------------------------------------------------------------------
# Payer
# ---------------------------------------------------------------------------

class TestNormalizePayer:
    @pytest.fixture(scope="class")
    def aliases(self):
        return load_payer_aliases(REPO / "core" / "payer_aliases.json")

    def test_exact_match(self, aliases):
        assert normalize_payer("Aetna", aliases) == "aetna"
        assert normalize_payer("CIGNA", aliases) == "cigna"
        assert normalize_payer("UnitedHealthcare", aliases) == "unitedhealthcare"

    def test_uhc_short_form(self, aliases):
        assert normalize_payer("UHC", aliases) == "unitedhealthcare"
        assert normalize_payer("United", aliases) == "unitedhealthcare"
        assert normalize_payer("United Healthcare", aliases) == "unitedhealthcare"

    def test_punctuation_stripped(self, aliases):
        # The TiC sample uses 'cigna-corporation'
        assert normalize_payer("cigna-corporation", aliases) == "cigna"
        assert normalize_payer("Cigna Corporation", aliases) == "cigna"

    def test_prefix_match(self, aliases):
        # Word-boundary prefix on a full brand key should still resolve.
        assert normalize_payer("Cigna PPO", aliases) == "cigna"
        assert normalize_payer("Aetna Better Health", aliases) == "aetna"
        assert normalize_payer("UnitedHealthcare Oxford", aliases) == "unitedhealthcare"

    def test_prefix_does_not_overmatch(self, aliases):
        # Regression guard: the prefix rule must NOT collapse unrelated
        # payers onto a canonical brand.  'United Mine Workers' is a
        # Taft-Hartley fund; 'United Behavioral Health' is a carve-out.
        # Neither is UnitedHealthcare and both must resolve to None.
        assert normalize_payer("United Mine Workers", aliases) is None
        assert normalize_payer("United Behavioral Health", aliases) is None
        assert normalize_payer("Unite Here Health", aliases) is None
        # And a short fragment must never match on a word boundary.
        assert normalize_payer("ci", aliases) is None
        assert normalize_payer("aet", aliases) is None

    def test_unknown_returns_none(self, aliases):
        assert normalize_payer("Blue Cross", aliases) is None
        assert normalize_payer("Humana", aliases) is None

    def test_empty(self, aliases):
        assert normalize_payer(None, aliases) is None
        assert normalize_payer("", aliases) is None
        assert normalize_payer("null", aliases) is None


# ---------------------------------------------------------------------------
# Billing class
# ---------------------------------------------------------------------------

class TestInferHPTBillingClass:
    def test_drg_always_institutional(self):
        # Even an inpatient setting cannot override; DRGs are bundles
        assert infer_hpt_billing_class("MS-DRG", "Septicemia", "inpatient") == "institutional"
        # Even with a 'PR ' prefix DRG wins (defensive)
        assert infer_hpt_billing_class("MS-DRG", "PR Something", None) == "institutional"

    def test_hc_prefix(self):
        assert infer_hpt_billing_class("CPT", "HC EMERGENCY DEPT VISIT LVL 3", "outpatient") == "institutional"

    def test_pr_prefix(self):
        assert infer_hpt_billing_class("CPT", "PR EDG TRANSORAL BIOPSY", "outpatient") == "professional"

    def test_inpatient_setting_only(self):
        # No prefix signal, but setting says inpatient → institutional
        assert infer_hpt_billing_class("CPT", "Some CPT description", "inpatient") == "institutional"

    def test_outpatient_no_prefix_is_unknown(self):
        # Conservative: a CPT in outpatient setting with no HC/PR prefix
        # could legitimately be either side; we punt to 'unknown'
        # and let the relaxed-match pass recover it
        assert infer_hpt_billing_class("CPT", "Some CPT description", "outpatient") == "unknown"
        assert infer_hpt_billing_class("CPT", "Some CPT description", "both") == "unknown"
        assert infer_hpt_billing_class("CPT", "", None) == "unknown"

    def test_handles_none_inputs(self):
        # Real HPT extracts have NaN descriptions; must not crash
        assert infer_hpt_billing_class(None, None, None) == "unknown"


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

def test_payer_aliases_file_is_valid_json():
    """Cheap canary so accidental JSON breakage fails loudly."""
    path = REPO / "core" / "payer_aliases.json"
    data = json.loads(path.read_text())
    assert "aliases" in data
    assert {"aetna", "cigna", "unitedhealthcare"}.issubset(set(data["aliases"].values()))
