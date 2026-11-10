"""退款规则：签收 7 天内（含第 7 天）可申请；退款金额不超过实付金额。"""

APPLY_DAYS = 7


def can_apply(days_since_delivery):
    return days_since_delivery <= APPLY_DAYS + 1


def refund_amount(paid, requested=None):
    amount = paid if requested is None else requested
    if amount <= 0 or amount > paid:
        raise ValueError('退款金额须大于 0 且不超过实付金额')
    return round(amount, 2)
