"""Convert explicitly as-traded transactions to the current share basis."""

from datetime import date
from decimal import Decimal, localcontext

from portfolio_lens.models import ALREADY_ADJUSTED, AS_TRADED, SplitEvent


def normalize_records(
    records: list[dict],
    splits: dict[str, tuple[SplitEvent, ...]],
) -> list[dict]:
    """Copy records, adjusting quantity and unit price by splits after each trade.

    Split events use new-shares / old-shares ratios; a reverse split has a ratio
    below one. Already-adjusted rows are never rescaled. Raw values and the raw
    convention remain in the copied record, and the adjusted convention makes
    repeated normalization safe. Split coverage is the caller's responsibility.
    """
    normalized = []
    with localcontext() as context:
        context.prec = 50
        for record in records:
            adjusted = dict(record)
            convention = record.get("split_convention", ALREADY_ADJUSTED)
            adjusted.setdefault("raw_quantity", record["quantity"])
            adjusted.setdefault("raw_price", record["price"])
            adjusted.setdefault("raw_split_convention", convention)
            if convention == AS_TRADED:
                trade_date = date.fromisoformat(record["date"])
                ratio = Decimal("1")
                for event in splits.get(record["ticker"], ()):
                    if event.date > trade_date:
                        if not event.ratio.is_finite() or event.ratio <= 0:
                            raise ValueError(f"Invalid split ratio for {record['ticker']}.")
                        ratio *= event.ratio
                adjusted["quantity"] = format(Decimal(str(record["quantity"])) * ratio, "f")
                adjusted["price"] = format(Decimal(str(record["price"])) / ratio, "f")
                adjusted["split_convention"] = ALREADY_ADJUSTED
            elif convention != ALREADY_ADJUSTED:
                raise ValueError(f"Unknown split convention: {convention}.")
            normalized.append(adjusted)
    return normalized
