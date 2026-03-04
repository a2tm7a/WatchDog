"""
Test script to demonstrate the new modular validation system.
Shows how easy it is to add new validators and use the system.
"""

import sqlite3
from validation_service import ValidationService
from validators import PurchaseCTAValidator, PriceMismatchValidator, ValidationResult


def test_individual_validators():
    """Test validators individually."""
    print("=" * 60)
    print("Testing Individual Validators")
    print("=" * 60)
    
    # Sample course data with issues
    broken_course = {
        'course_name': 'Test Course with Broken Link',
        'base_url': 'https://allen.in/',
        'cta_link': 'https://allen.in/',  # Same as base — link stays on listing, i.e. broken
        'price': '₹ 10,000',
        'pdp_price': '₹ 10,000',
        'cta_status': 'N/A',
        'is_broken': 1,
        'price_mismatch': 0
    }
    
    price_mismatch_course = {
        'course_name': 'Test Course with Price Mismatch',
        'base_url': 'https://allen.in/',
        'cta_link': 'https://allen.in/course',
        'price': '₹ 10,000',
        'pdp_price': '₹ 15,000',  # Different price!
        'is_broken': 0,
        'price_mismatch': 1
    }
    
    # Test PurchaseCTAValidator (covers broken links + missing buy button)
    print("\n1. Testing PurchaseCTAValidator (broken link case):")
    broken_validator = PurchaseCTAValidator()
    issues = broken_validator.validate(broken_course)
    for issue in issues:
        print(f"   [{issue.severity}] {issue.message}")
        print(f"   Course: {issue.course_name}")
    
    # Test PriceMismatchValidator
    print("\n2. Testing PriceMismatchValidator:")
    price_validator = PriceMismatchValidator()
    issues = price_validator.validate(price_mismatch_course)
    for issue in issues:
        print(f"   [{issue.severity}] {issue.message}")
        print(f"   Expected: {issue.expected}, Actual: {issue.actual}")


def test_validator_chain():
    """Test chaining validators together."""
    print("\n" + "=" * 60)
    print("Testing Validator Chain")
    print("=" * 60)
    
    # Course with multiple issues
    multi_issue_course = {
        'course_name': 'Course with Multiple Issues',
        'base_url': 'https://allen.in/',
        'cta_link': 'https://allen.in/',   # Same as base — broken
        'price': '₹ 10,000',
        'pdp_price': '₹ 15,000',           # Price mismatch
        'cta_status': 'N/A',
        'is_broken': 1,
        'price_mismatch': 1
    }
    
    # Build chain: CTA check first, price mismatch second
    cta_validator = PurchaseCTAValidator()
    price_validator = PriceMismatchValidator()
    cta_validator.set_next(price_validator)
    
    # Run validation through chain
    issues = cta_validator.validate(multi_issue_course)
    
    print(f"\nFound {len(issues)} issues:")
    for i, issue in enumerate(issues, 1):
        print(f"\n{i}. {issue.type} [{issue.severity}]")
        print(f"   {issue.message}")


def test_validation_service():
    """Test the full ValidationService."""
    print("\n" + "=" * 60)
    print("Testing ValidationService")
    print("=" * 60)
    
    service = ValidationService()
    
    # Validate all courses in database
    print("\nValidating all courses in database...")
    all_issues = service.validate_all_courses()
    
    # Get summary
    summary = service.get_summary()
    print(f"\nTotal Issues: {summary['total_issues']}")
    print(f"By Type: {summary['by_type']}")
    print(f"By Severity: {summary['by_severity']}")
    
    # Get critical issues
    critical = service.get_issues_by_severity('CRITICAL')
    if critical:
        print(f"\nCritical Issues ({len(critical)}):")
        for issue in critical:
            print(f"  - {issue.course_name}: {issue.message}")


def demonstrate_extensibility():
    """Show how easy it is to add a new validator."""
    print("\n" + "=" * 60)
    print("Demonstrating Extensibility")
    print("=" * 60)
    
    print("""
To add a new validator (e.g., StartDateValidator):

1. Create validators/start_date_validator.py:

    from .base_validator import BaseValidator, ValidationResult
    
    class StartDateValidator(BaseValidator):
        def _validate(self, course_data):
            issues = []
            card_date = course_data.get('start_date')
            pdp_date = course_data.get('pdp_start_date')
            
            if card_date and pdp_date and card_date != pdp_date:
                issues.append(ValidationResult(
                    type='START_DATE_MISMATCH',
                    severity='LOW',
                    message='Start date mismatch',
                    course_name=course_data['course_name'],
                    expected=card_date,
                    actual=pdp_date
                ))
            return issues

2. Add to validators/__init__.py:
    from .start_date_validator import StartDateValidator

3. Add to ValidationService chain:
    start_date = StartDateValidator()
    cta.set_next(price_mismatch).set_next(start_date)

That's it! No changes to scraper.py or handlers needed.
    """)


if __name__ == "__main__":
    print("\n🔍 VALIDATION SYSTEM DEMONSTRATION\n")
    
    test_individual_validators()
    test_validator_chain()
    test_validation_service()
    demonstrate_extensibility()
    
    print("\n" + "=" * 60)
    print("✓ All tests completed!")
    print("=" * 60)
