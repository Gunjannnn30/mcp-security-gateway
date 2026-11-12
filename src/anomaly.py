"""Machine-learning based behavioural anomaly detector.

Learns normal tool behaviour from collected features and assigns an anomaly
score to new activity (1.0 = normal, closer to 0.0 = more anomalous).
Treated as an additional signal, never the sole reason to block.

Tries scikit-learn's IsolationForest first. If sklearn cannot be imported
(e.g. blocked by a Windows application-control policy), falls back to a
simple statistical detector: it flags samples that are far from the mean of
the normal samples (z-score based). Same public API either way.
"""

from __future__ import annotations

import os
from statistics import mean, stdev
from typing import Any

import numpy as np

try:  # scikit-learn may be blocked on some machines (Windows app control).
    from sklearn.ensemble import IsolationForest

    _SKLEARN = True
except Exception:  # noqa: BLE001 - fall back to the statistical detector.
    _SKLEARN = False

MODEL_FILE = os.path.join(
    os.path.dirname(__file__), "..", "models", "anomaly_model.joblib"
)

FEATURE_KEYS = [
    "call_frequency",
    "request_size",
    "response_size",
    "network_connections",
    "file_accesses",
    "process_spawns",
]


class AnomalyDetector:
    def __init__(self, model_file: str = MODEL_FILE) -> None:
        self.model_file = model_file
        self.model: Any = None
        self.trained = False
        # Statistical fallback state.
        self._means: list[float] = []
        self._stds: list[float] = []
        self._trained_count = 0
        self._use_sklearn = _SKLEARN
        self._load()

    def _load(self) -> None:
        # We deliberately do not unpickle sklearn models across the fallback
        # boundary; retraining on the normal samples is cheap and safer.
        if os.path.exists(self.model_file) and self._use_sklearn:
            try:
                import joblib

                self.model = joblib.load(self.model_file)
                self.trained = True
            except Exception:  # noqa: BLE001 - keep prototype running
                self.model = None
                self.trained = False

    def save(self) -> None:
        if not self._use_sklearn:
            # Nothing meaningful to persist for the statistical fallback.
            return
        os.makedirs(os.path.dirname(self.model_file), exist_ok=True)
        try:
            import joblib

            if self.model is not None:
                joblib.dump(self.model, self.model_file)
        except Exception:  # noqa: BLE001 - keep prototype running
            pass

    def features_from_dict(self, raw: dict) -> list[float]:
        return [float(raw.get(k, 0.0)) for k in FEATURE_KEYS]

    def train(self, normal_samples: list[dict]) -> None:
        """Train on samples of normal tool behaviour."""
        if not normal_samples:
            return
        X = np.array([self.features_from_dict(s) for s in normal_samples], dtype=float)

        if self._use_sklearn:
            from sklearn.ensemble import IsolationForest

            self.model = IsolationForest(
                n_estimators=100, contamination=0.1, random_state=42
            )
            self.model.fit(X)
            self.trained = True
            self.save()
        else:
            # Statistical fallback: per-feature mean and population stddev.
            self._means = [float(X[:, i].mean()) for i in range(X.shape[1])]
            self._stds = [
                float(X[:, i].std()) if X[:, i].std() > 0 else 1.0
                for i in range(X.shape[1])
            ]
            self._trained_count = len(normal_samples)
            self.trained = True

    def _sklearn_score(self, sample: dict) -> float:
        X = np.array([self.features_from_dict(sample)], dtype=float)
        raw = float(self.model.decision_function(X)[0])
        return float(1.0 / (1.0 + np.exp(-raw)))  # sigmoid -> 0..1

    def _stat_score(self, sample: dict) -> float:
        """Z-score based anomaly score: 1.0 = normal, lower = anomalous."""
        vals = self.features_from_dict(sample)
        total_z = 0.0
        for i, v in enumerate(vals):
            z = abs(v - self._means[i]) / self._stds[i]
            total_z += z
        # Average z-score across features; map to 0..1 via exp decay.
        avg_z = total_z / len(vals) if vals else 0.0
        return float(np.exp(-avg_z / 2.0))  # z=0 -> 1.0, z=2 -> ~0.37, z=4 -> ~0.14

    def score(self, sample: dict) -> float:
        """Return anomaly score where 1.0 = normal, lower = more anomalous."""
        if not self.trained:
            # Not trained yet -> neutral (no signal).
            return 0.5
        if self._use_sklearn and self.model is not None:
            return self._sklearn_score(sample)
        return self._stat_score(sample)

    def is_anomalous(self, sample: dict, threshold: float = 0.3) -> tuple[bool, float]:
        score = self.score(sample)
        return score < threshold, score
