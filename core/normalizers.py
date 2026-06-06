"""
normalizers.py
==============
Field-level normalization for the TiC × HPT matching pipeline.

Everything here is deliberately small and testable.  The choices that
matter are documented inline; the rest is straightforward.

References
----------
- Serif: "Apples to Crabapples: Comparing A Hospital MRF To A Payer MRF"
  https://www.serifhealth.com/blog/apples-to-crabapples-comparing-a-hospital-mrf-to-a-payer-mrf
- CMS Hospital Price Transparency v2 schema:
  https://www.cms.gov/priorities/key-initiatives/hospital-price-transparency
- CMS Transparency in Coverage rule:
  https://www.cms.gov/healthplan-price-transparency
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# EIN
# ---------------------------------------------------------------------------

_EIN_FILENAME_RE = re.compile(r"^([0-9]{2}-?[0-9]{7})")


def normalize_ein(raw) -> Optional[str]:
    """Strip non-digits, zero-pad to 9. Returns None for empty input."""
    if raw is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(raw))
    return digits.zfill(9) if digits else None


def ein_from_hpt_source_file(source_file_name) -> Optional[str]:
    """
    Extract the federal EIN embedded as the prefix of HPT v2 source_file_name.

    Why this exists: in the provided extract, only 1 of 3 hospitals
    (Montefiore) stores its federal EIN in the `license_number` column.
    Mount Sinai stores a NY state operating certificate (e.g. '330024');
    NYU Langone stores a NY DOH facility ID (e.g. '7002053H').  CMS
    Hospital Price Transparency v2 *does* require the file name to encode
    the EIN, so the filename prefix is the only reliable cross-file key.

    Without this step, recall on this extract drops from 100% to 33% of
    hospitals.  This is the single highest-leverage normalizer in the
    pipeline.
    """
    if not source_file_name:
        return None
    m = _EIN_FILENAME_RE.match(str(source_file_name).strip())
    return normalize_ein(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Code & code_type
# ---------------------------------------------------------------------------

_CODE_TYPE_MAP = {
    "ms-drg": "MS-DRG",
    "msdrg":  "MS-DRG",
    "ms drg": "MS-DRG",
    "drg":    "MS-DRG",
    "apr-drg": "APR-DRG",
    "aprdrg":  "APR-DRG",
    "apr drg": "APR-DRG",
    "cpt":    "CPT",
    "hcpcs":  "HCPCS",
    "icd":    "ICD",
    "icd-10-cm": "ICD",
    "icd-10-pcs": "ICD",
    "rc":     "RC",
    "rev":    "RC",
    "ndc":    "NDC",
    "local":  "LOCAL",
    "cstm":   "LOCAL",
}


def normalize_code_type(raw) -> Optional[str]:
    """Canonicalize the code_type string. Returns None for empty input."""
    if raw is None or str(raw).strip().lower() in ("", "null", "nan"):
        return None
    key = str(raw).strip().lower()
    return _CODE_TYPE_MAP.get(key, str(raw).strip().upper())


def normalize_code(raw, code_type=None) -> Optional[str]:
    """
    Canonicalize a billing code based on its code_type.

    DRG quirks observed in this extract:
      - Montefiore + Mount Sinai write '872'
      - NYU Langone writes 'MS-DRG 872'
      - Some payers (Matt Robben's blog notes UHC) post DRG codes with
        an extra leading zero.

    We strip all non-digits for DRG codes and lstrip leading zeros.  For
    CPT/HCPCS we upper-case to preserve any alpha suffix (e.g. '99213A').
    """
    if raw is None or str(raw).strip().lower() in ("", "null", "nan"):
        return None
    code = str(raw).strip()
    ct = normalize_code_type(code_type) if code_type else None

    if ct in ("MS-DRG", "APR-DRG"):
        digits = re.sub(r"[^0-9]", "", code)
        return digits.lstrip("0") or "0" if digits else None
    if ct in ("CPT", "HCPCS"):
        return code.upper()
    return code


# ---------------------------------------------------------------------------
# Payer name
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def load_payer_aliases(path: Path) -> dict[str, str]:
    """Load the alias map from a JSON file with shape {'aliases': {...}}."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("aliases", {})


def _clean_payer(raw: str) -> str:
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", str(raw).lower())).strip()


def normalize_payer(raw, aliases: dict[str, str]) -> Optional[str]:
    """
    Map a raw payer-name string to a canonical key (e.g. 'aetna').

    Strategy:
      1. Exact alias hit on the cleaned string.
      2. Word-boundary prefix match, but ONLY against full brand keys
         (a key that equals its own canonical value and is >= 5 chars,
         i.e. 'aetna', 'cigna', 'unitedhealthcare').  This catches
         'cigna ppo', 'aetna better health', 'unitedhealthcare oxford'
         while refusing to expand short/ambiguous abbreviations.
      3. Otherwise return None — the caller decides whether to drop the
         row, log it, or treat as a novel payer.

    Why the restriction matters (this is a real correctness fix, not
    style): an earlier version accepted a match whenever the cleaned
    string was a prefix of an alias OR an alias was a prefix of the
    cleaned string.  That silently mis-resolved unrelated payers — e.g.
    'United Mine Workers' and 'United Behavioral Health' both collapsed
    onto UnitedHealthcare, and a two-character string like 'ci' resolved
    to Cigna.  None of those fired on *this* clean extract, but the rule
    is unbounded and fails silently, which is worse than a thresholded
    fuzzy match.  We now require a true word boundary ('aetna ' not
    'aet') and only expand unambiguous brand tokens.

    We intentionally do not do fuzzy (rapidfuzz) matching at the core
    level: with only three payers in this extract every variant resolves
    cleanly via the alias file.  Fuzzy matching belongs at a layer where
    we accumulate observed misses and tune a threshold — that lives in
    analysis/extensions, not core.
    """
    if raw is None or str(raw).strip().lower() in ("", "null", "nan"):
        return None
    cleaned = _clean_payer(raw)
    if cleaned in aliases:
        return aliases[cleaned]
    # Prefix-expand only full, unambiguous brand keys, and only on a word boundary.
    for key, canonical in aliases.items():
        if key == canonical and len(key) >= 5 and cleaned.startswith(key + " "):
            return canonical
    return None


# ---------------------------------------------------------------------------
# Billing class
# ---------------------------------------------------------------------------

_INPATIENT_CODE_TYPES = {"MS-DRG", "APR-DRG"}


def infer_hpt_billing_class(code_type: Optional[str], description: Optional[str],
                            setting: Optional[str]) -> str:
    """
    Map an HPT row to TiC's billing_class vocabulary
    ('institutional' | 'professional' | 'unknown').

    HPT does not have a billing_class field; we infer it from three signals
    in priority order:

      1. Code type — DRG codes are inpatient bundles and are always
         institutional.

      2. Description prefix — many hospitals' HPT files tag rows with
         'HC ' (hospital/institutional) or 'PR ' (professional).  This
         pattern is documented by Serif and is visible on Montefiore's
         CPT rows in this extract (1,350 'HC ' prefixes vs 176 'PR ').

      3. Setting field — 'inpatient' → institutional.  'outpatient' and
         'both' are ambiguous: same CPT code can be billed by the facility
         AND a physician at the same encounter, with separate rates.  In
         the absence of other signal we mark these 'unknown' and let the
         matcher decide whether to keep or drop the constraint.

    This is intentionally conservative: it is better to label a row
    'unknown' and recover it via a billing-class-relaxed pass than to
    mislabel it and produce a false positive.
    """
    ct = normalize_code_type(code_type)
    if ct in _INPATIENT_CODE_TYPES:
        return "institutional"

    desc = (description or "").strip().upper()
    if desc.startswith("HC ") or desc.startswith("HOSP "):
        return "institutional"
    if desc.startswith("PR ") or desc.startswith("PROF "):
        return "professional"

    s = (setting or "").strip().lower()
    if s == "inpatient":
        return "institutional"

    return "unknown"
