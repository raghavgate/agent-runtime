"""Model-scoped, user-editable estimates; never infer rates for another model."""
import math


def rates_for(config, model_name):
    entry = config.get("pricing", {}).get(model_name)
    if not entry:
        return None
    for key in ("input_usd_per_million", "output_usd_per_million"):
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError("Pricing rates must be finite nonnegative numbers")
    return dict(entry)


def estimate(usage, rates):
    if not rates or usage is None:
        return None
    # Conservative standard input rate; cached-token discounts are not deducted.
    return (usage["input_tokens"] * rates["input_usd_per_million"] +
            usage["output_tokens"] * rates["output_usd_per_million"]) / 1_000_000
