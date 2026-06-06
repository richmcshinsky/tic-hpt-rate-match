"""
match.py
========
TiC × HPT rate matching pipeline.

Design summary
--------------
The output is one row per matched or unmatched rate observation at the
grain (hospital_ein, code, code_type, payer_canonical, billing_class).
Both sides are collapsed to that grain before matching — TiC across its
NPI sub-groups, HPT across its plan/posting rows — so the unified table
genuinely has one row per key (no row is duplicated just because a
hospital posted the same negotiated dollar under a dozen plan names).

We do *not* try to pick a single "best" rate per key — that is a
downstream product question that depends on the consumer.  Instead we
expose both sides' median/min/max plus a dispersion count, and every
match and every miss, with enough metadata to support that decision.

Match strategy: two passes against a hard join on (EIN, code, code_type).

  Pass 1 — Strict.   Require exact canonical payer AND exact billing class.
                     Highest precision; this is the "deterministic SQL
                     baseline" the brief refers to.

  Pass 2 — Billing-class-relaxed.  Same key but accept rows where billing
                     class differs OR one side is 'unknown'.  This recovers
                     outpatient CPT rows where HPT does not cleanly tag
                     institutional vs professional, which is the dominant
                     source of recall loss in this extract.

Records matched in Pass 1 are removed from consideration before Pass 2.
Keys unmatched after both passes become hpt_only / tic_only output rows.

Both the joins and the output assembly are vectorised (pandas.merge plus
column-wise construction); there are no per-row Python loops over the
join surface, which is what would have to change first at national volume.

Deferred to analysis/extensions (not in this core)
--------------------------------------------------
- Fuzzy payer-name matching (rapidfuzz).  With three known payers an
  alias file is sufficient; fuzzy matching is the right move when we
  start ingesting hundreds of payers and need to detect novel variants.
- Plan/NPI explode.  We collapse HPT across plans and TiC across NPI
  sub-groups to hit the stated grain.  Exploding either dimension back
  out (to ask "which plan / which physician group drove the outlier?")
  is a documented follow-up, not a core primitive.
- Composite confidence scoring.  We surface the inputs (match_level,
  billing_class_relaxed, dispersion counts) so a downstream model can be
  trained, but we do not pretend to a hand-tuned formula here.
- Rate-resolution ("which rate do I show the user?") logic.  That is a
  product policy, not a data-engineering primitive.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from core.normalizers import (
    ein_from_hpt_source_file,
    infer_hpt_billing_class,
    load_payer_aliases,
    normalize_code,
    normalize_code_type,
    normalize_ein,
    normalize_payer,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output schema (31 columns)
# Identity + provenance are the join-relevant fields; both sides expose
# median/min/max + a dispersion count; two comparability columns flag when a
# delta is NOT a like-for-like dollar comparison; the free-text columns at the
# bottom are for analyst review and explicitly not for joining on.
# ---------------------------------------------------------------------------

OUTPUT_COLUMNS = [
    # Identity
    "record_id",
    "hospital_ein",
    "hospital_name",
    "code",
    "code_type",
    "payer_canonical",
    "billing_class",
    # Provenance
    "data_source",            # 'both' | 'hpt_only' | 'tic_only'
    "match_level",            # 1=strict, 2=billing_class_relaxed, 0=unmatched
    "billing_class_relaxed",  # bool
    # Rate-value corroboration (Pass 3): cross-side exact-dollar agreement
    # within the (ein, code, code_type, payer) block, independent of billing class.
    "rate_value_corroborated",  # bool
    "corroborated_values",      # '; '-joined dollar value(s) present on BOTH sides
    # HPT rates (collapsed across plans at this key)
    "hpt_rate_median",
    "hpt_rate_min",
    "hpt_rate_max",
    "hpt_plan_count",         # distinct HPT postings collapsed into this row
    "hpt_rate_methodology",   # distinct methodologies seen, '; '-joined
    "hpt_methodology_mixed",  # True if >1 methodology collapsed -> delta not like-for-like
    # TiC rates (collapsed across NPI sub-groups at this key)
    "tic_rate_median",
    "tic_rate_min",
    "tic_rate_max",
    "tic_npi_group_count",
    "tic_negotiation_types",  # distinct TiC negotiation_type(s) for this key
    # CMS baseline (from TiC; rate=median across sub-groups, schedule(s) joined)
    "cms_baseline_rate",
    "cms_baseline_schedule",
    "cms_baseline_mixed",     # True if >1 schedule blended -> baseline is mixed, not authoritative
    # Delta (median vs median)
    "rate_delta",
    "rate_delta_pct",
    # Free-text context (kept for analyst review, not for joining on)
    "plan_name_hpt",          # representative plan; see hpt_plan_count for n
    "network_name_tic",
    "hpt_payer_raw",
    "hpt_setting",
    "code_description",
]

KEY_COLS = ["_ein", "_code", "_code_type", "_payer", "_billing"]


# ---------------------------------------------------------------------------
# Stats — populated during run, surfaced in the report
# ---------------------------------------------------------------------------

@dataclass
class PipelineStats:
    tic_rows_input: int = 0
    tic_rows_dropped_percentage: int = 0  # negotiation_type='percentage'
    tic_rows_after_filter: int = 0
    tic_match_groups: int = 0             # after NPI sub-group collapse

    hpt_rows_input: int = 0
    hpt_rows_dropped_local: int = 0       # code_type='LOCAL' (chargemaster items)
    hpt_rows_dropped_no_dollar: int = 0   # no negotiated_dollar
    hpt_rows_dropped_no_payer: int = 0    # payer_name not in alias map
    hpt_rows_match_eligible: int = 0      # eligible postings before collapse
    hpt_match_groups: int = 0             # after plan-level collapse

    matches_pass1: int = 0
    matches_pass2: int = 0
    unmatched_hpt: int = 0
    unmatched_tic: int = 0
    rate_corroborated_blocks: int = 0   # (ein,code,payer) blocks with exact cross-side $ agreement
    rate_corroborated_rows: int = 0     # output rows flagged rate_value_corroborated
    output_rows: int = 0

    def as_markdown(self) -> str:
        return (
            "| Stage | Rows |\n|---|---:|\n"
            f"| TiC input | {self.tic_rows_input:,} |\n"
            f"| TiC dropped (negotiation_type=percentage) | {self.tic_rows_dropped_percentage:,} |\n"
            f"| TiC after filtering | {self.tic_rows_after_filter:,} |\n"
            f"| TiC match groups (post NPI collapse) | {self.tic_match_groups:,} |\n"
            f"| HPT input | {self.hpt_rows_input:,} |\n"
            f"| HPT dropped (code_type=LOCAL chargemaster) | {self.hpt_rows_dropped_local:,} |\n"
            f"| HPT dropped (no dollar rate) | {self.hpt_rows_dropped_no_dollar:,} |\n"
            f"| HPT dropped (payer not in alias map) | {self.hpt_rows_dropped_no_payer:,} |\n"
            f"| HPT eligible postings | {self.hpt_rows_match_eligible:,} |\n"
            f"| HPT match groups (post plan collapse) | {self.hpt_match_groups:,} |\n"
            f"| Matches — Pass 1 (strict) | {self.matches_pass1:,} |\n"
            f"| Matches — Pass 2 (billing-class relaxed) | {self.matches_pass2:,} |\n"
            f"| Unmatched HPT (hpt_only output rows) | {self.unmatched_hpt:,} |\n"
            f"| Unmatched TiC (tic_only output rows) | {self.unmatched_tic:,} |\n"
            f"| Total output rows | {self.output_rows:,} |\n"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _join_distinct(s: pd.Series, lower: bool = False) -> "str | None":
    """Sorted distinct non-empty values, '; '-joined. Lower-cases when asked."""
    norm = (lambda x: x.lower()) if lower else (lambda x: x)
    vals = sorted({norm(str(x).strip()) for x in s if pd.notna(x) and str(x).strip()})
    return "; ".join(vals) if vals else None


def _schedule_class(part: str) -> str:
    """A CMS schedule's class with the PFS locality suffix stripped:
    'PFS_NONFACILITY_1320201' -> 'PFS_NONFACILITY', 'OPPS' -> 'OPPS'.
    Multi-locality within one class is normal MAC variance, not a mismatch."""
    tokens = part.split("_")
    keep = [t for t in tokens if not (t.isdigit() or t == "NPA")]
    return "_".join(keep) if keep else part


def _rate_value_set(s: pd.Series) -> frozenset:
    """Distinct rates in a group, rounded to the cent — used for value corroboration."""
    return frozenset(round(float(x), 2) for x in s.dropna())


def _blended(joined: "str | None", key=lambda p: p) -> bool:
    """True when a '; '-joined distinct string spans >1 class under `key`.
    One helper for both 'mixed' flags so they cannot drift apart: methodology
    uses the identity key; cms_baseline uses _schedule_class to ignore locality."""
    if pd.isna(joined) or not joined:
        return False
    classes = {key(p.strip()) for p in joined.split(";") if p.strip()}
    return len(classes) > 1


def _norm_billing(series: pd.Series) -> pd.Series:
    """Lower/strip a billing_class column and map empties to 'unknown'."""
    out = series.astype(str).str.lower().str.strip()
    return out.replace({"nan": "unknown", "none": "unknown", "null": "unknown", "": "unknown"})


# ---------------------------------------------------------------------------
# Preprocess TiC
# ---------------------------------------------------------------------------

def preprocess_tic(df: pd.DataFrame, aliases: dict[str, str], stats: PipelineStats) -> pd.DataFrame:
    """
    Normalize TiC and collapse to one row per match key.

    Why collapse: TiC files publish one row per (key × NPI sub-group) where
    a sub-group is a set of NPIs that share a single negotiated rate.  The
    cross-file match is at the (hospital, code, payer, billing_class) level,
    not the sub-group level, so we aggregate across sub-groups using:

        rate_median  — the headline number
        rate_min/max — the contracted range across sub-groups
        npi_group_count — how many distinct rates were collapsed

    This preserves the dispersion signal without forcing a single number.

    We also drop rows where negotiation_type='percentage'.  In the provided
    extract there are 3 such rows with rate=70, which means "70 percent of
    [the cms_baseline_rate column]".  Treating 70 as a dollar value would
    silently produce ~99% deltas.  Dropping is the honest move at this
    stage; conversion is a feature for v2.
    """
    stats.tic_rows_input = len(df)
    df = df.copy()

    pct_mask = df["negotiation_type"].astype(str).str.lower() == "percentage"
    stats.tic_rows_dropped_percentage = int(pct_mask.sum())
    df = df[~pct_mask].copy()
    stats.tic_rows_after_filter = len(df)

    df["_ein"]        = df["ein"].apply(normalize_ein)
    df["_code_type"]  = df["code_type"].apply(normalize_code_type)
    df["_code"]       = df.apply(lambda r: normalize_code(r["code"], r["code_type"]), axis=1)
    df["_payer"]      = df["payer"].apply(lambda p: normalize_payer(p, aliases))
    df["_billing"]    = _norm_billing(df["billing_class"])
    df["rate"]        = pd.to_numeric(df["rate"], errors="coerce")
    df["cms_baseline_rate"] = pd.to_numeric(df["cms_baseline_rate"], errors="coerce")

    grouped = (
        df.groupby(KEY_COLS, dropna=False)
          .agg(
              tic_rate_median       = ("rate", "median"),
              tic_rate_min          = ("rate", "min"),
              tic_rate_max          = ("rate", "max"),
              tic_npi_group_count   = ("rate", "count"),
              # cms_baseline disagrees across sub-groups in ~15/34 keys, so
              # 'first' would be order-dependent. Take the median (deterministic)
              # and join the distinct schedule(s) so the variation stays visible.
              # The cms_baseline_mixed flag below tells a consumer when that
              # median blended different schedules (e.g. PFS_FACILITY vs
              # PFS_NONFACILITY) and therefore is NOT itself a single baseline.
              cms_baseline_rate     = ("cms_baseline_rate", "median"),
              cms_baseline_schedule = ("cms_baseline_schedule", _join_distinct),
              network_name_tic      = ("network_name", "first"),
              tic_negotiation_types = ("negotiation_type", _join_distinct),
              _rate_values          = ("rate", _rate_value_set),
          )
          .reset_index()
    )
    # cms_baseline_mixed is True when the median blended >1 schedule CLASS
    # (PFS_FACILITY with PFS_NONFACILITY, OPPS with PFS, etc.). The median is
    # deterministic, but across mixed classes the baseline is 'blended', not
    # 'authoritative'. Same _blended() helper as hpt_methodology_mixed so the
    # two flags use one definition of "spans more than one class".
    grouped["cms_baseline_mixed"] = grouped["cms_baseline_schedule"].map(
        lambda j: _blended(j, key=_schedule_class)
    )
    stats.tic_match_groups = len(grouped)
    logger.info("TiC: %d rows → %d match groups (after %d pct-rate drops)",
                stats.tic_rows_after_filter, stats.tic_match_groups,
                stats.tic_rows_dropped_percentage)
    return grouped


# ---------------------------------------------------------------------------
# Preprocess HPT
# ---------------------------------------------------------------------------

def preprocess_hpt(df: pd.DataFrame, aliases: dict[str, str], stats: PipelineStats) -> pd.DataFrame:
    """
    Normalize HPT, then collapse to the same grain as TiC.

    Filters applied (in order):

      1. code_type='LOCAL' — chargemaster custom codes.  In this extract
         410 LOCAL rows (all NYU) carry CPT-shaped raw_code values that
         coincidentally collide with real CPT codes (e.g. '43239' attached
         to a shoulder implant).  Matching against TiC's CPT 43239 would
         produce 100% false-positive rows.  We exclude them up front and
         log the count.

      2. Missing standard_charge_negotiated_dollar.  Can't compare a rate
         we don't have.  Some HPT rows publish only a percentage-of-charges
         value (see standard_charge_negotiated_percentage); those are
         legitimate rates but not directly comparable to TiC dollar values
         without each hospital's gross-charge chargemaster.

      3. Payer name not resolvable via the alias map.  In this extract we
         only have aliases for the three payers present in TiC; HPT has
         ~70+ distinct payer strings.  Resolving the long tail is the next
         feature, not a correctness issue.

    Collapse: a single (hospital, code, payer, billing_class) key is posted
    once per plan in HPT, frequently with the same negotiated dollar.  We
    collapse across plans — symmetric to the TiC NPI collapse — exposing
    hpt_rate_median/min/max and hpt_plan_count, so the unified table is one
    row per key rather than one row per plan posting.
    """
    stats.hpt_rows_input = len(df)
    df = df.copy()

    # 1. Drop LOCAL chargemaster rows
    local_mask = df["code_type"].astype(str).str.lower() == "local"
    stats.hpt_rows_dropped_local = int(local_mask.sum())
    df = df[~local_mask].copy()

    # 2. Drop rows with no dollar rate
    df["_hpt_rate"] = pd.to_numeric(df["standard_charge_negotiated_dollar"], errors="coerce")
    no_dollar = df["_hpt_rate"].isna()
    stats.hpt_rows_dropped_no_dollar = int(no_dollar.sum())
    df = df[~no_dollar].copy()

    # Normalize fields
    df["_ein_filename"] = df["source_file_name"].apply(ein_from_hpt_source_file)
    df["_ein_license"]  = df["license_number"].apply(normalize_ein)
    df["_ein"]          = df["_ein_filename"].fillna(df["_ein_license"])
    df["_code_type"]    = df["code_type"].apply(normalize_code_type)
    df["_code"]         = df.apply(lambda r: normalize_code(r["raw_code"], r["code_type"]), axis=1)
    df["_payer"]        = df["payer_name"].apply(lambda p: normalize_payer(p, aliases))
    df["_billing"]      = df.apply(
        lambda r: infer_hpt_billing_class(r["code_type"], r["description"], r["setting"]),
        axis=1,
    )

    # 3. Drop rows with no canonical payer (long-tail payer names)
    unresolved = df["_payer"].isna()
    stats.hpt_rows_dropped_no_payer = int(unresolved.sum())
    df = df[~unresolved].copy()
    stats.hpt_rows_match_eligible = len(df)

    # Collapse across plans to the match grain
    grouped = (
        df.groupby(KEY_COLS, dropna=False)
          .agg(
              hpt_rate_median      = ("_hpt_rate", "median"),
              hpt_rate_min         = ("_hpt_rate", "min"),
              hpt_rate_max         = ("_hpt_rate", "max"),
              hpt_plan_count       = ("_hpt_rate", "count"),
              hpt_rate_methodology = ("standard_charge_methodology",
                                      lambda s: _join_distinct(s, lower=True)),
              _rate_values         = ("_hpt_rate", _rate_value_set),
              hospital_name        = ("hospital_name", "first"),
              plan_name            = ("plan_name", "first"),
              payer_name           = ("payer_name", "first"),
              setting              = ("setting", "first"),
              description          = ("description", "first"),
          )
          .reset_index()
    )
    # Flag keys where the collapse blended >1 methodology: a median across a
    # case-rate dollar and a percent-of-charges dollar is not a like-for-like
    # number, so a delta on these rows should be read as presence, not magnitude.
    # Same _blended() helper as cms_baseline_mixed.
    grouped["hpt_methodology_mixed"] = grouped["hpt_rate_methodology"].map(_blended)
    stats.hpt_match_groups = len(grouped)
    logger.info(
        "HPT: %d rows → %d eligible postings → %d match groups "
        "(dropped %d LOCAL, %d no-dollar, %d unresolved-payer)",
        stats.hpt_rows_input, stats.hpt_rows_match_eligible, stats.hpt_match_groups,
        stats.hpt_rows_dropped_local, stats.hpt_rows_dropped_no_dollar,
        stats.hpt_rows_dropped_no_payer,
    )
    return grouped


# ---------------------------------------------------------------------------
# Output assembly (vectorised — no per-row loops)
# ---------------------------------------------------------------------------

def _assemble_matched(j: pd.DataFrame, match_level: int, relaxed: bool,
                      bc_hpt_col: str, bc_tic_col: str) -> pd.DataFrame:
    if len(j) == 0:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    hpt_med = pd.to_numeric(j["hpt_rate_median"], errors="coerce")
    tic_med = pd.to_numeric(j["tic_rate_median"], errors="coerce")
    delta = (tic_med - hpt_med).round(2)
    pct = (delta / hpt_med * 100).where(hpt_med.notna() & (hpt_med != 0)).round(2)

    bc_hpt = j[bc_hpt_col].to_numpy()
    bc_tic = j[bc_tic_col].to_numpy()
    # When billing was relaxed, prefer the more specific side ('unknown' loses).
    billing = np.where(bc_hpt == "unknown", bc_tic, bc_hpt)

    out = pd.DataFrame({
        "hospital_ein":          j["_ein"].to_numpy(),
        "hospital_name":         j["hospital_name"].to_numpy(),
        "code":                  j["_code"].to_numpy(),
        "code_type":             j["_code_type"].to_numpy(),
        "payer_canonical":       j["_payer"].to_numpy(),
        "billing_class":         billing,
        "data_source":           "both",
        "match_level":           match_level,
        "billing_class_relaxed": relaxed,
        "hpt_rate_median":       hpt_med.to_numpy(),
        "hpt_rate_min":          pd.to_numeric(j["hpt_rate_min"], errors="coerce").to_numpy(),
        "hpt_rate_max":          pd.to_numeric(j["hpt_rate_max"], errors="coerce").to_numpy(),
        "hpt_plan_count":        pd.array(j["hpt_plan_count"].to_numpy(), dtype="Int64"),
        "hpt_rate_methodology":  j["hpt_rate_methodology"].to_numpy(),
        "hpt_methodology_mixed": j["hpt_methodology_mixed"].to_numpy(),
        "tic_rate_median":       tic_med.to_numpy(),
        "tic_rate_min":          pd.to_numeric(j["tic_rate_min"], errors="coerce").to_numpy(),
        "tic_rate_max":          pd.to_numeric(j["tic_rate_max"], errors="coerce").to_numpy(),
        "tic_npi_group_count":   pd.array(j["tic_npi_group_count"].to_numpy(), dtype="Int64"),
        "tic_negotiation_types": j["tic_negotiation_types"].to_numpy(),
        "cms_baseline_rate":     pd.to_numeric(j["cms_baseline_rate"], errors="coerce").to_numpy(),
        "cms_baseline_schedule": j["cms_baseline_schedule"].to_numpy(),
        "cms_baseline_mixed":    j["cms_baseline_mixed"].to_numpy(),
        "rate_delta":            delta.to_numpy(),
        "rate_delta_pct":        pct.to_numpy(),
        "plan_name_hpt":         j["plan_name"].to_numpy(),
        "network_name_tic":      j["network_name_tic"].to_numpy(),
        "hpt_payer_raw":         j["payer_name"].to_numpy(),
        "hpt_setting":           j["setting"].to_numpy(),
        "code_description":      j["description"].to_numpy(),
    })
    return out.reindex(columns=OUTPUT_COLUMNS)


def _assemble_hpt_only(h: pd.DataFrame) -> pd.DataFrame:
    if len(h) == 0:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    out = pd.DataFrame({
        "hospital_ein":          h["_ein"].to_numpy(),
        "hospital_name":         h["hospital_name"].to_numpy(),
        "code":                  h["_code"].to_numpy(),
        "code_type":             h["_code_type"].to_numpy(),
        "payer_canonical":       h["_payer"].to_numpy(),
        "billing_class":         h["_billing"].to_numpy(),
        "data_source":           "hpt_only",
        "match_level":           0,
        "billing_class_relaxed": False,
        "hpt_rate_median":       pd.to_numeric(h["hpt_rate_median"], errors="coerce").to_numpy(),
        "hpt_rate_min":          pd.to_numeric(h["hpt_rate_min"], errors="coerce").to_numpy(),
        "hpt_rate_max":          pd.to_numeric(h["hpt_rate_max"], errors="coerce").to_numpy(),
        "hpt_plan_count":        pd.array(h["hpt_plan_count"].to_numpy(), dtype="Int64"),
        "hpt_rate_methodology":  h["hpt_rate_methodology"].to_numpy(),
        "hpt_methodology_mixed": h["hpt_methodology_mixed"].to_numpy(),
        "plan_name_hpt":         h["plan_name"].to_numpy(),
        "hpt_payer_raw":         h["payer_name"].to_numpy(),
        "hpt_setting":           h["setting"].to_numpy(),
        "code_description":      h["description"].to_numpy(),
    })
    return out.reindex(columns=OUTPUT_COLUMNS)


def _assemble_tic_only(t: pd.DataFrame) -> pd.DataFrame:
    if len(t) == 0:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    out = pd.DataFrame({
        "hospital_ein":          t["_ein"].to_numpy(),
        "code":                  t["_code"].to_numpy(),
        "code_type":             t["_code_type"].to_numpy(),
        "payer_canonical":       t["_payer"].to_numpy(),
        "billing_class":         t["_billing"].to_numpy(),
        "data_source":           "tic_only",
        "match_level":           0,
        "billing_class_relaxed": False,
        "tic_rate_median":       pd.to_numeric(t["tic_rate_median"], errors="coerce").to_numpy(),
        "tic_rate_min":          pd.to_numeric(t["tic_rate_min"], errors="coerce").to_numpy(),
        "tic_rate_max":          pd.to_numeric(t["tic_rate_max"], errors="coerce").to_numpy(),
        "tic_npi_group_count":   pd.array(t["tic_npi_group_count"].to_numpy(), dtype="Int64"),
        "tic_negotiation_types": t["tic_negotiation_types"].to_numpy(),
        "cms_baseline_rate":     pd.to_numeric(t["cms_baseline_rate"], errors="coerce").to_numpy(),
        "cms_baseline_schedule": t["cms_baseline_schedule"].to_numpy(),
        "cms_baseline_mixed":    t["cms_baseline_mixed"].to_numpy(),
        "network_name_tic":      t["network_name_tic"].to_numpy(),
    })
    return out.reindex(columns=OUTPUT_COLUMNS)


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------

def match_rates(tic: pd.DataFrame, hpt: pd.DataFrame,
                stats: PipelineStats) -> pd.DataFrame:
    """
    Two-pass matcher.  See module docstring for the strategy.

    Both sides are already collapsed to the match grain, so each pass is a
    single pandas.merge and the output is built column-wise — no per-row
    Python loops over the join surface, which matters at national volume.
    """
    hpt = hpt.reset_index(drop=True).copy()
    tic = tic.reset_index(drop=True).copy()
    hpt["_hpt_idx"] = hpt.index
    tic["_tic_idx"] = tic.index

    # Pass 1 — strict: EIN + code + code_type + payer + billing_class
    p1 = hpt.merge(tic, on=KEY_COLS, how="inner")
    matched_hpt = set(p1["_hpt_idx"].tolist())
    matched_tic = set(p1["_tic_idx"].tolist())
    df1 = _assemble_matched(p1, match_level=1, relaxed=False,
                            bc_hpt_col="_billing", bc_tic_col="_billing")
    stats.matches_pass1 = len(df1)

    # Pass 2 — billing-class relaxed: join on the key minus billing_class,
    # over rows not already matched in Pass 1.
    join4 = ["_ein", "_code", "_code_type", "_payer"]
    hpt_rem = hpt[~hpt["_hpt_idx"].isin(matched_hpt)]
    tic_rem = tic[~tic["_tic_idx"].isin(matched_tic)]
    p2 = hpt_rem.merge(tic_rem, on=join4, how="inner", suffixes=("_hpt", "_tic"))
    # Accept only where billing class actually differs or one side is 'unknown'.
    # (Equal-billing pairs would have matched in Pass 1; this guards the
    # fan-out where one HPT key maps to several TiC billing groups.)
    accepted = p2[
        (p2["_billing_hpt"] != p2["_billing_tic"])
        | (p2["_billing_hpt"] == "unknown")
        | (p2["_billing_tic"] == "unknown")
    ]
    matched_hpt |= set(accepted["_hpt_idx"].tolist())
    matched_tic |= set(accepted["_tic_idx"].tolist())
    df2 = _assemble_matched(accepted, match_level=2, relaxed=True,
                            bc_hpt_col="_billing_hpt", bc_tic_col="_billing_tic")
    stats.matches_pass2 = len(df2)

    # Unmatched
    hpt_un = hpt[~hpt["_hpt_idx"].isin(matched_hpt)]
    tic_un = tic[~tic["_tic_idx"].isin(matched_tic)]
    df_h = _assemble_hpt_only(hpt_un)
    df_t = _assemble_tic_only(tic_un)
    stats.unmatched_hpt = len(df_h)
    stats.unmatched_tic = len(df_t)

    frames = [f for f in (df1, df2, df_h, df_t) if not f.empty]
    # The hpt_only / tic_only frames legitimately carry all-NA columns (the
    # other side's rates). pandas warns that a future version will change how
    # such columns affect concat dtypes; we intentionally keep them, so silence
    # that one deprecation rather than leak it to the CLI.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        out = (pd.concat(frames, ignore_index=True) if frames
               else pd.DataFrame(columns=OUTPUT_COLUMNS))

    # Backfill hospital_name on tic_only rows from the EIN -> name mapping
    # HPT exposes.  Hospital name doesn't exist in TiC, so a TiC-only row
    # would otherwise show as NaN even though we know which hospital it is.
    ein_to_name = (
        hpt.dropna(subset=["_ein", "hospital_name"])
           .drop_duplicates("_ein")
           .set_index("_ein")["hospital_name"]
           .to_dict()
    )
    mask = out["hospital_name"].isna() & out["hospital_ein"].notna()
    out.loc[mask, "hospital_name"] = out.loc[mask, "hospital_ein"].map(ein_to_name)

    # Restore clean integer dtype: concat with the all-NaN reindexed frames
    # upcasts the Int64 count columns to float, rendering '17.0' in the CSV.
    for col in ("hpt_plan_count", "tic_npi_group_count"):
        out[col] = out[col].astype("Int64")
    # Mixed-methodology flag is meaningful only where there is an HPT rate;
    # cms_baseline_mixed only where there is a TiC rate.
    out["hpt_methodology_mixed"] = out["hpt_methodology_mixed"].astype("boolean")
    out["cms_baseline_mixed"] = out["cms_baseline_mixed"].astype("boolean")

    # Pass 3 — rate-value corroboration (annotation; does not change the grain).
    # Within each (ein, code, code_type, payer) block, find dollar values that
    # appear on BOTH sides regardless of billing class. Exact cross-file rate
    # agreement is strong evidence the records are the same negotiated price,
    # so it surfaces line-item matches the key-level join splits apart — e.g.
    # Mount Sinai × UHC × 43239 agrees at $6,438 even though billing-class
    # inference put the two postings on different rows.
    def _block_values(frame: pd.DataFrame) -> dict:
        acc: dict = {}
        for ein, code, ct, payer, vals in zip(
            frame["_ein"], frame["_code"], frame["_code_type"],
            frame["_payer"], frame["_rate_values"]
        ):
            acc.setdefault((ein, code, ct, payer), set()).update(vals)
        return acc

    hpt_block, tic_block = _block_values(hpt), _block_values(tic)
    corroborated = {
        k: sorted(hpt_block[k] & tic_block[k])
        for k in (hpt_block.keys() & tic_block.keys())
        if hpt_block[k] & tic_block[k]
    }
    keys4 = list(zip(out["hospital_ein"], out["code"], out["code_type"], out["payer_canonical"]))
    out["rate_value_corroborated"] = [k in corroborated for k in keys4]
    out["corroborated_values"] = [
        "; ".join(f"{v:.2f}" for v in corroborated[k]) if k in corroborated else None
        for k in keys4
    ]
    stats.rate_corroborated_blocks = len(corroborated)
    stats.rate_corroborated_rows = int(out["rate_value_corroborated"].sum())

    out["record_id"] = [f"R{i:05d}" for i in range(1, len(out) + 1)]
    out = out[OUTPUT_COLUMNS]
    stats.output_rows = len(out)
    logger.info("Match: pass1=%d, pass2=%d, hpt_only=%d, tic_only=%d, total=%d",
                stats.matches_pass1, stats.matches_pass2,
                stats.unmatched_hpt, stats.unmatched_tic, stats.output_rows)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(tic_path: Path, hpt_path: Path, aliases_path: Path) -> "tuple[pd.DataFrame, PipelineStats]":
    """End-to-end entry point. Returns (unified_dataframe, stats)."""
    aliases = load_payer_aliases(aliases_path)
    tic_raw = pd.read_csv(tic_path, dtype=str)
    hpt_raw = pd.read_csv(hpt_path, dtype=str)
    stats = PipelineStats()
    tic = preprocess_tic(tic_raw, aliases, stats)
    hpt = preprocess_hpt(hpt_raw, aliases, stats)
    out = match_rates(tic, hpt, stats)
    return out, stats
