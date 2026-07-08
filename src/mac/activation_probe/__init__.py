"""Advisory external-activation classification primitives.

The package operates only on activation tensors supplied by a caller that owns
and instruments the model runtime.  It cannot recover hidden states from hosted
models or from proxy traffic, and it does not implement the Jacobian lens.
Heavyweight open-weight runtimes remain optional.  Nothing in this package
participates in task approval, review, or publication decisions.
"""

from .classifier import ActivationProbeClassifier, ActivationProbePrediction
from .runtime import ActivationBatch, ForwardHookActivationExtractor

__all__ = [
    "ActivationBatch",
    "ActivationProbeClassifier",
    "ActivationProbePrediction",
    "ForwardHookActivationExtractor",
]
