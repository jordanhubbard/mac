"""Runtime-neutral residual-stream activation extraction.

The prototype selects Hugging Face model modules plus ordinary forward hooks
as its integration surface.  ``torch`` and ``transformers`` remain optional:
the extractor only requires hookable modules and array-like outputs, so its
contract can be tested with a tiny fake model in the default MAC environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Protocol

import numpy as np


class HookHandle(Protocol):
    def remove(self) -> None: ...


class HookableModule(Protocol):
    def register_forward_hook(self, hook: Any) -> HookHandle: ...


def _as_numpy(value: Any) -> np.ndarray:
    """Convert a torch-like or array-like hook value without importing torch."""
    if isinstance(value, (tuple, list)) and value:
        value = value[0]
    if hasattr(value, "last_hidden_state"):
        value = value.last_hidden_state
    for method in ("detach", "cpu"):
        candidate = getattr(value, method, None)
        if callable(candidate):
            value = candidate()
    candidate = getattr(value, "numpy", None)
    if callable(candidate):
        value = candidate()
    array = np.asarray(value, dtype=float)
    if array.ndim == 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 2:
        raise ValueError(
            "residual activation must have shape [seq_len, hidden_dim] "
            "(or [1, seq_len, hidden_dim])"
        )
    if not np.isfinite(array).all():
        raise ValueError("residual activation contains non-finite values")
    return array


@dataclass(frozen=True)
class ActivationBatch:
    """Per-layer, per-token residual activations from one inference."""

    layers: Mapping[str, np.ndarray]

    def layer(self, name: str) -> np.ndarray:
        try:
            return self.layers[name]
        except KeyError as exc:
            raise KeyError("activation layer not captured: %s" % name) from exc

    def mean_pool(self, name: str) -> np.ndarray:
        return self.layer(name).mean(axis=0)

    def to_jsonable(self) -> Dict[str, Any]:
        return {name: value.tolist() for name, value in self.layers.items()}


class ForwardHookActivationExtractor:
    """Capture selected residual modules during a model forward pass.

    ``layers`` maps stable audit names to hookable model modules.  With a
    Hugging Face causal LM, callers normally provide decoder block modules
    such as ``model.model.layers[8]``.  The hook is removed in ``finally`` so a
    failed inference cannot contaminate subsequent requests.
    """

    def __init__(self, model: Any, layers: Mapping[str, HookableModule]) -> None:
        if not layers:
            raise ValueError("at least one residual layer is required")
        self.model = model
        self.layers = dict(layers)

    def capture(self, *args: Any, **kwargs: Any) -> ActivationBatch:
        captured: Dict[str, np.ndarray] = {}
        handles: list[HookHandle] = []

        def hook_for(name: str):
            def capture_hook(_module: Any, _inputs: Any, output: Any) -> None:
                captured[name] = _as_numpy(output).copy()

            return capture_hook

        try:
            for name, module in self.layers.items():
                handles.append(module.register_forward_hook(hook_for(name)))
            self.model(*args, **kwargs)
        finally:
            for handle in reversed(handles):
                handle.remove()

        missing = sorted(set(self.layers) - set(captured))
        if missing:
            raise RuntimeError(
                "model forward did not emit configured layers: %s" % missing
            )
        return ActivationBatch(layers=captured)


def resolve_modules(
    model: Any, dotted_paths: Iterable[str]
) -> Dict[str, HookableModule]:
    """Resolve dotted attribute/index paths without depending on transformers."""
    resolved: Dict[str, HookableModule] = {}
    for path in dotted_paths:
        current = model
        for part in str(path).split("."):
            current = current[int(part)] if part.isdigit() else getattr(current, part)
        if not callable(getattr(current, "register_forward_hook", None)):
            raise TypeError("configured layer is not hookable: %s" % path)
        resolved[str(path)] = current
    return resolved
