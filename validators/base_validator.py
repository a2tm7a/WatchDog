"""
Base Validator
Abstract base class for all validation rules.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any
from dataclasses import dataclass


@dataclass
class ValidationResult:
    """Represents the result of a validation check."""
    type: str  # e.g., 'BROKEN_LINK', 'PRICE_MISMATCH'
    severity: str  # 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'
    message: str
    course_name: str
    field: str = None  # Which field has the issue
    expected: Any = None
    actual: Any = None
    viewport: str = 'desktop'  # 'desktop' | 'mobile'
    base_url: str = 'Unknown'


class BaseValidator(ABC):
    """
    Abstract base class for validators using Chain of Responsibility pattern.
    Each validator can be chained to the next validator.
    """
    
    def __init__(self):
        self.next_validator = None
    
    def set_next(self, validator: 'BaseValidator') -> 'BaseValidator':
        """
        Set the next validator in the chain.
        Returns the next validator for method chaining.
        """
        self.next_validator = validator
        return validator
    
    def validate(self, course_data: Dict[str, Any]) -> List[ValidationResult]:
        """
        Validate the course data and return a list of issues found.
        Automatically chains to the next validator if set.
        
        Args:
            course_data: Dictionary containing course information
            
        Returns:
            List of ValidationResult objects
        """
        issues = self._validate(course_data)
        
        # Auto-inject viewport and base_url into all issues.
        # course_data is the authoritative source — always overwrite the
        # dataclass defaults so the correct viewport is stamped on every result.
        viewport = course_data.get('viewport', 'desktop')
        base_url = course_data.get('base_url', 'Unknown')
        for issue in issues:
            issue.viewport = viewport
            issue.base_url = base_url
            
        # Chain to next validator
        if self.next_validator:
            issues.extend(self.next_validator.validate(course_data))
        
        return issues
    
    @abstractmethod
    def _validate(self, course_data: Dict[str, Any]) -> List[ValidationResult]:
        """
        Implement the actual validation logic.
        Must be overridden by subclasses.
        
        Args:
            course_data: Dictionary containing course information
            
        Returns:
            List of ValidationResult objects for issues found
        """
        pass
