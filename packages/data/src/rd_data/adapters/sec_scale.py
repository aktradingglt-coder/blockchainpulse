"""Scale-error checks for SEC company facts (D-036).

XBRL facts are entered with a scale (units, thousands, millions); a wrong one makes a value exactly a power of 1000 off.
Nothing in a filing marks such a value as wrong, so it is found from the rest of the same EDGAR response: the same
period in other filings, neighbouring periods, sums across periods, the same year's shorter year-to-date, exact
accounting identities, the company's own size, and the filing's own comparatives.
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from datetime import date, timedelta

from rd_data.schemas import FundamentalFact

#: How close (in log10) two filings' values must be to a ratio of 10^3, 10^6, ... to count as a scale error.
SCALE_TOLERANCE = 0.02
#: Neighbouring periods (same concept, unit and duration) a fact is compared with, within NEIGHBOUR_WINDOW_DAYS: a
#: line last reported a decade ago says nothing about this year's scale.
NEIGHBOURS = 4
NEIGHBOUR_WINDOW_DAYS = 5 * 366
#: Orders of magnitude from the neighbours beyond which a fact is flagged (genuine one-offs reach ~3 on real data).
FLAG_DISTANCE = 2.5
#: Flags (not holds) are raised only on the most recent filings, so years-old one-offs do not clutter every response.
FLAG_FILINGS = 4
#: Orders of magnitude from the neighbours beyond which a fact is held whatever the concept (none on real data).
HOLD_DISTANCE = 3.5
#: A concept whose facts usually sit within 10^0.15 (x1.4) of their neighbours is "stable": there, a fact about a power
#: of 1000 (+/- 10^0.5) from its neighbours is held, not just flagged.
STABLE_SPREAD = 0.15
NEAR_POWER = 0.5
#: How much closer (orders of magnitude) to its neighbours the reference filing must be to decide a disagreement.
DECISIVE_MARGIN = 1.0
#: Share of an accession's comparative facts that must be a power of 1000 from other filings to hold the whole filing.
FILING_SCALE_SHARE = 0.5
#: Relative tolerance for "January-June = January-March + April-June" (each value is rounded where it was reported).
ADDITIVITY_TOLERANCE = 0.01
#: Share of a relation's largest term that leaving one side of a repair out must exceed (beyond twice the repaired
#: residual) for that side to count as needed: rounding to the reported unit stays far below it.
NEEDED_SHARE = 1e-4
_CURRENCY = re.compile(r"^[A-Z]{3}$")
#: Held facts needed (with FILING_SCALE_SHARE of the column) before a filing's whole column for one date is held.
COLUMN_MIN_HELD = 3
#: A filing's column for one date is held whole when at least COLUMN_SHIFT_MIN of its currency facts can be compared
#: with their neighbouring periods (other filings; or the filing's own periods within PREVIOUS_WINDOW_DAYS when other
#: filings report too few) and the column sits a power of 1000 from them: the
#: median step within NEAR_POWER of it, and COLUMN_SHIFT_SHARE of the steps within 10^1. A whole statement never moves
#: that way in clean data; for a young filer it is often the only evidence there is.
COLUMN_SHIFT_MIN = 10
COLUMN_SHIFT_SHARE = 0.6
PREVIOUS_WINDOW_DAYS = 400
#: A currency fact larger than this multiple of the company's own total assets is held. Across about 400,000 clean
#: us-gaap facts of 15 large filers, outside revenue and cost lines, the largest ratio is 1.5 (XOM gross PP&E); the only
#: larger ones are option intrinsic values entered at the wrong scale (NVDA FY2012-13, MMM Q1-2022: 4.1x to 14.9x).
MAX_ASSETS_MULTIPLE = 3.0
#: Revenue, cost and purchase lines scale with asset turnover (2.3x at most in the clean data, AMZN 2011 revenue),
#: which is higher for low-margin distributors and traders not among the calibration filers: they are held only from
#: this multiple, still far below where a x1000 error lands.
TURNOVER_ASSETS_MULTIPLE = 20.0
_TURNOVER = re.compile(r"Revenue|CostOf|CostsAndExpenses|OperatingExpenses|Purchases|SalesOf")
#: Amounts that are not the company's own and legitimately exceed its balance sheet (derivative notionals, client
#: assets under management or custody), cumulative equity accounts (a long-loss-making company's accumulated deficit
#: and paid-in capital can dwarf what it has left) and the dei taxonomy (public float is a market value) are exempt.
_NOT_BOUNDED_BY_ASSETS = re.compile(r"Notional|UnderManagement|UnderCustody|Custod|Fiduciary")
#: Cumulative equity balances (instants only: their flows are bounded like any other line).
_CUMULATIVE_EQUITY = re.compile(
    r"PaidInCapital|RetainedEarnings|AccumulatedDeficit|StockholdersEquity|TreasuryStockValue"
)
#: Below this total (in the reporting currency) a company's assets say little about the size of its line items.
MIN_ASSETS_FOR_BOUND = 1e7
#: Tax amounts (expense and its parts, reconciliation lines, taxes paid, deferred-tax and uncertain-tax balances) are
#: held from this multiple of total assets: the largest of about 30,000 clean tax facts in 15 large filers is 0.43x.
TAX_ASSETS_MULTIPLE = 1.0
_TAX_AMOUNT = re.compile(
    r"IncomeTax|TaxExpenseBenefit|TaxReconciliation|IncomeTaxesPaid|TaxPaid|UnrecognizedTaxBenefits|DeferredTax"
    r"|ValuationAllowance|TaxCredit"
)
_NOT_A_TAX_AMOUNT = re.compile(r"BeforeIncomeTax|NetOfTax|AfterTax|BeforeTax|NetOfIncomeTax")
#: Changes in a balance (working-capital changes, period increases) and other comprehensive income are held from this
#: multiple of total assets: clean maxima 0.34x, 0.61x and 0.14x respectively.
CHANGE_ASSETS_MULTIPLE = 1.0
_CHANGE = re.compile(r"^IncreaseDecreaseIn|PeriodIncreaseDecrease|^OtherComprehensiveIncome")
#: Cash-flow lines (gross proceeds, payments and repayments; net cash by activity) are held from this multiple: clean
#: maxima 0.96x (AAPL purchases of securities) and 0.72x.
FLOW_ASSETS_MULTIPLE = 2.0
_FLOW = re.compile(r"^(Proceeds|Payments|Repayments)|^NetCashProvidedByUsedIn")
#: Headline totals whose sign filers do not re-tag between filings: a sign disagreement or a sign-only break of a
#: period sum in these is checked (in other lines, conventions for OCI, hedge and allowance amounts flip often).
SIGN_CHECKED = frozenset(
    {
        "NetIncomeLoss",
        "ProfitLoss",
        "OperatingIncomeLoss",
        "GrossProfit",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeTaxExpenseBenefit",
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInFinancingActivities",
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "CostOfRevenue",
        "OperatingExpenses",
        "ComprehensiveIncomeNetOfTax",
    }
)
#: When the sign history does not single out one side of a sign error, hold every candidate (True) or none (False).
SIGN_UNDECIDED_HOLDS = False
#: Balances and totals that cannot be negative: none of about 28,000 clean facts of these concepts is. A negative one
#: is a sign error and is held.
NEVER_NEGATIVE = frozenset(
    {
        "Assets",
        "AssetsCurrent",
        "AssetsNoncurrent",
        "Liabilities",
        "LiabilitiesCurrent",
        "LiabilitiesAndStockholdersEquity",
        "CashAndCashEquivalentsAtCarryingValue",
        "Cash",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentGross",
        "Goodwill",
        "IntangibleAssetsNetExcludingGoodwill",
        "InventoryNet",
        "AccountsReceivableNetCurrent",
        "AccountsPayableCurrent",
        "LongTermDebt",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    }
)
#: Largest identity system (terms sharing identities, e.g. the 12-line income-tax table) solved by search.
MAX_IDENTITY_TERMS = 14
#: A group of facts this close to its own history (orders of magnitude) is taken as correctly scaled.
CONSISTENT_DISTANCE = 1.0
#: Exact US-GAAP identities, checked within one filing and period: total = sum of parts. Applied only where every term
#: is reported; on the recorded AMZN facts all 261 instances hold to within 1%.
IDENTITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("IncomeTaxExpenseBenefit", ("CurrentIncomeTaxExpenseBenefit", "DeferredIncomeTaxExpenseBenefit")),
    (
        "CurrentIncomeTaxExpenseBenefit",
        ("CurrentFederalTaxExpenseBenefit", "CurrentStateAndLocalTaxExpenseBenefit", "CurrentForeignTaxExpenseBenefit"),
    ),
    (
        "DeferredIncomeTaxExpenseBenefit",
        (
            "DeferredFederalIncomeTaxExpenseBenefit",
            "DeferredStateAndLocalIncomeTaxExpenseBenefit",
            "DeferredForeignIncomeTaxExpenseBenefit",
        ),
    ),
    (
        "IncomeTaxExpenseBenefit",
        (
            "FederalIncomeTaxExpenseBenefitContinuingOperations",
            "StateAndLocalIncomeTaxExpenseBenefitContinuingOperations",
            "ForeignIncomeTaxExpenseBenefitContinuingOperations",
        ),
    ),
    (
        "FederalIncomeTaxExpenseBenefitContinuingOperations",
        ("CurrentFederalTaxExpenseBenefit", "DeferredFederalIncomeTaxExpenseBenefit"),
    ),
    (
        "StateAndLocalIncomeTaxExpenseBenefitContinuingOperations",
        ("CurrentStateAndLocalTaxExpenseBenefit", "DeferredStateAndLocalIncomeTaxExpenseBenefit"),
    ),
    (
        "ForeignIncomeTaxExpenseBenefitContinuingOperations",
        ("CurrentForeignTaxExpenseBenefit", "DeferredForeignIncomeTaxExpenseBenefit"),
    ),
    (
        "IncomeTaxesPaidNet",
        (
            "IncomeTaxPaidFederalAfterRefundReceived",
            "IncomeTaxPaidStateAndLocalAfterRefundReceived",
            "IncomeTaxPaidForeignAfterRefundReceived",
        ),
    ),
    (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign",
        ),
    ),
    (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign",
        ),
    ),
    ("SellingGeneralAndAdministrativeExpense", ("SellingAndMarketingExpense", "GeneralAndAdministrativeExpense")),
    ("LiabilitiesAndStockholdersEquity", ("Assets",)),
)
#: A filing's comparatives for a line (at least MIN_COMPARATIVES of them, e.g. a 10-K's two prior years) must agree
#: within this many orders of magnitude to vouch for its scale. One comparative (a 10-Q's prior-year quarter) proves
#: nothing about a line that moves a lot.
MIN_COMPARATIVES = 2
COMPARATIVE_SPREAD = 0.5
#: Statement identities with signs: (concept, coefficient) terms summing to zero within one filing and period. Each
#: held on every instance, or all but a handful, of the clean company facts of 15 large filers (D-036).
#: A term of a signed identity: the concepts that may report it (first reported wins), its coefficient, and whether the
#: identity holds without it (an exchange-rate line a filer with no foreign cash does not report).
_Term = tuple[tuple[str, ...], float, bool]


def _term(*concepts: str, coef: float, optional: bool = False) -> _Term:
    return (concepts, coef, optional)


_CASH_CHANGE = (
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
    "CashAndCashEquivalentsPeriodIncreaseDecrease",
)
_FX_ON_CASH = (
    "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAnd"
    "DiscontinuedOperations",
    "EffectOfExchangeRateOnCashAndCashEquivalents",
)
_REVENUE = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")
_AFTER_TAX = (
    "IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest",
    "IncomeLossFromContinuingOperations",
    "ProfitLoss",
    "NetIncomeLoss",
)
#: A sign-flip repair must close its relations to within this share of the largest term: reported figures are exact up
#: to rounding, and a flip that merely lands inside ADDITIVITY_TOLERANCE is a coincidence, not evidence.
FLIP_TOLERANCE = 0.001
SIGNED_IDENTITIES: tuple[tuple[_Term, ...], ...] = (
    (
        _term(*_CASH_CHANGE, coef=1.0),
        _term("NetCashProvidedByUsedInOperatingActivities", coef=-1.0),
        _term("NetCashProvidedByUsedInInvestingActivities", coef=-1.0),
        _term("NetCashProvidedByUsedInFinancingActivities", coef=-1.0),
        _term(*_FX_ON_CASH, coef=-1.0, optional=True),
    ),
    (
        _term("ComprehensiveIncomeNetOfTax", coef=1.0),
        _term("NetIncomeLoss", coef=-1.0),
        _term(
            "OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent",
            "OtherComprehensiveIncomeLossNetOfTax",
            coef=-1.0,
        ),
    ),
    (
        _term("ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest", coef=1.0),
        _term("ProfitLoss", coef=-1.0),
        _term("OtherComprehensiveIncomeLossNetOfTax", coef=-1.0),
    ),
    (
        _term("ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest", coef=1.0),
        _term("ComprehensiveIncomeNetOfTax", coef=-1.0),
        _term("ComprehensiveIncomeNetOfTaxAttributableToNoncontrollingInterest", coef=-1.0),
    ),
    (
        _term("ProfitLoss", coef=1.0),
        _term("NetIncomeLoss", coef=-1.0),
        _term("NetIncomeLossAttributableToNoncontrollingInterest", coef=-1.0),
    ),
    (  # income from continuing operations (or net income) = pre-tax income (equity-method income included) - tax
        _term(*_AFTER_TAX, coef=1.0),
        _term("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", coef=-1.0),
        _term("IncomeTaxExpenseBenefit", coef=1.0),
    ),
    (  # the same, before equity-method income, which is then added after tax
        _term(*_AFTER_TAX, coef=1.0),
        _term(
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
            coef=-1.0,
        ),
        _term("IncomeTaxExpenseBenefit", coef=1.0),
        _term("IncomeLossFromEquityMethodInvestments", coef=-1.0, optional=True),
    ),
    (  # pre-tax income = revenue - all costs and expenses (filers whose costs include non-operating items)
        _term("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", coef=1.0),
        _term(*_REVENUE, coef=-1.0),
        _term("CostsAndExpenses", coef=1.0),
    ),
    (
        _term("OtherComprehensiveIncomeLossCashFlowHedgeGainLossAfterReclassificationAndTax", coef=1.0),
        _term("OtherComprehensiveIncomeLossCashFlowHedgeGainLossBeforeReclassificationAfterTax", coef=-1.0),
        _term("OtherComprehensiveIncomeLossCashFlowHedgeGainLossReclassificationAfterTax", coef=1.0),
    ),
    (
        _term("OtherComprehensiveIncomeLossAvailableForSaleSecuritiesAdjustmentNetOfTax", coef=1.0),
        _term("OtherComprehensiveIncomeUnrealizedHoldingGainLossOnSecuritiesArisingDuringPeriodNetOfTax", coef=-1.0),
        _term("OtherComprehensiveIncomeLossReclassificationAdjustmentFromAOCIForSaleOfSecuritiesNetOfTax", coef=1.0),
    ),
    (
        _term("GrossProfit", coef=1.0),
        _term("RevenueFromContractWithCustomerExcludingAssessedTax", coef=-1.0),
        _term("CostOfGoodsAndServicesSold", coef=1.0),
    ),
    (_term("GrossProfit", coef=1.0), _term("Revenues", coef=-1.0), _term("CostOfRevenue", coef=1.0)),
    (
        _term("OperatingIncomeLoss", coef=1.0),
        _term("GrossProfit", coef=-1.0),
        _term("OperatingExpenses", coef=1.0),
    ),
    (
        _term("OperatingIncomeLoss", coef=1.0),
        _term(*_REVENUE, coef=-1.0),
        _term("CostsAndExpenses", coef=1.0),
    ),
    (_term("Assets", coef=1.0), _term("AssetsCurrent", coef=-1.0), _term("AssetsNoncurrent", coef=-1.0)),
    (
        _term("Liabilities", coef=1.0),
        _term("LiabilitiesCurrent", coef=-1.0),
        _term("LiabilitiesNoncurrent", coef=-1.0),
    ),
)
#: Statutory tax in the rate reconciliation = statutory rate x pre-tax income (whichever pre-tax total is reported).
STATUTORY_RATE = "EffectiveIncomeTaxRateReconciliationAtFederalStatutoryIncomeTaxRate"
STATUTORY_AMOUNT = "IncomeTaxReconciliationIncomeTaxExpenseBenefitAtFederalStatutoryIncomeTaxRate"
PRETAX_TOTALS = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
)
#: Rate-reconciliation lines (currency amounts, the statutory amount included): total tax = their signed sum.
_RECONCILING = ("IncomeTaxReconciliation", "EffectiveIncomeTaxRateReconciliation")
_OCI_TOTALS = (
    "OtherComprehensiveIncomeLossNetOfTax",
    "OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent",
)


def _oci_part(concept: str) -> bool:
    """An after-tax OCI component (not a total, not a pre-tax, tax or arising/reclassification sub-line)."""
    inner = concept.replace("AfterReclassification", "")
    return (
        concept.startswith("OtherComprehensiveIncome")
        and concept.endswith(("NetOfTax", "AfterReclassificationAndTax"))
        and concept not in _OCI_TOTALS
        and "Parent" not in concept
        and "Reclassification" not in inner
        and "Arising" not in concept
    )


def _oci_parent_part(concept: str) -> bool:
    return (
        concept.startswith("OtherComprehensiveIncome")
        and concept.endswith(("PortionAttributableToParent", "Parent"))
        and concept not in _OCI_TOTALS
    )


def _utb_movement(concept: str) -> bool:
    return concept.startswith("UnrecognizedTaxBenefits") and any(
        w in concept for w in ("Increase", "Decrease", "Reduction")
    )


#: Relations learned per filing (_learned_relations): (total, is a line, balance for a rollforward or None).
LEARNED: tuple[tuple[str | None, Callable[[str], bool], str | None], ...] = (
    ("IncomeTaxExpenseBenefit", lambda c: c.startswith(_RECONCILING), None),
    ("OtherComprehensiveIncomeLossNetOfTax", _oci_part, None),
    ("OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent", _oci_parent_part, None),
    (None, _utb_movement, "UnrecognizedTaxBenefits"),
)
_LEARNED_PREFIXES = (*_RECONCILING, "OtherComprehensiveIncome", "UnrecognizedTaxBenefits")
#: Most lines a learned relation may have (signs are searched exhaustively).
MAX_LEARNED_LINES = 12
#: Largest group of facts tried as the mis-scaled side of a system larger than MAX_IDENTITY_TERMS.
MAX_GROUP_IN_LARGE_SYSTEM = 3
#: (change over a period, balance): the change equals the closing minus the opening balance, from the same filing.
CHANGES: tuple[tuple[str, str], ...] = (
    ("GoodwillPeriodIncreaseDecrease", "Goodwill"),
    (
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
)
#: A balance (instant fact) in a recent filing that moved more than 10^INSTANT_FLAG_STEP from the previous period end
#: is flagged: balances move smoothly (3 genuine moves that large in 17 years of AMZN balances).
INSTANT_FLAG_STEP = 2.0
#: Year-to-date ratio checks need this many other years, whose ratios sit within 10^PREFIX_SPREAD of their median.
PREFIX_HISTORY = 3
PREFIX_SPREAD = 0.3


def _scale_like(log_ratio: float, tolerance: float = SCALE_TOLERANCE) -> bool:
    k = round(log_ratio / 3)
    return k != 0 and abs(log_ratio - 3 * k) < tolerance


def _duration(f: FundamentalFact) -> int | None:
    return None if f.period_start is None else round((f.period_end - f.period_start).days / 91)


def scale_errors(records: list[FundamentalFact]) -> tuple[set[int], set[int]]:
    """XBRL scale errors (thousands entered as units, millions as thousands): (quarantine, flag). See D-036.

    Each fact is compared with the nearest NEIGHBOURS periods of the same concept, unit and duration. Measured on the
    recorded AMZN company facts (28,891 facts, 9,052 periods filed more than once): legitimate restatements reach a
    cross-filing ratio of 41 (a 20:1 split), and genuine one-offs sit up to ~10^3 from their neighbours, so:

    * held — within one period, filings a power of 1000 from the filing closest to the neighbours; a fact ~a power of
      1000 from its neighbours on a stable concept; any fact more than 10^HOLD_DISTANCE from its neighbours; and every
      fact of a filing whose comparatives are mostly a power of 1000 from the other filings (a mis-scaled filing);
    * held — facts that break "a period = its two halves" (e.g. January-June = January-March + April-June) when
      scaling one filing's facts in that sum by a power of 1000 makes it add up;
    * held — a year-to-date fact whose ratio to the same year's shorter year-to-date (from other filings: the year
      against its nine months, nine months against the half) is about a power of 1000 off the ratio other years show;
    * held — a held fact's siblings: the same filing's other facts of that concept and period end (a scale is set per
      table, so e.g. the trailing-twelve-month figure beside a mis-scaled quarter shares its error); and the whole
      column (one filing, one period end) once most of it is held;
    * flagged — any other fact more than 10^FLAG_DISTANCE from its neighbours (kept, for review).

    Whenever two filings are weighed against each other, neighbours reported by either of them are left out, so a
    mis-scaled filing never vouches for itself or against the other.
    """
    logs = [math.log10(abs(f.value)) if f.value else None for f in records]
    by_period: dict[_PeriodKey, list[int]] = {}
    for i, f in enumerate(records):
        if logs[i] is not None:
            by_period.setdefault((f.concept, f.unit, _duration(f), f.period_end), []).append(i)
    neighbours = _Neighbours(records, logs, by_period)
    distance = {
        i: d
        for idx in by_period.values()
        for i in idx
        if (d := neighbours.distance(i, frozenset({records[i].accession}))) is not None
    }
    held, flagged = _fact_holds(records, distance)
    held |= {i for i, f in enumerate(records) if f.value < 0 and f.concept in NEVER_NEGATIVE}
    held |= _filing_disagreements(records, logs, by_period, neighbours)
    held |= _sign_disagreements(records, by_period, neighbours)
    held |= _additivity_errors(records, neighbours)
    held |= _prefix_errors(records, logs, neighbours)
    held |= _comparative_errors(records, logs)
    held |= _column_shifts(records, logs, neighbours)
    held |= _identity_errors(records, held, neighbours)
    recent = _recent_accessions(records)
    for i, f in enumerate(records):
        step = neighbours.previous_step(i) if f.period_start is None and f.accession in recent else None
        if step is not None and step > INSTANT_FLAG_STEP:
            flagged.add(i)
    held |= _beyond_assets(records, logs, held)
    tables = {(records[i].concept, records[i].unit, records[i].accession, records[i].period_end) for i in held}
    held |= {i for i, f in enumerate(records) if (f.concept, f.unit, f.accession, f.period_end) in tables}
    # A filing's column (its facts for one period end) that is mostly held is held whole: the scale was set for the
    # column, and the facts that slipped through the other checks share it.
    column: dict[tuple[str, date], list[int]] = {}
    for i, f in enumerate(records):
        column.setdefault((f.accession, f.period_end), []).append(i)
    for idx in column.values():
        hits = sum(i in held for i in idx)
        if hits >= COLUMN_MIN_HELD and hits / len(idx) >= FILING_SCALE_SHARE:
            held.update(idx)
    held |= _identity_errors(records, held, neighbours)  # tables that add up around a held term share its scale
    return held, flagged - held


def current_columns(records: list[FundamentalFact]) -> dict[str, list[int]]:
    """Each filing's current-period column: its us-gaap currency facts for its latest period end reported with at least
    half as many facts as its busiest one (a 10-K's prior-year end can carry more facts than its current one). A
    cover-page public float or a single later-dated footnote fact does not move it, and a filing with no such facts (a
    cover-only amendment) has none."""
    ends: dict[str, Counter[date]] = {}
    money = [i for i, f in enumerate(records) if _CURRENCY.match(f.unit) and f.taxonomy != "dei"]
    for i in money:
        ends.setdefault(records[i].accession, Counter())[records[i].period_end] += 1
    main = {acc: max(e for e, n in c.items() if 2 * n >= max(c.values())) for acc, c in ends.items()}
    columns: dict[str, list[int]] = {}
    for i in money:
        if records[i].period_end == main[records[i].accession]:
            columns.setdefault(records[i].accession, []).append(i)
    return columns


_SeriesKey = tuple[str, str, int | None]
_PeriodKey = tuple[str, str, int | None, date]


class _Neighbours:
    """Orders of magnitude between a fact and the nearest NEIGHBOURS periods of its series (same concept, unit and
    duration), leaving out the values reported by the given filings."""

    def __init__(
        self, records: list[FundamentalFact], logs: list[float | None], by_period: dict[_PeriodKey, list[int]]
    ) -> None:
        self.records = records
        self.logs = logs
        self.series: dict[_SeriesKey, dict[date, list[tuple[str, float]]]] = {}
        for (concept, unit, dur, end), idx in by_period.items():
            self.series.setdefault((concept, unit, dur), {})[end] = [
                (records[i].accession, v) for i in idx if (v := logs[i]) is not None
            ]
        self.signed: dict[_SeriesKey, dict[date, dict[str, float]]] = {}
        for (concept, unit, dur, end), idx in by_period.items():
            self.signed.setdefault((concept, unit, dur), {})[end] = {
                records[i].accession: records[i].value for i in idx
            }
        self._nearest: dict[tuple[_SeriesKey, date], list[date]] = {}

    def distance(self, i: int, exclude: frozenset[str]) -> float | None:
        levels = self._levels(i, exclude)
        value = self.logs[i]
        if len(levels) < 2 or value is None:
            return None
        return abs(value - statistics.median(levels))

    def _levels(self, i: int, exclude: frozenset[str]) -> list[float]:
        """The nearest NEIGHBOURS periods' levels (median log value per period), leaving out the given filings."""
        f = self.records[i]
        key = (f.concept, f.unit, _duration(f))
        periods = self.series.get(key, {})
        nearest = self._nearest.get((key, f.period_end))
        if nearest is None:
            window = [e for e in periods if e != f.period_end and abs((e - f.period_end).days) <= NEIGHBOUR_WINDOW_DAYS]
            nearest = sorted(window, key=lambda e: abs((e - f.period_end).days))
            self._nearest[(key, f.period_end)] = nearest
        levels: list[float] = []
        for e in nearest:
            values = [v for acc, v in periods[e] if acc not in exclude]
            if values:
                levels.append(statistics.median(values))
            if len(levels) == NEIGHBOURS:
                break
        return levels

    def sign_against_history(self, i: int, exclude: frozenset[str]) -> bool:
        """Whether a fact's sign is the opposite of most of its (at least two) nearest neighbouring periods, as other
        filings report them."""
        f = self.records[i]
        periods = self.series.get((f.concept, f.unit, _duration(f)), {})
        nearest = self._nearest.get(((f.concept, f.unit, _duration(f)), f.period_end))
        if nearest is None:
            self.distance(i, exclude)
            nearest = self._nearest.get(((f.concept, f.unit, _duration(f)), f.period_end), [])
        signs: list[float] = []
        for e in nearest:
            values = [
                self.signed[(f.concept, f.unit, _duration(f))][e][acc] for acc, _ in periods[e] if acc not in exclude
            ]
            if values:
                signs.append(math.copysign(1.0, statistics.median(values)))
            if len(signs) == NEIGHBOURS:
                break
        opposite = sum(sg != math.copysign(1.0, f.value) for sg in signs)
        return len(signs) >= 2 and 2 * opposite > len(signs)

    def level_step(self, i: int) -> float | None:
        """Signed orders of magnitude to the fact from the same period as other filings report it, else from its
        neighbouring periods in other filings, else (a line no other filing reports) from the other periods its own
        filing reports beside it."""
        f = self.records[i]
        value = self.logs[i]
        if value is None:
            return None
        periods = self.series.get((f.concept, f.unit, _duration(f)), {})
        same = [v for acc, v in periods.get(f.period_end, []) if acc != f.accession]
        if same:
            return value - statistics.median(same)
        levels = self._levels(i, frozenset({f.accession}))
        if levels:
            return value - statistics.median(levels)
        own = [
            statistics.median(values)
            for e, reported in periods.items()
            if e != f.period_end
            and abs((e - f.period_end).days) <= PREVIOUS_WINDOW_DAYS
            and (values := [v for acc, v in reported if acc == f.accession])
        ]
        return value - statistics.median(own) if own else None

    def own_distance(self, i: int) -> float | None:
        """Orders of magnitude between a fact and the other periods its own filing reports for the same line (the
        comparatives beside it): the fallback evidence for a line that no other filing reports."""
        f = self.records[i]
        value = self.logs[i]
        periods = self.series.get((f.concept, f.unit, _duration(f)), {})
        levels = [
            statistics.median(own)
            for e, reported in periods.items()
            if e != f.period_end and (own := [v for acc, v in reported if acc == f.accession])
        ]
        if len(levels) < MIN_COMPARATIVES or max(levels) - min(levels) > COMPARATIVE_SPREAD or value is None:
            return None  # too few, or disagreeing, comparatives say nothing about this cell's scale
        return abs(value - statistics.median(levels))

    def previous_step(self, i: int) -> float | None:
        """Orders of magnitude between a fact and the previous period end of its series, as other filings report it."""
        f = self.records[i]
        value = self.logs[i]
        periods = self.series.get((f.concept, f.unit, _duration(f)), {})
        earlier = sorted((e for e in periods if e < f.period_end), reverse=True)
        for e in earlier:
            values = [v for acc, v in periods[e] if acc != f.accession]
            if values and value is not None:
                return abs(value - statistics.median(values))
        return None

    def mean_distance(self, part: Sequence[int], exclude: frozenset[str]) -> float | None:
        found = [d for i in part if (d := self.distance(i, exclude)) is not None]
        return statistics.mean(found) if len(found) == len(part) else None


def _recent_accessions(records: list[FundamentalFact]) -> set[str]:
    """The FLAG_FILINGS most recently filed accessions: the filings reports actually use."""
    filed_on: dict[str, date] = {}
    for f in records:
        filed_on[f.accession] = max(filed_on.get(f.accession, f.filed), f.filed)
    return set(sorted(filed_on, key=filed_on.__getitem__)[-FLAG_FILINGS:])


def _column_shifts(records: list[FundamentalFact], logs: list[float | None], neighbours: _Neighbours) -> set[int]:
    """A filing's column for one date (its currency facts) that sits a power of 1000 from the neighbouring periods of
    its series: the scale was set for the whole statement (see COLUMN_SHIFT_MIN). Neighbours are medians of several
    periods, so one mis-scaled earlier column does not make the next, genuine one look shifted."""
    columns: dict[tuple[str, date], list[int]] = {}
    for i, f in enumerate(records):
        if logs[i] is not None and _CURRENCY.match(f.unit) and f.taxonomy != "dei":
            columns.setdefault((f.accession, f.period_end), []).append(i)
    out: set[int] = set()
    for idx in columns.values():
        steps = [d for i in idx if (d := neighbours.level_step(i)) is not None]
        if len(steps) < COLUMN_SHIFT_MIN:
            continue
        centre = statistics.median(steps)
        power = round(centre / 3)
        if power == 0 or abs(centre - 3 * power) > NEAR_POWER:
            continue
        if sum(abs(d - 3 * power) <= 1 for d in steps) >= COLUMN_SHIFT_SHARE * len(steps):
            out.update(idx)
    return out


def _comparative_errors(records: list[FundamentalFact], logs: list[float | None]) -> set[int]:
    """A filing's newest period of a concept against the comparatives the same filing reports (the prior years beside
    it in the same table, same unit and duration). When those comparatives agree with each other (within
    10^COMPARATIVE_SPREAD) and the newest cell is about a power of 1000 away from them, the cell is held: the filing
    itself shows the scale it uses for that line. This needs no other filing, so it covers lines first reported in a
    new table, whose history is only their own comparatives."""
    series: dict[tuple[str, str, str, int | None], dict[date, list[float]]] = {}
    members: dict[tuple[str, str, str, int | None, date], list[int]] = {}
    for i, f in enumerate(records):
        if (v := logs[i]) is not None:
            key = (f.accession, f.concept, f.unit, _duration(f))
            series.setdefault(key, {}).setdefault(f.period_end, []).append(v)
            members.setdefault((*key, f.period_end), []).append(i)
    held: set[int] = set()
    for key, periods in series.items():
        if len(periods) < 1 + MIN_COMPARATIVES:
            continue
        newest = max(periods)
        levels = [statistics.median(v) for e, v in periods.items() if e != newest]
        if max(levels) - min(levels) > COMPARATIVE_SPREAD:
            continue  # the line itself swings between years: no scale to read off its comparatives
        reference = statistics.median(levels)
        for i in members[(*key, newest)]:
            off = (logs[i] or 0.0) - reference
            if abs(off) > FLAG_DISTANCE and _scale_like(off, NEAR_POWER):
                held.add(i)
    return held


def _assets_multiple(concept: str) -> float:
    if _TAX_AMOUNT.search(concept) and not _NOT_A_TAX_AMOUNT.search(concept):
        return TAX_ASSETS_MULTIPLE
    if _CHANGE.search(concept):
        return CHANGE_ASSETS_MULTIPLE
    if _FLOW.search(concept):
        return FLOW_ASSETS_MULTIPLE
    if _TURNOVER.search(concept):
        return TURNOVER_ASSETS_MULTIPLE
    return MAX_ASSETS_MULTIPLE


def _beyond_assets(records: list[FundamentalFact], logs: list[float | None], held: set[int]) -> set[int]:
    """Currency facts too large for the company: more than MAX_ASSETS_MULTIPLE times its total assets (same currency),
    TAX_ASSETS_MULTIPLE times for a tax amount, CHANGE_ASSETS_MULTIPLE for changes and OCI, FLOW_ASSETS_MULTIPLE for
    cash-flow lines and TURNOVER_ASSETS_MULTIPLE for revenue and cost lines. Total assets is the median of the
    NEIGHBOURS nearest period ends, each as reported by other filings where there are any, leaving out facts already
    held: one mis-scaled filing cannot shrink the yardstick and hold the newest genuine figures."""
    assets: dict[tuple[str, date], list[tuple[str, float]]] = {}
    for i, f in enumerate(records):
        if f.concept == "Assets" and f.period_start is None and i not in held and (v := logs[i]) is not None:
            assets.setdefault((f.unit, f.period_end), []).append((f.accession, v))
    ends: dict[str, list[date]] = {}
    for unit, end in assets:
        ends.setdefault(unit, []).append(end)
    floor = math.log10(MIN_ASSETS_FOR_BOUND)
    out: set[int] = set()
    for i, f in enumerate(records):
        value = logs[i]
        if value is None or f.unit not in ends or f.taxonomy == "dei" or _NOT_BOUNDED_BY_ASSETS.search(f.concept):
            continue
        if f.period_start is None and _CUMULATIVE_EQUITY.search(f.concept):
            continue
        nearest = sorted(ends[f.unit], key=lambda e: abs((e - f.period_end).days))[:NEIGHBOURS]
        levels = []
        for e in nearest:
            reported = assets[(f.unit, e)]
            others = [v for acc, v in reported if acc != f.accession] or [v for _, v in reported]
            levels.append(statistics.median(others))
        reference = statistics.median(levels)
        if reference < floor:
            continue
        multiple = _assets_multiple(f.concept)
        if value - reference > math.log10(multiple):
            out.add(i)
    return out


_Instance = tuple[tuple[int, float], ...]


def _identity_errors(records: list[FundamentalFact], held: set[int], neighbours: _Neighbours) -> set[int]:
    """Exact relations within one filing (IDENTITIES, statutory tax = rate x pre-tax income, CHANGES), solved together:
    relations that share facts form one system. A broken system is repaired by rescaling a group of its facts by a
    power of 1000 so that every relation holds again (each repaired relation needing every term). Scaling a group up is
    the same as scaling the rest down, so the two sides are weighed against their own earlier periods (_blame); when
    that does not decide, the whole system is held. A relation that holds as reported around a held fact is held with
    it when its other facts are off their own history too (the table was mis-scaled together); when they sit on their
    history, the relation holding is evidence that the held fact is right, not a reason to hold more."""
    footnotes, statements = _instances(records)
    instances = footnotes + statements
    broken = [inst for inst in instances if not _balances(_contributions(records, inst))]
    consistent = [inst for inst in instances if inst not in broken]
    out: set[int] = set()
    # Statement identities (SIGNED_IDENTITIES) and footnote tables are solved as separate systems: joined through a
    # shared total (income tax), the tax table would exceed MAX_IDENTITY_TERMS and lose its whole-table repairs.
    explained: set[_Instance] = set()
    for system in [*_systems(footnotes), *_systems(statements)]:
        if any(inst in broken for inst in system):
            held_here, fixed = _repair(records, system, [inst for inst in system if inst in broken], neighbours)
            out |= held_here
            explained |= fixed
    for system in _systems(instances):
        unexplained = [inst for inst in system if inst in broken and inst not in explained]
        if unexplained:
            system_broken = [inst for inst in system if inst in broken]
            out |= _sign_repair(records, system, system_broken, unexplained, neighbours)
    known = held | out
    for _ in range(len(IDENTITIES)):  # spread through nested identities (total -> current -> current federal)
        spread: set[int] = set()
        for inst in consistent:
            terms = {k for k, _ in inst}
            rest = terms - known
            if rest and terms & known and _off_history(neighbours, records, rest):
                spread |= rest
        if not spread:
            break
        known |= spread
        out |= spread
    return out


def _instances(records: list[FundamentalFact]) -> tuple[list[_Instance], list[_Instance]]:
    """Every exact relation the response lets us check, as signed terms that sum to zero: (footnote and tax-table
    relations, statement identities)."""
    wanted = {c for total, parts in IDENTITIES for c in (total, *parts)} | {STATUTORY_AMOUNT, *PRETAX_TOTALS}
    wanted |= {c for pair in CHANGES for c in pair}
    wanted |= {c for terms in SIGNED_IDENTITIES for concepts, _, _ in terms for c in concepts}
    index: dict[tuple[str, str, date | None, date], dict[str, int]] = {}
    rates: dict[tuple[str, date | None, date], float] = {}
    for i, f in enumerate(records):
        if (f.concept in wanted or f.concept.startswith(_LEARNED_PREFIXES)) and _CURRENCY.match(f.unit):
            index.setdefault((f.accession, f.unit, f.period_start, f.period_end), {})[f.concept] = i
        elif f.concept == STATUTORY_RATE and f.unit == "pure":
            rates[(f.accession, f.period_start, f.period_end)] = f.value
    out: list[_Instance] = []
    statements: list[_Instance] = []
    for (accession, unit, start, end), terms in index.items():
        for total, parts in IDENTITIES:
            if total in terms and all(p in terms for p in parts):
                out.append(((terms[total], 1.0), *((terms[p], -1.0) for p in parts)))
        for signed in SIGNED_IDENTITIES:
            if (instance := _signed_instance(signed, terms)) is not None:
                statements.append(instance)
        rate = rates.get((accession, start, end))
        pretax = next((terms[c] for c in PRETAX_TOTALS if c in terms), None)
        if rate and STATUTORY_AMOUNT in terms and pretax is not None:
            out.append(((terms[STATUTORY_AMOUNT], 1.0), (pretax, -rate)))
        if start is None:
            continue
        for change, balance in CHANGES:
            closing = index.get((accession, unit, None, end), {}).get(balance)
            opening = index.get((accession, unit, None, start - timedelta(days=1)), {}).get(balance)
            if change in terms and closing is not None and opening is not None:
                out.append(((terms[change], 1.0), (closing, -1.0), (opening, 1.0)))
    return out + _learned_relations(records, index), statements


def _signed_instance(signed: tuple[_Term, ...], terms: dict[str, int]) -> _Instance | None:
    """One filing's and period's instance of a signed identity: each term reported under the first of its concepts the
    filing uses; None when a required term is not reported at all."""
    instance: list[tuple[int, float]] = []
    for concepts, coef, optional in signed:
        found = next((terms[c] for c in concepts if c in terms), None)
        if found is not None:
            instance.append((found, coef))
        elif not optional:
            return None
    return tuple(instance)


def _learned_relations(
    records: list[FundamentalFact], index: dict[tuple[str, str, date | None, date], dict[str, int]]
) -> list[_Instance]:
    """Relations whose lines and signs vary by filer (LEARNED: the rate reconciliation, OCI and its parts, the
    unrecognized-tax-benefit rollforward). A filing reports such a table for two or three years; the signs that close it
    in all its years but at most one (and in at least two) are learned from the filing itself, and only then is the
    relation used. A table no sign assignment closes is left alone: its tagging is the filer's own."""
    cells: dict[tuple[str, str], dict[tuple[date | None, date], dict[str, int]]] = {}
    for (accession, unit, start, end), terms in index.items():
        cells.setdefault((accession, unit), {})[(start, end)] = terms
    out: list[_Instance] = []
    for table in cells.values():
        for total, is_line, balance in LEARNED:
            rows: list[tuple[list[tuple[int, float]], dict[str, int]]] = []
            for (start, end), terms in table.items():
                lines = {c: i for c, i in terms.items() if is_line(c)}
                if start is None or not lines:
                    continue
                if balance is None:
                    if total in terms:
                        rows.append(([(terms[total], 1.0)], lines))
                    continue
                closing = table.get((None, end), {}).get(balance)
                opening = table.get((None, start - timedelta(days=1)), {}).get(balance)
                if closing is not None and opening is not None:
                    rows.append(([(closing, 1.0), (opening, -1.0)], lines))
            names = sorted({c for _, lines in rows for c in lines})
            if len(rows) < 2 or len(names) > MAX_LEARNED_LINES:
                continue
            signs = _learn_signs(records, rows, names)
            if signs is not None:
                out += [(*anchor, *((lines[c], -signs[c]) for c in sorted(lines))) for anchor, lines in rows]
    return out


def _learn_signs(
    records: list[FundamentalFact], rows: list[tuple[list[tuple[int, float]], dict[str, int]]], names: list[str]
) -> dict[str, float] | None:
    """The signs that close the most years; among those, the ones that close them most exactly. A line far smaller than
    the table's largest term (an opening balance) closes it either way within the tolerance, but only its true sign
    leaves no residual beyond rounding."""
    best: dict[str, float] | None = None
    best_score = (0, 0.0)
    for combination in itertools.product((1.0, -1.0), repeat=len(names)):
        signs = dict(zip(names, combination, strict=True))
        closed, residual = 0, 0.0
        for anchor, lines in rows:
            terms = [
                *(coef * records[i].value for i, coef in anchor),
                *(-signs[c] * records[i].value for c, i in lines.items()),
            ]
            if _balances(terms):
                closed += 1
                residual += abs(sum(terms)) / max(abs(v) for v in terms)
        if (closed, -residual) > (best_score[0], -best_score[1]) or best is None:
            best, best_score = signs, (closed, residual)
    if best_score[0] < 2 or best_score[0] < len(rows) - 1:
        return None
    return best


def _contributions(
    records: list[FundamentalFact], inst: _Instance, group: AbstractSet[int] = frozenset(), factor: float = 1.0
) -> list[float]:
    return [coef * records[k].value * (factor if k in group else 1.0) for k, coef in inst]


def _off_history(neighbours: _Neighbours, records: list[FundamentalFact], facts: set[int]) -> bool:
    """Whether these facts sit away from their own history (or have none to sit on)."""
    found = [d for i in facts if (d := neighbours.distance(i, frozenset({records[i].accession}))) is not None]
    return not found or statistics.median(found) > CONSISTENT_DISTANCE


def _systems(instances: list[_Instance]) -> list[list[_Instance]]:
    """Group relations that share facts."""
    systems: list[list[_Instance]] = []
    for inst in instances:
        facts = {k for k, _ in inst}
        joined = [s for s in systems if any(facts & {k for k, _ in other} for other in s)]
        merged = [inst, *(other for s in joined for other in s)]
        systems = [s for s in systems if s not in joined] + [merged]
    return systems


def _repair(
    records: list[FundamentalFact], system: list[_Instance], broken: list[_Instance], neighbours: _Neighbours
) -> tuple[set[int], set[_Instance]]:
    """Find groups of facts whose power-of-1000 rescale makes broken relations close without breaking any that closed;
    blame one side of each (_blame). Relations that no rescale closes (a filer's own tagging) are left alone. When the
    history cannot decide, the facts of the relations that rescale would close are held. Returns the facts held and the
    broken relations some rescale explains."""
    terms = sorted({k for inst in system for k, _ in inst})
    largest = len(terms) // 2 if len(terms) <= MAX_IDENTITY_TERMS else MAX_GROUP_IN_LARGE_SYSTEM
    own = frozenset({records[terms[0]].accession})
    held: set[int] = set()
    explained: set[_Instance] = set()
    repairs: list[set[int]] = []
    for size in range(1, largest + 1):
        for group in itertools.combinations(terms, size):
            rest = [k for k in terms if k not in group]
            if size * 2 == len(terms) and rest < list(group):
                continue  # an even split is the same pair seen from the other side
            if any(found <= set(group) for found in repairs):
                continue  # only minimal repairs: adding a line too small to matter repairs nothing new
            fixed = next(
                (f for k in (-2, -1, 1, 2) if (f := _fixed(records, system, broken, set(group), 1000.0**k))), []
            )
            if not fixed:
                continue
            repairs.append(set(group))
            explained.update(fixed)
            involved = {k for inst in fixed for k, _ in inst}
            blamed = _blame(neighbours, list(group), [k for k in rest if k in involved], own)
            held |= set(blamed) & involved if blamed is not None else involved
    return held, explained


def _sign_repair(
    records: list[FundamentalFact],
    system: list[_Instance],
    broken: list[_Instance],
    unexplained: list[_Instance],
    neighbours: _Neighbours,
) -> set[int]:
    """Broken relations no rescale explains may be explained by a single fact entered with the wrong sign: flipping it
    closes the relation exactly (FLIP_TOLERANCE) without breaking any relation of the system (statement identities and
    footnote tables together, so a total that another identity confirms is never the one flipped). The fact is held
    when its sign opposes most of its history, or when it is the only fact whose flip closes that relation; otherwise
    nothing is held (SIGN_UNDECIDED_HOLDS)."""
    terms = sorted({k for inst in system for k, _ in inst})
    own = frozenset({records[terms[0]].accession})
    flips = [
        (k, fixed)
        for k in terms
        if (fixed := _fixed(records, system, broken, {k}, -1.0))
        and all(_exact(_contributions(records, inst, {k}, -1.0)) for inst in fixed)
    ]
    flipped: set[int] = set()
    for inst in unexplained:  # decided per broken relation: a system can join several periods' statements
        candidates = [k for k, fixed in flips if inst in fixed]
        against = {k for k in candidates if neighbours.sign_against_history(k, own)}
        if against:
            flipped |= against
        elif len(candidates) == 1:
            flipped.add(candidates[0])
        elif candidates and SIGN_UNDECIDED_HOLDS:
            flipped |= {k for k, _ in inst}
    return flipped


def _fixed(
    records: list[FundamentalFact], system: list[_Instance], broken: list[_Instance], group: set[int], factor: float
) -> list[_Instance]:
    """The broken relations this rescale closes (each needing every term), or none if it breaks a closed one."""
    closed = []
    for inst in system:
        contributions = _contributions(records, inst, group, factor)
        if inst not in broken:
            if not _balances(contributions):
                return []
        elif _closes_with_both_sides(contributions, [k in group for k, _ in inst]):
            closed.append(inst)
    return closed


def _closes_with_both_sides(contributions: Sequence[float], rescaled: Sequence[bool]) -> bool:
    """The relation closes after the rescale, and needs both the rescaled terms and the others to do so: leaving either
    side out must leave a residual well beyond the repaired one (a rescale that only shrinks its terms into
    insignificance, or dwarfs the rest, proves nothing). A genuine line far smaller than the relation's largest term
    still counts when the repaired relation closes much more exactly with it than without it."""
    if not (_balances(contributions) and any(rescaled) and not all(rescaled)):
        return False
    residual = abs(sum(contributions))
    floor = 2 * residual + NEEDED_SHARE * max(abs(v) for v in contributions)
    without_group = abs(sum(0.0 if r else v for v, r in zip(contributions, rescaled, strict=True)))
    without_rest = abs(sum(v if r else 0.0 for v, r in zip(contributions, rescaled, strict=True)))
    return without_group > floor and without_rest > floor


def _blame(neighbours: _Neighbours, one: list[int], other: list[int], own: frozenset[str]) -> list[int] | None:
    """Which of two groups is mis-scaled, judged by how far each sits from its own earlier periods (as other filings
    report them; members with no history are left out of the mean). A group with no history at all is blamed when the
    other side sits well within its history (under 10^CONSISTENT_DISTANCE), and cleared when the other side is far."""
    a, b = _known_mean(neighbours, one, own), _known_mean(neighbours, other, own)
    if a is not None and b is not None:
        if abs(a - b) >= DECISIVE_MARGIN:
            return one if a > b else other
        return None
    if a is None and b is None:
        return None
    known, unknown, score = (one, other, a) if a is not None else (other, one, b)
    assert score is not None  # noqa: S101 - one side is known here
    if score < CONSISTENT_DISTANCE:
        return unknown
    if score > FLAG_DISTANCE:
        return known
    return None


def _known_mean(neighbours: _Neighbours, group: list[int], own: frozenset[str]) -> float | None:
    """Mean distance of a group from its history: other filings' neighbouring periods where they exist. A line no
    other filing reports falls back on its own filing's comparatives, but only as evidence against it (far from them):
    a row mis-scaled in every year agrees with itself, so agreement there vouches for nothing."""
    found: list[float] = []
    for i in group:
        d = neighbours.distance(i, own)
        if d is None and (d := neighbours.own_distance(i)) is not None and d <= FLAG_DISTANCE:
            d = None
        if d is not None:
            found.append(d)
    return statistics.mean(found) if found else None


def _exact(contributions: Sequence[float]) -> bool:
    """Signed terms that should sum to zero do, within FLIP_TOLERANCE of the largest term."""
    largest = max(abs(v) for v in contributions)
    return largest == 0 or abs(sum(contributions)) <= FLIP_TOLERANCE * largest


def _balances(contributions: Sequence[float]) -> bool:
    """Signed terms that should sum to zero do, within ADDITIVITY_TOLERANCE of the largest term."""
    largest = max(abs(v) for v in contributions)
    return largest == 0 or abs(sum(contributions)) <= ADDITIVITY_TOLERANCE * largest


def _fact_holds(records: list[FundamentalFact], distance: dict[int, float]) -> tuple[set[int], set[int]]:
    """Per fact: held when far beyond what its series ever does, flagged (recent filings only) when unusual."""
    spread: dict[_SeriesKey, list[float]] = {}
    for i, d in distance.items():
        f = records[i]
        spread.setdefault((f.concept, f.unit, _duration(f)), []).append(d)
    typical = {k: statistics.median(v) for k, v in spread.items()}
    recent = _recent_accessions(records)
    held: set[int] = set()
    flagged: set[int] = set()
    for i, d in distance.items():
        f = records[i]
        stable = typical[(f.concept, f.unit, _duration(f))] < STABLE_SPREAD
        if d > HOLD_DISTANCE or (d > FLAG_DISTANCE and stable and _scale_like(d, NEAR_POWER)):
            held.add(i)
        elif d > FLAG_DISTANCE and f.accession in recent:
            flagged.add(i)  # history is not re-flagged on every response: only the filings reports actually use
    return held, flagged


def _clear_pick(
    neighbours: _Neighbours, groups: Sequence[Sequence[int]], exclude: frozenset[str], *, nearest: bool
) -> int | None:
    """Index of the group of facts clearly nearest (or furthest from) its neighbouring periods, by DECISIVE_MARGIN,
    judged without the disputing filings' own values; None when the neighbours do not decide."""
    scores = [neighbours.mean_distance(group, exclude) for group in groups]
    known = [d for d in scores if d is not None]
    if len(known) < len(scores):
        return None
    order = sorted(range(len(groups)), key=lambda g: known[g] if nearest else -known[g])
    if len(order) == 1 or abs(known[order[1]] - known[order[0]]) >= DECISIVE_MARGIN:
        return order[0]
    return None


def _majority(records: list[FundamentalFact], groups: Sequence[Sequence[int]]) -> int | None:
    """The group reported by two or more filings when every other group comes from a single filing: one filing's
    error, not a restatement that several filings carry."""
    counts = [len({records[i].accession for i in g}) for g in groups]
    best = max(range(len(groups)), key=lambda g: counts[g])
    if counts[best] >= 2 and all(c == 1 for g, c in enumerate(counts) if g != best):
        return best
    return None


def _sign_disagreements(
    records: list[FundamentalFact], by_period: dict[_PeriodKey, list[int]], neighbours: _Neighbours
) -> set[int]:
    """Periods filed more than once where filings report the same amount with opposite signs: the side that a single
    filing reports against two or more, or else the side whose sign is the opposite of the series' neighbouring periods
    (other filings), is held; when neither decides, both are."""
    held: set[int] = set()
    for (concept, *_), idx in by_period.items():
        if concept not in SIGN_CHECKED or len({records[i].accession for i in idx}) < 2:
            continue
        positive = [i for i in idx if records[i].value > 0]
        negative = [i for i in idx if records[i].value < 0]
        if not positive or not negative:
            continue
        a = statistics.median(records[i].value for i in positive)
        b = statistics.median(records[i].value for i in negative)
        if abs(a + b) > ADDITIVITY_TOLERANCE * max(abs(a), abs(b)):
            continue  # a restatement, not the same amount with its sign flipped
        groups = [positive, negative]
        pick = _majority(records, groups)
        if pick is not None:
            held.update(groups[1 - pick])
            continue
        exclude = frozenset(records[i].accession for i in idx)
        against = [all(neighbours.sign_against_history(i, exclude) for i in g) for g in groups]
        if against.count(True) == 1:
            held.update(groups[against.index(True)])
        elif SIGN_UNDECIDED_HOLDS:
            held.update(idx)
    return held


def _filing_disagreements(
    records: list[FundamentalFact],
    logs: list[float | None],
    by_period: dict[_PeriodKey, list[int]],
    neighbours: _Neighbours,
) -> set[int]:
    """Periods filed more than once: filings a power of 1000 from the reference filing (the one closest to its
    independent neighbours) are held. When the neighbours do not clearly side with one filing, the disagreeing
    filings' facts of that concept are all held (a guess could pick the wrong scale). A filing is held whole when
    most of everything it shares with other filings (not only the disputed periods) is a power of 1000 off."""
    held: set[int] = set()
    off_by_accession: dict[str, list[bool]] = {}
    ambiguous: set[tuple[str, str, str]] = set()
    for idx in by_period.values():
        accessions = frozenset(records[i].accession for i in idx)
        if len(accessions) < 2:
            continue
        values = [v for i in idx if (v := logs[i]) is not None]
        if not any(_scale_like(a - b) for a in values for b in values):
            for i in idx:  # filings agree up to restatements: this period counts for every filing as "on scale"
                off_by_accession.setdefault(records[i].accession, []).append(False)
            continue
        # Group the facts by scale; the group clearly nearest the neighbouring periods is kept.
        groups: list[list[int]] = []
        for i in idx:
            log_i = logs[i]
            group = next((g for g in groups if (v := logs[g[0]]) is not None and log_i is not None
                          and abs(log_i - v) < 1.5), None)  # fmt: skip
            if group is None:
                groups.append([i])
            else:
                group.append(i)
        keep = _clear_pick(neighbours, groups, accessions, nearest=True)
        if keep is None:
            keep = _majority(records, groups)
        if keep is None:
            ambiguous.update((records[i].concept, records[i].unit, records[i].accession) for i in idx)
            continue
        keep_log = statistics.median(v for i in groups[keep] if (v := logs[i]) is not None)
        wrong = [i for i in idx if (v := logs[i]) is not None and _scale_like(v - keep_log)]
        held.update(wrong)
        for i in idx:
            off_by_accession.setdefault(records[i].accession, []).append(i in wrong)
    held.update(i for i, f in enumerate(records) if (f.concept, f.unit, f.accession) in ambiguous)
    bad_filings = {
        acc for acc, offs in off_by_accession.items() if sum(offs) >= 3 and sum(offs) / len(offs) >= FILING_SCALE_SHARE
    }
    held.update(i for i, f in enumerate(records) if f.accession in bad_filings)
    return held


def _additivity_errors(records: list[FundamentalFact], neighbours: _Neighbours) -> set[int]:
    """Currency flows add up across periods: a fact for a period equals the facts for its two consecutive parts
    (year-to-date = earlier year-to-date + latest quarter). The parts usually come from different filings, so a
    mis-scaled filing breaks the sum even when the newest values have no other filing to be compared with."""
    spans: dict[tuple[str, str, date, date], list[int]] = {}
    for i, f in enumerate(records):
        if f.period_start is not None and _CURRENCY.match(f.unit):
            spans.setdefault((f.concept, f.unit, f.period_start, f.period_end), []).append(i)
    starts: dict[tuple[str, str, date], list[date]] = {}
    for concept, unit, start, end in spans:
        starts.setdefault((concept, unit, end), []).append(start)
    held: set[int] = set()
    for (concept, unit, start, end), whole in spans.items():
        for split in starts[(concept, unit, end)]:
            first = spans.get((concept, unit, start, split - timedelta(days=1)))
            if split > start and first:
                for combo in itertools.product(whole, first, spans[(concept, unit, split, end)]):
                    held |= _misscaled_in(records, combo, neighbours)
    return held


def _prefix_errors(records: list[FundamentalFact], logs: list[float | None], neighbours: _Neighbours) -> set[int]:
    """Year-to-date facts against the same year's previous year-to-date (same start, earlier end; the full year
    against its nine months). Their ratio follows the business (a full year is usually about 4/3 of nine months) and
    is checked against the ratio the concept shows in other years, so the newest annual figure is checked against
    numbers from the same year rather than against years-old annual values only."""
    spans: dict[tuple[str, str, date, date], list[int]] = {}
    for i, f in enumerate(records):
        if f.period_start is not None and logs[i] is not None:
            spans.setdefault((f.concept, f.unit, f.period_start, f.period_end), []).append(i)
    ends: dict[tuple[str, str, date], list[date]] = {}
    for concept, unit, start, end in spans:
        ends.setdefault((concept, unit, start), []).append(end)
    chain: list[tuple[tuple[str, str, date, date], tuple[str, str, date, date]]] = []
    history: dict[tuple[str, str, int, int], list[tuple[date, float]]] = {}
    for (concept, unit, start), dates in ends.items():
        dates.sort()
        for shorter, longer in itertools.pairwise(dates):
            whole, part = (concept, unit, start, longer), (concept, unit, start, shorter)
            chain.append((whole, part))
            key = (concept, unit, _quarters(start, longer), _quarters(start, shorter))
            ratio = statistics.median(_known(logs, spans[whole])) - statistics.median(_known(logs, spans[part]))
            history.setdefault(key, []).append((start, ratio))
    held: set[int] = set()
    for whole, part in chain:
        concept, unit, start, longer = whole
        others = [
            r for s, r in history[(concept, unit, _quarters(start, longer), _quarters(start, part[3]))] if s != start
        ]
        if len(others) < PREFIX_HISTORY:
            continue
        expected = statistics.median(others)
        if statistics.median(abs(r - expected) for r in others) > PREFIX_SPREAD:
            continue  # the ratio swings from year to year (e.g. financing flows): no usual ratio to judge by
        for i in spans[whole]:
            prefix = [k for k in spans[part] if records[k].accession != records[i].accession]
            log_i = logs[i]
            if not prefix or log_i is None:
                continue  # one filing alone cannot show its own scale error
            off = log_i - statistics.median(_known(logs, prefix)) - expected
            if abs(off) > FLAG_DISTANCE and _scale_like(off, NEAR_POWER):
                exclude = frozenset({records[i].accession, *(records[k].accession for k in prefix)})
                # the prefix is judged as a "whole" in its own pair; here only this fact can be held, unless the
                # neighbours clearly blame the prefix
                if _clear_pick(neighbours, [[i], prefix], exclude, nearest=False) != 1:
                    held.add(i)
    return held


def _quarters(start: date, end: date) -> int:
    return round((end - start).days / 91)


def _known(logs: list[float | None], idx: Sequence[int]) -> list[float]:
    return [v for i in idx if (v := logs[i]) is not None]


def _adds_up(values: Sequence[float]) -> bool:
    whole, first, second = values
    return abs(whole - first - second) <= ADDITIVITY_TOLERANCE * max(abs(v) for v in values)


def _adds_up_with_every_term(values: Sequence[float]) -> bool:
    """The sum holds and needs all three terms: rescaling one term into insignificance proves nothing."""
    return _adds_up(values) and not any(
        _adds_up([0.0 if j == k else v for j, v in enumerate(values)]) for k in range(3)
    )


def _sign_flipped_in(records: list[FundamentalFact], combo: tuple[int, ...], neighbours: _Neighbours) -> set[int]:
    """The facts of one ``whole = first + second`` sum that one filing's sign error explains: flipping that filing's
    facts makes the sum hold. The filing whose facts oppose the sign of their neighbouring periods (in other filings)
    is held; when that does not decide, every candidate is (nothing else here can tell a restatement of sign)."""
    if records[combo[0]].concept not in SIGN_CHECKED:
        return set()
    values = [records[i].value for i in combo]
    accessions = frozenset(records[i].accession for i in combo)
    fixes: list[list[int]] = []
    for acc in sorted(accessions):
        flipped = [-v if records[i].accession == acc else v for i, v in zip(combo, values, strict=True)]
        whole, first, second = flipped
        if _adds_up_with_every_term(flipped) and _exact([whole, -first, -second]):
            fixes.append([i for i in combo if records[i].accession == acc])
    if not fixes:
        return set()  # neither a scale nor a sign error (e.g. a restatement): left to the other checks
    against = [all(neighbours.sign_against_history(i, accessions) for i in part) for part in fixes]
    if against.count(True) == 1:
        return set(fixes[against.index(True)])
    return {i for part in fixes for i in part} if SIGN_UNDECIDED_HOLDS else set()


def _misscaled_in(records: list[FundamentalFact], combo: tuple[int, ...], neighbours: _Neighbours) -> set[int]:
    """The facts of one ``whole = first + second`` sum that a power-of-1000 scale error in one filing explains."""
    values = [records[i].value for i in combo]
    accessions = frozenset(records[i].accession for i in combo)
    if len(accessions) < 2 or _adds_up(values):
        return set()  # one filing alone cannot show its own scale error; a sum that adds up shows none
    fixes: list[list[int]] = []
    for acc in sorted(accessions):
        part = [i for i in combo if records[i].accession == acc]
        for k in (-2, -1, 1, 2):
            scaled = [v * 1000.0**k if records[i].accession == acc else v for i, v in zip(combo, values, strict=True)]
            if _adds_up_with_every_term(scaled):
                fixes.append(part)
    if not fixes:
        return _sign_flipped_in(records, combo, neighbours)
    # Scaling one filing up is the same as scaling the other down: the side far from its neighbouring periods is the
    # mis-scaled one. Without a clear answer every candidate is held (a guess could serve the wrong scale).
    wrong = _clear_pick(neighbours, fixes, accessions, nearest=False)
    return set(fixes[wrong]) if wrong is not None else {i for part in fixes for i in part}
