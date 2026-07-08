"""Advisory J-lens activation auditing primitives.

The package is deliberately independent from the hub and keeps heavyweight
open-weight runtimes optional.  Nothing in this package participates in task
approval, review, or publication decisions.
"""

from .classifier import JLensClassifier, JLensPrediction
from .runtime import ActivationBatch, ForwardHookActivationExtractor

__all__ = [
    "ActivationBatch",
    "ForwardHookActivationExtractor",
    "JLensClassifier",
    "JLensPrediction",
]
