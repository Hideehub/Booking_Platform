from datetime import timedelta

from django import template

register = template.Library()


@register.filter
def money(amount_minor: int, currency: str) -> str:
    """2000000, "NGN" → "NGN 20,000.00". Amounts are stored in minor units."""
    return f"{currency} {amount_minor / 100:,.2f}"


@register.filter
def minutes(duration: timedelta) -> str:
    """timedelta(minutes=90) → "90 min"."""
    return f"{int(duration.total_seconds() // 60)} min"
