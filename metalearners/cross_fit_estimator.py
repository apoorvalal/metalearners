# Copyright (c) QuantCo 2024-2024
# SPDX-License-Identifier: LicenseRef-QuantCo

from dataclasses import dataclass, field
from typing import Literal, Optional, Union

import numpy as np
from sklearn.base import is_classifier
from sklearn.model_selection import KFold, cross_validate
from typing_extensions import Self

from metalearners._utils import Matrix, Vector, _ScikitModel, index_matrix

_OOS_WHITELIST = ["overall", "median", "mean"]
# As of 24/01/19, no convenient way of dynamically creating a literal collection that
# mypy can deal with seems to exist. Therefore we duplicate the values.
# See https://stackoverflow.com/questions/64522040/typing-dynamically-create-literal-alias-from-list-of-valid-values
_OosMethod = Literal["overall", "median", "mean"]


def _validate_oos_method(
    oos_method: Optional[_OosMethod], enable_overall: bool
) -> None:
    if oos_method not in _OOS_WHITELIST:
        raise ValueError(
            f"oos_method {oos_method} not supported. Supported values are "
            f"{_OOS_WHITELIST}."
        )
    if oos_method == "overall" and not enable_overall:
        raise ValueError(
            "In order to use 'overall' prediction method, the estimator's "
            "enable_overall property has to be set to True."
        )


def _validate_n_folds(n_folds: int) -> None:
    if n_folds <= 0:
        raise ValueError(
            f"n_folds needs to be a strictly positive integer but is {n_folds}."
        )


_PredictMethod = Literal["predict", "predict_proba"]


