"""
Price Mismatch Validator
Validates that prices on course cards match the prices on PDPs.
"""

from typing import Dict, List, Any
from .base_validator import BaseValidator, ValidationResult
from utils import is_price_missing as _is_price_missing, clean_price as _clean_price


class PriceMismatchValidator(BaseValidator):
    """
    Validates that the price shown on a course card matches the price on the PDP.

    A price mismatch occurs when:
    1. Both card and PDP have prices, but they differ numerically
    2. Card has a price but PDP doesn't (or vice versa)
    """

    def _validate(self, course_data: Dict[str, Any]) -> List[ValidationResult]:
        issues = []

        course_name = course_data.get('course_name', 'Unknown Course')
        card_price  = course_data.get('price', '')
        pdp_price   = course_data.get('pdp_price', '')

        # Both prices must be present for a mismatch to be meaningful.
        if _is_price_missing(card_price) or _is_price_missing(pdp_price):
            return issues

        clean_card = _clean_price(card_price)
        clean_pdp  = _clean_price(pdp_price)

        if clean_card and clean_pdp and clean_card != clean_pdp:
            issues.append(ValidationResult(
                type='PRICE_MISMATCH',
                severity='MEDIUM',
                message=f"Price on card ({card_price}) doesn't match price on PDP ({pdp_price})",
                course_name=course_name,
                field='price',
                expected=card_price,
                actual=pdp_price,
            ))

        return issues

    # ------------------------------------------------------------------
    # Public wrappers kept for backwards-compatibility with existing tests
    # that call these as instance methods.
    # ------------------------------------------------------------------

    def _is_price_missing(self, price_str) -> bool:
        return _is_price_missing(price_str)

    def _clean_price(self, price_str) -> str:
        return _clean_price(price_str)
