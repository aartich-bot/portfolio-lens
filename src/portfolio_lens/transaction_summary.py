"""Read-only transaction summaries; no prices, holdings, storage, or UI dependencies."""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, localcontext


def transaction_rows(records: list[dict]) -> list[dict]:
    """Copy saved records, adding exact trade value and a weekend display flag."""
    with localcontext() as context:
        # Two allowed 12-place inputs can produce 48 significant digits.
        context.prec = 64
        return [
            {
                **record,
                "total_value": Decimal(record["quantity"]) * Decimal(record["price"]),
                "weekend": date.fromisoformat(record["date"]).weekday() >= 5,
            }
            for record in records
        ]


def summarize_transactions(records: list[dict]) -> dict:
    """Aggregate cash spent/received, daily net flows, monthly activity, and broker counts.

    Net invested is cumulative BUY value minus SELL value, not market value or return.
    Empty calendar months between the first and last trade are included as zeroes.
    Decimal values remain exact until the presentation layer prepares chart coordinates.
    """
    rows = transaction_rows(records)
    daily = defaultdict(lambda: Decimal(0))
    monthly = defaultdict(lambda: {"BUY": Decimal(0), "SELL": Decimal(0)})
    brokers = Counter()
    broker_labels = {}
    totals = {"BUY": Decimal(0), "SELL": Decimal(0)}
    with localcontext() as context:
        context.prec = 64
        for row in rows:
            kind, value = row["transaction_type"], row["total_value"]
            totals[kind] += value
            daily[row["date"]] += value if kind == "BUY" else -value
            monthly[row["date"][:7]][kind] += value
            broker_key = row["broker"].casefold()
            broker_labels.setdefault(broker_key, row["broker"])
            brokers[broker_key] += 1
        cumulative = Decimal(0)
        timeline = []
        for day, value in sorted(daily.items()):
            cumulative += value
            timeline.append({"date": day, "net_invested": cumulative})
        calendar = []
        if monthly:
            current, end = min(monthly), max(monthly)
            while current <= end:
                calendar.append({"month": current, **monthly[current]})
                year, month = map(int, current.split("-"))
                current = f"{year + (month == 12):04d}-{month % 12 + 1:02d}"
        return {
            "count": len(rows),
            "bought": totals["BUY"],
            "sold": totals["SELL"],
            "net_invested": totals["BUY"] - totals["SELL"],
            "daily": timeline,
            "monthly": calendar,
            "brokers": [
                {"broker": broker_labels[key], "count": count}
                for key, count in sorted(brokers.items(), key=lambda item: (-item[1], item[0]))
            ],
        }
