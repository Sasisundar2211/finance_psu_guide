from django import template

register = template.Library()


@register.filter
def inr(paise):
    """Render an integer paise amount as INR, e.g. 149900 -> ₹1,499."""
    if paise is None:
        return ""
    rupees, rem = divmod(int(paise), 100)
    return f"₹{rupees:,}.{rem:02d}" if rem else f"₹{rupees:,}"