@dataclass
class CrossFitEstimator:
    """Helper class for cross-fitting estimators on data.

    Conceptually, it allows for fitting ``n_folds`` or ``n_folds`` + 1 models on
    ``n_folds`` folds of the data.

    ``estimator_factory`` is a class implementing an estimator with a scikit-learn
    interface. Instantiation parameters can be passed to ``estimator_params``.
    An example argument for ``estimator_factory`` would be ``lightgbm.LGBMRegressor``.

    Importantly, the ``CrossFitEstimator`` can handle in-sample and out-of-sample
    ('oos') data for prediction. When doing in-sample prediction the single model will
    be used in which the respective data point has not been part of the training set.
    When doing oos prediction, different options exist. These options either rely on
    combining the ``n_folds`` models or using a model trained on all of the data
    (``enable_overall``).
    """

    n_folds: int
    estimator_factory: type[_ScikitModel]
    estimator_params: dict = field(default_factory=dict)
    enable_overall: bool = True
    _estimators: list[_ScikitModel] = field(init=False)
    _overall_estimator: Optional[_ScikitModel] = field(init=False)
    _test_indices: Optional[tuple[np.ndarray]] = field(init=False)

    def __post__init__(self):
        _validate_n_folds(self.n_folds)
        self._estimators: list[_ScikitModel] = []
        self._overall_estimator: Optional[_ScikitModel] = None
        self._test_indices: Optional[tuple[np.ndarray]] = None

    def _train_overall_estimator(
        self, X: Matrix, y: Union[Matrix, Vector]
    ) -> _ScikitModel:
        model = self.estimator_factory(**self.estimator_params)
        return model.fit(X, y)

    def fit(
        self,
        X: Matrix,
        y: Union[Vector, Matrix],
        **kwargs,
    ) -> Self:
        """Fit the underlying estimators.

        One estimator is trained per ``n_folds``.

        If ``enable_overall`` is set, an additional estimator is trained on all data.
        """
        cv_result = cross_validate(
            self.estimator_factory(**self.estimator_params),
            X,
            y,
            # TODO: Consider using StratifiedKFold for classifiers.
            cv=KFold(
                n_splits=self.n_folds,
                shuffle=True,
            ),
            return_estimator=True,
            return_indices=True,
        )
        self._estimators = cv_result["estimator"]
        self._test_indices = cv_result["indices"]["test"]
        if self.enable_overall:
            self.overall_estimator = self._train_overall_estimator(X, y)
        return self

    def _initialize_prediction_tensor(
        self, n_observations: int, n_outputs: int, n_folds: int
    ) -> np.ndarray:
        return np.zeros((n_observations, n_outputs, n_folds))

    @property
    def _n_classes(self):
        if not self._estimators:
            raise RuntimeError(
                "Number of classes can only be determined if estimators exist."
            )
        if (
            not all(is_classifier(est) for est in self._estimators)
            or not all(hasattr(est, "fitted_") for est in self._estimators)
            or not all(hasattr(est, "n_classes_") for est in self._estimators)
        ):
            raise RuntimeError(
                "Number of classes can only be determined if all estimators are "
                "classifiers and have a fitted_ and n_classes_ attributes."
            )
        if not all(est.fitted_ for est in self._estimators):  # type: ignore
            raise RuntimeError(
                "Number of classes can only be determined once fit has been called ."
            )
        n_classes = [est.n_classes_ for est in self._estimators]  # type: ignore
        if min(n_classes) != max(n_classes):
            raise RuntimeError(
                f"Fold estimators don't all have the same number of classes: "
                f"{n_classes}"
            )
        return n_classes[0]

    def _n_outputs(self, method: _PredictMethod) -> int:
        if method == "predict_proba":
            return self._n_classes
        return 1

    def _predict_all(self, X: Matrix, method: _PredictMethod) -> np.ndarray:
        n_outputs = self._n_outputs(method)
        predictions = self._initialize_prediction_tensor(
            n_observations=len(X),
            n_outputs=n_outputs,
            n_folds=self.n_folds,
        )
        for i, estimator in enumerate(self._estimators):
            predictions[:, :, i] = np.reshape(
                getattr(estimator, method)(X), (-1, n_outputs)
            )
        return predictions

    def _predict_mean(self, X: Matrix, method: _PredictMethod) -> np.ndarray:
        all_predictions = self._predict_all(X=X, method=method)
        return np.mean(all_predictions, axis=2)

    def _predict_median(self, X: Matrix, method: _PredictMethod) -> np.ndarray:
        all_predictions = self._predict_all(X=X, method=method)
        return np.median(all_predictions, axis=2)

    def _predict_in_sample(
        self,
        X: Matrix,
        method: _PredictMethod,
    ) -> np.ndarray:
        if not self._test_indices:
            raise ValueError()
        n_outputs = self._n_outputs(method)
        predictions = self._initialize_prediction_tensor(
            n_observations=len(X),
            n_outputs=n_outputs,
            n_folds=1,
        )
        for estimator, indices in zip(self._estimators, self._test_indices):
            fold_predictions = np.reshape(
                getattr(estimator, method)(index_matrix(X, indices)), (-1, n_outputs, 1)
            )
            predictions[indices] = fold_predictions
        return predictions[:, :, 0]

    def _predict(
        self,
        X: Matrix,
        is_oos: bool,
        method: _PredictMethod,
        oos_method: Optional[_OosMethod] = None,
        w: Optional[Union[Vector, Matrix]] = None,
    ) -> np.ndarray:
        if is_oos:
            _validate_oos_method(oos_method, self.enable_overall)
            if oos_method == "overall":
                return getattr(self.overall_estimator, method)(X)
            if oos_method == "mean":
                if method != "predict_proba" and any(
                    is_classifier(est) for est in self._estimators
                ):
                    raise ValueError(
                        "Cannot create a mean of classes. Please use a different oos_method."
                    )
                return self._predict_mean(X, method=method)
            return self._predict_median(X, method=method)
        return self._predict_in_sample(X, method=method)

    def predict(
        self,
        X: Matrix,
        is_oos: bool,
        oos_method: Optional[_OosMethod] = None,
        **kwargs,
    ) -> np.ndarray:
        """Predict from ``X``.

        If ``is_oos``, the ``oos_method`` will be used to generate predictions
        on 'out of sample' data. 'Out of sample' refers to this data not having been
        used in the ``fit`` method. The ``oos_method`` ``'overall'`` can only be used
        if the ``CrossFitEstimator`` has been initialized with
        ``enable_overall=True``.
        """
        return self._predict(
            X=X,
            is_oos=is_oos,
            method="predict",
            oos_method=oos_method,
        )

    def predict_proba(
        self,
        X: Matrix,
        is_oos: bool,
        oos_method: Optional[_OosMethod] = None,
    ) -> np.ndarray:
        """Predict probability from ``X``.

        If ``is_oos``, the ``oos_method`` will be used to generate predictions
        on 'out of sample' data. 'Out of sample' refers to this data not having been
        used in the ``fit`` method. The ``oos_method`` ``'overall'`` can only be used
        if the ``CrossFitEstimator`` has been initialized with
        ``enable_overall=True``.
        """
        return self._predict(
            X=X,
            is_oos=is_oos,
            method="predict_proba",
            oos_method=oos_method,
        )

    def score(self, X, y, sample_weight=None, **kwargs):
        raise NotImplementedError()

    def set_params(self, **params):
        raise NotImplementedError()
