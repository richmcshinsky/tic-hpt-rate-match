# Healthcare Price-Transparency Rate Match

[![tests](https://github.com/richmcshinsky/tic-hpt-rate-match/actions/workflows/test.yml/badge.svg)](https://github.com/richmcshinsky/tic-hpt-rate-match/actions/workflows/test.yml)

**Author:** Richard McShinsky · **LinkedIn:** [Richard McShinsky](https://www.linkedin.com/in/richard-mcshinsky/)

Note: This is a take-home submission for a senior data scientist role at a price-transparency startup, completed in ~3 hours and iterated for clarity afterward.

This repo combines a sample of Transparency-in-Coverage (TiC) payer rates with a sample of Hospital Price Transparency (HPT) rates into a single per-rate dataset, and walks through what the matches and non-matches say about the structure of US healthcare billing data.

The intent is not to ship a production matcher. It is to make a defensible set of choices, surface what those choices cost, and be honest about where this work would need to grow before it could anchor a Snowflake-scale pipeline.

---

## How to run

```bash
pip install -r requirements.txt
python -m core.run                # uses defaults: data/*.csv → out/unified_rates.csv
python -m pytest tests/ -q        # normalizer + matcher tests
```

`python -m core.run --help` shows the CLI flags (custom paths, alias file, output location). Inputs are the two CSVs Serif hosts at `https://mrf.serifhealth.com/public/`; both are committed under `data/` so the run is reproducible offline.

Sample extracts are mirrored under data/ from Serif's public bucket at https://mrf.serifhealth.com/public/ for offline reproducibility.

---

## Outputs

| File | Contents |
|---|---|
| `out/unified_rates.csv` | 41 rows. **Exactly one row per `(hospital_ein, code, code_type, payer_canonical, billing_class)`** where at least one side has a dollar rate. 33 columns covering identity, both sides' median/min/max rates, a dispersion count per side, three comparability columns (`tic_negotiation_types` plus the boolean flags `hpt_methodology_mixed` and `cms_baseline_mixed`), two rate-corroboration columns (`rate_value_corroborated`, `corroborated_values`), the CMS baseline, deltas, and provenance fields. |
| `out/unified_rates.csv.stats.json` | Pipeline counters from the run (filter counts, collapse counts, match-level counts, and rate-corroboration counts). Quoted throughout this README so the numbers stay live. |

**Schema note:** `hpt_methodology_mixed` and `cms_baseline_mixed` are nullable booleans (`True` / `False` / blank where the flag does not apply, e.g. a `tic_only` row has no HPT methodology to mix). In the CSV they serialize as `True` / `False` / empty — read them as nullable booleans; do not `astype(bool)`, which would coerce the blanks to `True`.

Run summary from the latest execution:

![Pipeline funnel: TiC 222 and HPT 2,950 input rows narrow to 34 and 37 match groups; a two-pass match yields 41 unified rows (20 strict, 13 relaxed, 4 hpt_only, 4 tic_only); Pass 3 flags 6 blocks / 11 rows with exact cross-side dollar agreement, including the $6,438 UHC 43239 match.](docs/pipeline_funnel.svg)

In one line: 222 TiC + 2,950 HPT rows → 34 + 37 match groups → **41 unified rows** (20 strict, 13 relaxed, 4 hpt-only, 4 tic-only), with **Pass 3** corroborating 6 blocks / 11 rows on exact cross-side dollar agreement (including the brief's $6,438 UHC × 43239 case). The figure is generated from `out/unified_rates.csv.stats.json` (`python tools/plot_funnel.py`), so it cannot drift from the data.

---

## Approach

### Schema choices

The unified grain is `(hospital_ein, code, code_type, payer_canonical, billing_class)`. That is the smallest grain at which a cross-file comparison is meaningful: dropping `billing_class` collapses facility and physician rates onto one row; adding plan or network multiplies rows where one side aggregates several networks against the other side's single plan.

Both inputs are collapsed to that grain before matching, and the collapse is symmetric:

- TiC publishes one row per NPI sub-group sharing a rate; I aggregate to `tic_rate_median / min / max` with `tic_npi_group_count`.
- HPT publishes one row per plan posting; I aggregate to `hpt_rate_median / min / max` with `hpt_plan_count`.

Each output row therefore carries an HPT rate, a TiC rate, or both with `rate_delta` and `rate_delta_pct` (median vs median). I deliberately do **not** emit a "recommended rate." Picking one number from two genuinely different sources hides exactly the structural disagreement the assignment is testing. The dispersion columns (`*_min`, `*_max`, the two counts) keep that disagreement visible instead.

`cms_baseline_rate` is aggregated two ways for two reasons. It is the **median** across sub-groups (not `first`) because ~15 of 34 TiC keys disagree on the baseline at the sub-group level, so `first` would have been order-dependent. `cms_baseline_mixed` then flags the subset of those disagreements that actually **cross schedule classes** — 8 of 37 keys, where the median blended e.g. PFS_FACILITY with PFS_NONFACILITY or OPPS with PFS. (Multi-locality within one class is normal MAC variance and is not flagged.) So: median because 15/34 disagree; flag because 8/37 of the disagreements are cross-class and make the median a blended number rather than one baseline. `cms_baseline_schedule` joins the distinct schedule(s) so the variation stays visible.

The informational columns — kept for analyst review, not for joining on — include `hpt_payer_raw`, `plan_name_hpt` (a representative plan; see `hpt_plan_count` for how many were collapsed), `code_description`, `network_name_tic`, and `hpt_setting`.

### When is a delta actually comparable?

`rate_delta` is only a like-for-like number when both sides are quoting the same *kind* of dollar. They often are not, and the pipeline does not silently pretend otherwise — it exposes the three signals needed to judge it:

- `tic_negotiation_types` — the TiC arrangement(s) behind the rate (`negotiated`, `fee schedule`; `percentage` is dropped upstream). All rows here are `arrangement=ffs`, but a per-diem or bundled TiC rate compared against an HPT case rate would be units-mismatched, and this column is where that shows up at national scale.
- `hpt_methodology_mixed` — `True` when the plan collapse blended more than one HPT methodology (12 rows flag `True`: 10 of the matched rows and 2 of the `hpt_only` rows mix e.g. `case rate` with `percent of total billed charges`). A median across those is not a single negotiated number, so the delta on those rows should be read as *presence/absence*, not magnitude.
- `cms_baseline_mixed` — `True` when the CMS baseline carried by TiC was itself a median across different schedule classes (e.g. PFS_FACILITY blended with PFS_NONFACILITY for the same CPT). The baseline itself is then a blended number, not an authoritative reference; the *delta-vs-baseline* on those rows should not be read as a percent-of-Medicare comparison until the schedule class is fixed downstream.

This is a deliberate **detect-before-correct** choice, not a time constraint. I could enumerate a crosswalk for the arrangements and methodologies present in *this* extract, but a crosswalk that is right for three payers and silently wrong for the fourth hides exactly the mismatches a flag surfaces — and a half-right mapping is more dangerous than an explicit "not comparable" marker. So the pipeline flags the non-comparable rows and leaves the correction to a layer that can be validated against ground truth. A consumer who needs a defensible dollar comparison filters to `hpt_methodology_mixed == False` with a matching arrangement; building and validating the full crosswalk is the highest-value next step (see limitations).

### Normalization (`core/normalizers.py`)

Five things have to go right or every later step is downstream noise. Each is unit-tested.

1. **EIN from the HPT filename, not `license_number`.** Only Montefiore (`131740114`) puts its federal EIN in `license_number`. Mount Sinai stores a NY state operating certificate (`330024`); NYU Langone stores a NY DOH facility ID (`7002053H`). Both are valid CMS-required identifiers, just not EINs. CMS HPT v2 requires the file name to encode the EIN, so the filename prefix is the only reliable cross-file key. Skip this step and recall on this extract collapses from three hospitals to one.
2. **DRG code shape.** NYU Langone writes `"MS-DRG 872"`; the other two write `"872"`. The normalizer strips non-digits and leading zeros for any DRG code type. Matt Robben's Serif blog also flags UHC posting DRGs with a leading zero, which the same rule handles.
3. **Payer canonicalization.** Punctuation-stripped exact match against a 12-entry alias JSON, then a **word-boundary** prefix expansion restricted to full brand keys (`aetna`, `cigna`, `unitedhealthcare`) — so `"cigna ppo"` resolves but `"United Mine Workers"` (a Taft-Hartley fund) and a bare `"ci"` do not. The restriction is deliberate: an unbounded prefix rule mis-resolves unrelated payers silently, which is worse than a thresholded fuzzy match. Fuzzy/embedding resolution is the next step once we have observed misses to tune against (see limitations).
4. **Billing class inference for HPT.** TiC has a `billing_class` column ("institutional" / "professional"); HPT does not. I infer it in priority order: DRG code type → `HC `/`PR ` description prefix → `setting == "inpatient"` → otherwise `unknown`. Ambiguous outpatient CPTs are punted to `unknown` rather than guessed; they are recoverable in the relaxed-class match pass.
5. **Filters with logged counts.** 3 TiC rows with `negotiation_type='percentage'` (these encode "70% of CMS baseline" with rate=70, which corrupts numeric aggregates); 410 HPT `code_type='LOCAL'` rows (all NYU chargemaster items with CPT-shaped numbers); 265 HPT rows with no dollar rate; 1,966 HPT rows whose payer string did not resolve. Every filter is counted in the stats file so a reviewer can reproduce the funnel.

### Matching (`core/match.py`)

Three passes — two that produce matches and one that annotates — all vectorised against the collapsed frames (no per-row Python loop over the join surface):

- **Pass 1 (strict, 20 matches):** join on `(ein, code, code_type, payer, billing_class)`.
- **Pass 2 (billing-class relaxed, 13 matches):** for keys still unmatched, drop the `billing_class` constraint and accept only pairs where the class differs or one side is `unknown`. This recovers rows where my HPT inference said `unknown` but TiC had a clear institutional or professional rate. Each pass-2 row is flagged `billing_class_relaxed=True` so a consumer can weight or discard it.
- **Pass 3 (rate-value corroboration, annotation):** within each `(ein, code, code_type, payer)` block, flag dollar values that appear on both sides regardless of billing class (`rate_value_corroborated`, `corroborated_values`). This is the "matching beyond the join" signal — it recovers the brief's `$6,438` case (see below). It annotates rather than re-pairing, so it never changes the grain (still 41 rows).

Rows unmatched after the first two passes become `hpt_only` (4) or `tic_only` (4). I did not implement a fuzzy-payer pass: with three payers the alias map is sufficient, and a wider net at this scale produces more false positives than it recovers.

### Matching beyond the deterministic join

The two-pass join above is the deterministic baseline. It answers "do these two keys agree exactly?" but the brief is really asking the harder question: "given two records that *might* describe the same negotiated price, how confident are we, and on what evidence?" That is a record-linkage problem, and the principled version has four stages:

1. **Blocking (candidate generation).** Never compare all-vs-all. Block on the cheap, high-recall key — here `(ein, code, code_type)` — to produce a small candidate set per block, then score within it. This is what keeps the approach O(n) per block instead of O(n²) and is the same partitioning that lets it run on Spark/Snowflake at national volume.

2. **Multi-signal scoring, not a single key.** Within a block, score each candidate pair on independent signals rather than demanding they all match exactly: entity (EIN today; EIN↔TIN↔NPI crosswalk at scale), payer (canonical alias → fuzzy → embedding), billing class (inferred, treated as *weak* evidence), plan/network similarity, methodology/arrangement compatibility, and — critically — **rate-value corroboration**: two rates that agree to the dollar are strong positive evidence the pair is real, *even when a weaker signal like billing class disagrees*. The $6,438 case is exactly this: value agreement should override a shaky `HC`/`PR` inference. Today billing class is used as a hard gate; it should be one weighted signal among several.

3. **Calibrated confidence + thresholds.** Combine the signals into a score (a hand-weighted logistic to start; a trained classifier once we have a labeled set), then act on bands: auto-accept high confidence, route the margin to human review, reject the tail. The output already exposes the raw inputs (`match_level`, `billing_class_relaxed`, `hpt_methodology_mixed`, `tic_negotiation_types`, dispersion counts) so a score can be fit on top without reprocessing.

4. **A label loop.** Record-linkage has no free ground truth, so the system has to manufacture it: seed a golden set from cases we can verify (the Mount Sinai `$6,438` match; the Aetna-872 `hpt_only` gap), measure precision/recall against it, and use active learning — send the lowest-confidence pairs to review and feed the labels back. The reviewed margin is where the model improves fastest.

**Stage 2 is implemented here as Pass 3 (rate-value corroboration).** After the two billing-class passes, within each `(ein, code, code_type, payer)` block the matcher intersects the distinct dollar values present on each side — independent of billing-class inference — and annotates every row in that block with `rate_value_corroborated` (bool) and `corroborated_values` (the agreeing dollar amount). On this extract it flags **6 blocks**, including the brief's case: Mount Sinai × UHC × 43239 shows `corroborated_values = 6438.00` on both the institutional and professional rows, surfacing the `$6,438 ↔ $6,438` agreement that the median grain split apart. It does not change the grain (still 41 rows) — it adds the evidence signal. Stages 1, 3, and 4's full confidence model is the documented next step.

---

## The four domain questions (asked in the brief)

### 1. What is DRG 872 and why do DRG rates behave differently from CPT/HCPCS when matching?

MS-DRG 872 is **"Septicemia or severe sepsis without mechanical ventilation >96 hours, without major complications or comorbidities (MCC)"** (871 is the same DRG *with* MCC). It is an inpatient bundle: one payment for the entire admission, inclusive of all institutional services. (The IPPS national base figure carried in TiC's `cms_baseline_rate` for this DRG is $6,829.75; I use that as the baseline rather than recomputing the relative-weight math.)

Three structural consequences for matching:

- **There is no professional side.** A DRG is institutional by definition; physician work for the same admission bills separately under CPT/HCPCS to professional fee schedules. So a DRG row has exactly one valid billing class, while a CPT row can have two — the same `43239` shows up in both the facility's HC bucket and a physician's PR bucket.
- **DRGs have an order-of-magnitude amplitude.** A CPT visit code like `99283` runs from about $1 to $5,200 across this extract (≈$46–$2,740 on the TiC side); DRG 872 runs $6,000–$90,000+. A mis-assigned billing class hides in the noise on CPTs and screams on DRGs.
- **DRGs are bundles, so honest matches can still disagree.** Of the 7 matched DRG rows in this output, 5 post a `Case Rate`, one a `fee schedule`, and one mixes `case rate; percent of total billed charges`. Those are not the same kind of number even when both sides are reporting honestly.

### 2. Does plan type (PPO / EPO / HMO) belong in the matching key or in confidence scoring?

**Confidence scoring, not the join key — at least for this dataset and this question.**

TiC names a `network_name` (`open-access-managed-choice`, `choice-plus`, `national-oap`); HPT names a `plan_name` (`LocalPlus`, `HC NY Essential`, `Medicare`). They overlap conceptually but not lexically, and the mapping is neither published nor stable. Putting plan in the join key would collapse pass-1 to a handful of rows. The cost is real: where TiC posts an Aetna PPO rate and HPT posts an Aetna HMO rate, we report both as a match with a delta — and that delta is structurally explainable, not noise. A plan-alignment score would let a downstream consumer decide how much to trust each match. I keep `plan_name_hpt` and `network_name_tic` in the output so that score can be built later.

### 3. When two records look like a match but rates are materially different, what are legitimate reasons?

In rough order of how often they explain the deltas in this output:

1. **Billing-class drift.** The single biggest cause, and the reason pass 2 exists. Concrete example from this run — Montefiore × CPT 43239 × Aetna:
   - The *professional* side matches cleanly in pass 1: HPT median $421.70 vs TiC professional median $453.63, a 7.6% delta.
   - The *relaxed* match crosses an `unknown`-billing HPT group ($1,394.79) against TiC's institutional rate ($11,000.00), a 689% delta — flagged `billing_class_relaxed=True` precisely so it can be triaged rather than trusted.

   Same hospital, same code, same payer; the clean match and the structural mismatch sit side by side, which is the point.
2. **Methodology mix.** Case Rate vs per-procedure negotiated vs percent-of-charges. The numbers won't agree and shouldn't.
3. **Network/plan mismatch within one payer.** A payer's commercial PPO does not pay what its Medicare Advantage or Marketplace plan pays, even when the file names them all "Aetna."
4. **Effective-date drift.** A 2024 HPT posting against a 2025 TiC rate diverges legitimately.
5. **Percent-of-X postings.** The three dropped `negotiation_type='percentage'` TiC rows would have anchored false deltas.
6. **Hospital posting error or outlier placeholders.** Real but rarer than the five above.

Design implication: a delta column is necessary but not sufficient. A useful downstream view also exposes *why* a delta is large (billing class, methodology, network) so an analyst can filter rather than re-investigate each row.

### 4. The brief's worked examples

> *"The negotiated UHC Commercial reimbursement rate for CPT/HCPCS code 43239 … is listed at 6438 in their hospital file. When you look for the same hospital entity in the UHC payer dataset, the same rate appears. However, both files have additional rates besides 6438 — why? What columns change when the price varies? Can any of the other records be aligned?"*

**The $6,438 match is real, and it is at Mount Sinai (EIN 131624096).** UHC × 43239: the HPT file lists `$6,438.00`, and the TiC institutional rates for that exact key are `{$1,532, $2,670, $4,608, $6,438}` — so `$6,438` appears on **both** sides, as an **institutional** rate. The brief reproduces exactly.

The instructive part is that my current grain does **not** surface it as a clean pair, and the reason is itself a finding:

- **Billing-class inference misroutes it.** The HPT `$6,438` row's description is `"Egd biopsy single/multiple"` — no `HC ` prefix — so my heuristic infers `billing_class = unknown`, and the relaxed pass pairs it with the TiC *professional* group. A different HPT posting, `$1,048.28` carrying the `HC ` prefix, becomes the *institutional* representative instead. So the genuinely-institutional `$6,438` lands on the wrong row.
- **Median collapse then buries it.** The institutional row reports HPT median `$1,048.28` vs TiC median `$3,639` (the `$6,438` survives only in `tic_rate_max`). The penny-for-penny `$6,438 ↔ $6,438` never lines up in one cell.

**"What columns change when the price varies?"** Within a single `(hospital, code, payer)` group, the rate moves with `billing_class` (facility vs professional — the biggest lever), the TiC NPI sub-group / network, and the HPT `plan_name` / `methodology`. **"Can any of the other records be aligned?"** Yes — and the pipeline now does: the **rate-value corroboration pass** (Pass 3, see *Matching beyond the deterministic join*) flags this block with `rate_value_corroborated = True` and `corroborated_values = 6438.00`, so the `$6,438 ↔ $6,438` agreement is recovered as an evidence signal even though the median grain placed the two postings on different rows. That is the line-item alignment the brief is asking for, surfaced without over-trusting the billing-class inference that split them.

The literal `$29,259.18` is in this sample — but at **Mount Sinai**, not Montefiore (the same wrong-hospital twist as the `$6,438` example; the brief's attribution looks drawn from an older sample). It is a Mount Sinai HPT posting for Aetna × DRG 872.

> *"Montefiore Medical Center lists Aetna PPO rates for DRG 872 at 29259.18. Do you see that rate in the Aetna TIC extract? (Hint: You will not.)"*

The hint holds, but the mechanism is richer than a simple absence, because TiC **does** carry Aetna DRG 872 (6 rows, all Mount Sinai institutional, median **$13,365**):

- **Mount Sinai × Aetna × 872 is a matched (`both`) row, not a gap.** HPT posts a median **$26,918** across 4 plans (range $13,708–$36,574, which contains the brief's $29,259.18); TiC pays a median **$13,365** — roughly **half** (−50%). The key matches, but the hospital's posted DRG rate is far from the payer's, and the exact $29,259.18 has no TiC counterpart — so "you will not see that rate" is true *as a divergent match*, the question-3 case on a DRG.
- **Montefiore (3 plans, $13,434–$51,351) and NYU (18 plans, $10,077–$93,157) × Aetna × 872 are true gaps** — no Aetna 872 on the TiC side for those hospitals, so they collapse to `data_source=hpt_only`.

So the example exercises both halves of what the brief is probing: a divergent DRG match where hospital and payer disagree by ~2×, and one-sided `hpt_only` rows. Commercial DRG postings being dense on the hospital side and sparse-or-divergent on the payer side is the structural pattern, not a bug.

### Worked example I *do* have

One of the cleanest matches in the output:

| field | value |
|---|---|
| hospital | NYU Langone (EIN 133971298) |
| code | MS-DRG 872 |
| payer | Cigna |
| billing_class | institutional |
| HPT rate | median $29,907.00 across 17 plans (range $24,149.58–$29,907.00), Case Rate methodology |
| TiC rate | median $29,907.25 |
| Δ | **$0.25** |
| CMS baseline (IPPS) | $6,829.75 |

The hospital and the payer agree to the penny on what Cigna pays NYU for a DRG 872 admission, at 4.4× the CMS IPPS base. That 17 plan postings collapse to a single headline rate (most post $29,907; the range is $24,149.58–$29,907.00) is itself the signal the collapse is meant to surface: this is essentially one negotiated number, not seventeen.

---

## Operating at national scale: expected fill rates

The binding constraint as this scales is not compute — it is how much of each side survives to a matchable state. On this extract the funnel already shows where the loss is:

- **Payer resolution is the dominant filter.** 1,966 of 2,950 HPT rows (~67%) drop because the payer string is not one of the three I aliased. Nationally that long tail explodes — BCBS subsidiaries, regional Medicaid MCOs, third-party administrators — so a naïve alias map's HPT keep-rate *falls* before the resolver in item 1 is built. Expect payer resolution, not throughput, to set the match rate.
- **HPT dollar fill is partial and uneven.** 265 rows here publish no `negotiated_dollar` (percent-of-charges only); across hospitals that share varies widely, and a chargemaster join is needed to dollarize them.
- **Identifier conventions are inconsistent.** The EIN-in-filename convention held for all three hospitals here, but it is not universal; `license_number` carried a federal EIN for only one of three. At scale this needs the NPPES-seeded EIN↔TIN↔NPI crosswalk, not a filename heuristic.
- **Code-type vocabularies collide.** 410 `LOCAL`/chargemaster rows carried CPT-shaped values that would false-match; this grows with hospital count.
- **TiC fill is comparatively clean** on rate/code/payer, but `billing_class` and NPI-list completeness vary, which is what forces the inference and collapse choices above.

Net expectation: as payer and hospital counts grow, raw match *rate* falls until payer resolution and entity resolution are hardened. Those two are the gates; I would instrument drop-reason fill rates per payer and per hospital and treat a rising `unresolved-payer` share as the primary signal that the resolver needs new training data.

## Testing and release in production

How I would ship and operate this, beyond the unit and pipeline tests already in the repo:

- **Golden-set regression.** A small, hand-labeled set of known-true and known-absent cases (the Mount Sinai `$6,438` match; the Montefiore Aetna-872 `hpt_only` gap) that must reproduce on every release — the linkage equivalent of a snapshot test.
- **Data contracts at ingest.** Schema and type validation per source file; quarantine files that fail rather than letting them corrupt the run; version the payer-alias map and log every resolution decision for audit.
- **Fill-rate and drop-reason monitoring.** Track `LOCAL` / `no-dollar` / `unresolved-payer` rates per payer and hospital and alert on drift; a sudden jump in unresolved-payer means a new name variant to alias.
- **Match-quality dashboards.** Distributions of `rate_delta_pct`, the strict-vs-relaxed share, and the mixed-methodology / mixed-baseline share over time, so silent regressions in match quality surface before a consumer notices.
- **Canary releases.** Roll a new matcher version on a few payers/states first, diff match counts and delta distributions against the previous version, and require sign-off on large swings before national rollout.
- **Idempotent, partitioned reruns.** Partition by EIN so any hospital can be reprocessed independently and every output is reproducible from `(inputs, alias version, code version)`.

## Limitations and follow-ups

In priority order, what I would do with another day:

1. **Payer-name resolution at scale.** The alias map plus word-boundary prefix is safe for three payers; it will not survive Anthem's BCBS subsidiaries or regional Medicaid MCOs. Next: alias → rapidfuzz → an LLM tiebreaker on residuals, with the LLM decision logged for audit. Serif's own [blog on payer matching](https://www.serifhealth.com/blog/payer-name-matching-price-transparency) describes essentially this progression.
2. **Plan / NPI explode.** I collapse HPT across plans and TiC across NPI sub-groups to hit the stated grain. The reverse operation — exploding either dimension to ask "which plan, or which physician group, drove the outlier?" — is supported by both schemas and is the natural next view.
3. **Plan-type alignment score.** As discussed in question 2.
4. **Arrangement↔methodology crosswalk (the comparability gate).** The highest-value next step. Map TiC `negotiation_type`/`arrangement` (ffs, per-diem, bundle, percent) to HPT `standard_charge_methodology` (case rate, fee schedule, percent-of-charges), so a delta is only computed between cells that represent the same kind of dollar — and convert percent-of-charges postings to expected dollars via the chargemaster. Today the relevant fields are exposed (`tic_negotiation_types`, `hpt_methodology_mixed`, `hpt_rate_methodology`, `cms_baseline_mixed`) so a consumer can filter to comparable pairs; the crosswalk makes that automatic and auditable.
5. **Production scaling.** The matching *logic* is a wide-then-narrow shuffle on `(ein, code, code_type, payer, billing_class)`: partitionable by EIN, broadcast-joinable on the small payer alias table — the blocking strategy in *Matching beyond the deterministic join*. It is the join shape, not this pandas code, that ports to Spark/Snowflake; the per-row `.apply` normalizers are the first thing to push into vectorized/UDF form. This implementation does not run at 300B rows; the join design does. (Fill-rate and release mechanics are covered in the two sections above.)

---

## Repo layout

```
serif_takehome/
├── README.md                       # this file
├── core/
│   ├── normalizers.py              # the five normalization rules
│   ├── match.py                    # matcher (2 join passes + corroboration) + schema + collapse
│   ├── payer_aliases.json          # 12-entry alias map
│   └── run.py                      # CLI entry point
├── tests/
│   ├── test_normalizers.py         # field-level unit tests + over-match guard
│   └── test_match.py               # end-to-end pipeline tests on a synthetic fixture
├── tools/
│   └── plot_funnel.py              # renders docs/pipeline_funnel.svg from stats.json
├── docs/
│   └── pipeline_funnel.svg         # the funnel figure embedded above
├── data/
│   ├── tic_extract_20250213.csv
│   └── hpt_extract_20250213.csv
└── out/                            # generated; gitignored in a real repo
    ├── unified_rates.csv
    └── unified_rates.csv.stats.json
```
