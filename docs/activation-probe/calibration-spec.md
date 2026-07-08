# Held-out calibration protocol

Calibration records are JSONL and must declare `split: calibration`; the loader
rejects any other split. Training and calibration exports must use distinct
files and dataset hashes. A fixed, recorded seed may create those files, but
the classifier training process must never open the calibration path.

The report contains expected calibration error (equal-width probability bins),
AUROC (pairwise ranking with half credit for ties), and accuracy at the probe's
declared threshold. Every production candidate should also record model ID,
layer path, checkpoint digest, calibration dataset digest, seed, and confidence
intervals. The checked-in fixture is synthetic and validates only plumbing.
It is not evidence that the classifier detects useful properties in a real
instrumented model, much less in a separate hosted model.
