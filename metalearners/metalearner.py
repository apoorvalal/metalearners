# Copyright (c) QuantCo 2024-2024
# SPDX-License-Identifier: LicenseRef-QuantCo

from abc import ABC, abstractmethod
from collections.abc import Callable, Collection
from copy import deepcopy
from typing import TypedDict

import numpy as np
import pandas as pd
from typing_extensions import Self

from metalearners._typing import OosMethod, PredictMethod, _ScikitModel
from metalearners._utils import (
    Matrix,
    Vector,
    index_matrix,
    validate_model_and_predict_method,
    validate_number_positive,
)
from metalearners.cross_fit_estimator import (
    OVERALL,
    CrossFitEstimator,
)

Params = dict[str, int | float | str]
Features = Collection[str] | Collection[int]
ModelFactory = type[_ScikitModel] | dict[str, type[_ScikitModel]]
PROPENSITY_MODEL = "propensity_model"
VARIANT_OUTCOME_MODEL = "variant_outcome_model"
TREATMENT_MODEL = "treatment_model"

NUISANCE = "nuisance"
TREATMENT = "treatment"


def _parse_fit_params(
    fit_params: None | dict,
    nuisance_model_names: set[str],
    treatment_model_names: set[str],
) -> dict[str, dict[str, dict[str, dict]]]:

    def _get_raw_fit_params():
        return fit_params

    def _get_empty_dict():
        return dict()

    def _get_result_skeleton(
        default_value_getter: Callable,
    ) -> dict[str, dict[str, dict[str, dict]]]:
        return {
            NUISANCE: {
                nuisance_model_kind: default_value_getter()
                for nuisance_model_kind in nuisance_model_names
            },
            TREATMENT: {
                treatment_model_kind: default_value_getter()
                for treatment_model_kind in treatment_model_names
            },
        }

    if fit_params is None or (
        (NUISANCE not in fit_params) and (TREATMENT not in fit_params)
    ):
        default_value_getter = _get_raw_fit_params if fit_params else _get_empty_dict
        return _get_result_skeleton(default_value_getter)

    result = _get_result_skeleton(_get_empty_dict)

    if NUISANCE in fit_params:
        for nuisance_model_kind in nuisance_model_names:
            if nuisance_model_kind in fit_params[NUISANCE]:
                result[NUISANCE][nuisance_model_kind] = fit_params[NUISANCE][
                    nuisance_model_kind
                ]
    if TREATMENT in fit_params:
        for treatment_model_kind in treatment_model_names:
            if treatment_model_kind in fit_params[TREATMENT]:
                result[TREATMENT][treatment_model_kind] = fit_params[TREATMENT][
                    treatment_model_kind
                ]
    return result


def _initialize_model_dict(argument, expected_names: Collection[str]) -> dict:
    if isinstance(argument, dict) and set(argument.keys()) == set(expected_names):
        return argument
    return {name: argument for name in expected_names}


def _combine_propensity_and_nuisance_specs(
    propensity_specs, nuisance_specs, nuisance_model_names: set[str]
) -> dict:
    if PROPENSITY_MODEL in nuisance_model_names:
        non_propensity_nuisance_model_names = nuisance_model_names - {PROPENSITY_MODEL}
        non_propensity_model_dict = _initialize_model_dict(
            nuisance_specs, non_propensity_nuisance_model_names
        )
        return non_propensity_model_dict | {PROPENSITY_MODEL: propensity_specs}

    return _initialize_model_dict(nuisance_specs, nuisance_model_names)


def _filter_x_columns(X: Matrix, feature_set: Features) -> Matrix:
    if feature_set is None:
        X_filtered = X
    elif len(feature_set) == 0:
        X_filtered = np.ones((len(X), 1))
    else:
        if isinstance(X, pd.DataFrame):
            X_filtered = X[list(feature_set)]
        else:
            X_filtered = X[:, np.array(feature_set)]
    return X_filtered


class _ModelSpecifications(TypedDict):
    # The quotes on MetaLearner are necessary for type hinting as it's not yet defined
    # here. Check https://stackoverflow.com/questions/55320236/does-python-evaluate-type-hinting-of-a-forward-reference
    # At some point evaluation at runtime will be the default and then this won't be needed.
    cardinality: Callable[["MetaLearner"], int]
    predict_method: Callable[["MetaLearner"], PredictMethod]


class MetaLearner(ABC):
    r"""MetaLearner abstract class. All metalearner implementations should inherit from
    it.

    All of

    * ``nuisance_model_factory``
    * ``treatment_model_factory``
    * ``nuisance_model_params``
    * ``treatment_model_params``
    * ``feature_set``

    can either

    * contain a single value, such that the value will be used for all relevant models
      of the respective MetaLearner or
    * a dictionary mapping from the relevant models (``model_kind``, a ``str``) to the
      respective value

    The possible values for defining ``feature_set`` (either one single value for all
    the models or the values inside the dictionary specifying for each model) can be:

    * ``None``: All columns will be used.
    * A list of strings or integers indicating which columns to use.
    * ``[]`` meaning that no present column should be used for that model and the
      input of the model should be a vector of 1s.

    To reuse already fitted models  ``fitted_nuisance_models`` and ``fitted_propensity_model``
    should be used. The models should be fitted on the same data the MetaLearner is going
    to call fit with. For an illustration, see :ref:`our example on reusing models <example-reuse>`.
    """

    @classmethod
    @abstractmethod
    def nuisance_model_specifications(cls) -> dict[str, _ModelSpecifications]:
        """Return the specifications of all first-stage models."""
        ...

    @classmethod
    @abstractmethod
    def treatment_model_specifications(cls) -> dict[str, _ModelSpecifications]:
        """Return the specifications of all second-stage models."""
        ...

    def _validate_params(self, **kwargs): ...

    @classmethod
    @abstractmethod
    def _supports_multi_treatment(cls) -> bool: ...

    @classmethod
    @abstractmethod
    def _supports_multi_class(cls) -> bool: ...

    def _outcome_predict_method(self):
        return "predict_proba" if self.is_classification else "predict"

    def _get_n_variants(self):
        return self.n_variants

    def _get_n_variants_minus_one(self):
        return self.n_variants - 1

    @classmethod
    def _validate_n_variants(cls, n_variants: int) -> None:
        if not isinstance(n_variants, int) or n_variants < 2:
            raise ValueError(
                "n_variants needs to be an integer strictly greater than 1."
            )
        if n_variants > 2 and not cls._supports_multi_treatment():
            raise NotImplementedError(
                f"Current implementation of {cls.__name__} only supports binary "
                f"treatment variants. Yet, n_variants was set to {n_variants}."
            )

    def _validate_treatment(self, w: Vector) -> None:
        if len(np.unique(w)) != self.n_variants:
            raise ValueError(
                "Number of variants present in the treatment are different than the "
                "number specified at instantiation."
            )
        # TODO: add support for different encoding of treatment variants (str, not consecutive ints...)
        if set(np.unique(w)) != set(range(self.n_variants)):
            raise ValueError(
                "Treatment variant should be encoded with values "
                f"{{0...{self.n_variants -1}}} and all variants should be present. "
                f"Yet we found the values {set(np.unique(w))}."
            )

    def _validate_outcome(self, y: Vector) -> None:
        if (
            self.is_classification
            and not self._supports_multi_class()
            and len(np.unique(y)) > 2
        ):
            raise ValueError(
                f"{self.__class__.__name__} does not support multiclass classification."
                f" Yet we found {len(np.unique(y))} classes."
            )

    def _validate_models(self) -> None:
        """Validate that the base models are appropriate.

        In particular, it is validated that a base model to be used with ``"predict"`` is
        recognized by ``scikit-learn`` as a regressor via ``sklearn.base.is_regressor`` and
        a model to be used with ``"predict_proba"`` is recognized by ``scikit-learn` as
        a classifier via ``sklearn.base.is_classifier``.
        """
        for model_kind in self.nuisance_model_specifications():
            if model_kind in self._prefitted_nuisance_models:
                factory = self._nuisance_models[model_kind][0].estimator_factory
            else:
                factory = self.nuisance_model_factory[model_kind]
            predict_method = self.nuisance_model_specifications()[model_kind][
                "predict_method"
            ](self)
            validate_model_and_predict_method(
                factory, predict_method, name=f"nuisance model {model_kind}"
            )

        for model_kind in self.treatment_model_specifications():
            factory = self.treatment_model_factory[model_kind]
            predict_method = self.treatment_model_specifications()[model_kind][
                "predict_method"
            ](self)
            validate_model_and_predict_method(
                factory, predict_method, name=f"treatment model {model_kind}"
            )

    def _qualified_fit_params(
        self,
        fit_params: None | dict,
    ) -> dict[str, dict[str, dict[str, dict]]]:
        return _parse_fit_params(
            fit_params=fit_params,
            nuisance_model_names=set(self.nuisance_model_specifications().keys()),
            treatment_model_names=set(self.treatment_model_specifications().keys()),
        )

    def __init__(
        self,
        is_classification: bool,
        # TODO: Consider whether we can make this not a state of the MetaLearner
        # but rather just a parameter of a predict call.
        n_variants: int,
        nuisance_model_factory: ModelFactory | None = None,
        treatment_model_factory: ModelFactory | None = None,
        propensity_model_factory: type[_ScikitModel] | None = None,
        nuisance_model_params: Params | dict[str, Params] | None = None,
        treatment_model_params: Params | dict[str, Params] | None = None,
        propensity_model_params: Params | None = None,
        fitted_nuisance_models: dict[str, list[CrossFitEstimator]] | None = None,
        fitted_propensity_model: CrossFitEstimator | None = None,
        feature_set: Features | dict[str, Features] | None = None,
        n_folds: int | dict[str, int] = 10,
        random_state: int | None = None,
    ):
        self._validate_params(
            nuisance_model_factory=nuisance_model_factory,
            treatment_model_factory=treatment_model_factory,
            propensity_model_factory=propensity_model_factory,
            is_classification=is_classification,
            n_variants=n_variants,
            nuisance_model_params=nuisance_model_params,
            treatment_model_params=treatment_model_params,
            propensity_model_params=propensity_model_params,
            feature_set=feature_set,
            n_folds=n_folds,
            random_state=random_state,
        )

        nuisance_model_specifications = self.nuisance_model_specifications()
        treatment_model_specifications = self.treatment_model_specifications()

        if PROPENSITY_MODEL in treatment_model_specifications:
            raise ValueError(
                f"{PROPENSITY_MODEL} can't be used as a treatment model name"
            )
        if (
            isinstance(nuisance_model_factory, dict)
            and PROPENSITY_MODEL in nuisance_model_factory.keys()
        ):
            raise ValueError(
                "Propensity model factory should be defined using propensity_model_factory "
                "and not nuisance_model_factory."
            )
        if (
            isinstance(nuisance_model_params, dict)
            and PROPENSITY_MODEL in nuisance_model_params.keys()
        ):
            raise ValueError(
                "Propensity model params should be defined using propensity_model_params "
                "and not nuisance_model_params."
            )
        if (
            PROPENSITY_MODEL in nuisance_model_specifications
            and propensity_model_factory is None
            and fitted_propensity_model is None
        ):
            raise ValueError(
                "propensity_model_factory or fitted_propensity_model needs to be defined "
                f"as the {self.__class__.__name__} has a propensity model."
            )

        self._validate_n_variants(n_variants)
        self.is_classification = is_classification
        self.n_variants = n_variants

        self.nuisance_model_factory = _combine_propensity_and_nuisance_specs(
            propensity_model_factory,
            nuisance_model_factory,
            set(nuisance_model_specifications.keys()),
        )
        if nuisance_model_params is None:
            nuisance_model_params = {}  # type: ignore
        if propensity_model_params is None:
            propensity_model_params = {}
        self.nuisance_model_params = _combine_propensity_and_nuisance_specs(
            propensity_model_params,
            nuisance_model_params,
            set(nuisance_model_specifications.keys()),
        )

        self.treatment_model_factory = _initialize_model_dict(
            treatment_model_factory, set(treatment_model_specifications.keys())
        )
        if treatment_model_params is None:
            self.treatment_model_params = _initialize_model_dict(
                {}, set(treatment_model_specifications.keys())
            )
        else:
            self.treatment_model_params = _initialize_model_dict(
                treatment_model_params, set(treatment_model_specifications.keys())
            )

        self.n_folds = _initialize_model_dict(
            n_folds,
            set(nuisance_model_specifications.keys())
            | set(treatment_model_specifications.keys()),
        )
        for model_kind, n_folds_model_kind in self.n_folds.items():
            validate_number_positive(n_folds_model_kind, f"{model_kind} n_folds", True)
        self.random_state = random_state

        self.feature_set = _initialize_model_dict(
            feature_set,
            set(nuisance_model_specifications.keys())
            | set(treatment_model_specifications.keys()),
        )

        self._nuisance_models: dict[str, list[CrossFitEstimator]] = {}
        not_fitted_nuisance_models = set(nuisance_model_specifications.keys())
        self._prefitted_nuisance_models: set[str] = set()

        if fitted_nuisance_models is not None:
            if not set(fitted_nuisance_models.keys()) <= set(
                nuisance_model_specifications.keys()
            ):
                raise ValueError(
                    "The keys present in fitted_nuisance_models should be a subset of"
                    f"{set(nuisance_model_specifications.keys())}"
                )
            self._nuisance_models |= deepcopy(fitted_nuisance_models)
            not_fitted_nuisance_models -= set(fitted_nuisance_models.keys())
            self._prefitted_nuisance_models |= set(fitted_nuisance_models.keys())

        if (
            PROPENSITY_MODEL in nuisance_model_specifications.keys()
            and fitted_propensity_model is not None
        ):
            self._nuisance_models |= {PROPENSITY_MODEL: [fitted_propensity_model]}
            not_fitted_nuisance_models -= {PROPENSITY_MODEL}
            self._prefitted_nuisance_models |= {PROPENSITY_MODEL}

        for name in not_fitted_nuisance_models:
            if self.nuisance_model_factory[name] is None:
                if name == PROPENSITY_MODEL:
                    raise ValueError(
                        f"A model for the nuisance model {name} needs to be defined. Either "
                        "in propensity_model_factory or in fitted_propensity_model."
                    )
                else:
                    raise ValueError(
                        f"A model for the nuisance model {name} needs to be defined. Either "
                        "in nuisance_model_factory or in fitted_nuisance_models."
                    )

        self._nuisance_models |= {
            name: [
                CrossFitEstimator(
                    n_folds=self.n_folds[name],
                    estimator_factory=self.nuisance_model_factory[name],
                    estimator_params=self.nuisance_model_params[name],
                    random_state=self.random_state,
                )
                for _ in range(nuisance_model_specifications[name]["cardinality"](self))
            ]
            for name in not_fitted_nuisance_models
        }
        self._treatment_models: dict[str, list[CrossFitEstimator]] = {
            name: [
                CrossFitEstimator(
                    n_folds=self.n_folds[name],
                    estimator_factory=self.treatment_model_factory[name],
                    estimator_params=self.treatment_model_params[name],
                    random_state=self.random_state,
                )
                for _ in range(
                    treatment_model_specifications[name]["cardinality"](self)
                )
            ]
            for name in set(treatment_model_specifications.keys())
        }

        self._validate_models()

    def _nuisance_tensors(self, n_obs: int) -> dict[str, list[np.ndarray]]:
        def dimension(n_obs, model_kind, model_ord, predict_method):
            if (
                n_outputs := self._nuisance_models[model_kind][model_ord]._n_outputs(
                    predict_method
                )
            ) > 1:
                return (n_obs, n_outputs)
            return (n_obs,)

        nuisance_tensors: dict[str, list[np.ndarray]] = {}
        for (
            model_kind,
            model_specifications,
        ) in self.nuisance_model_specifications().items():
            nuisance_tensors[model_kind] = []
            for model_ord in range(model_specifications["cardinality"](self)):
                nuisance_tensors[model_kind].append(
                    np.zeros(
                        dimension(
                            n_obs,
                            model_kind,
                            model_ord,
                            model_specifications["predict_method"](self),
                        )
                    )
                )
        return nuisance_tensors

    def fit_nuisance(
        self,
        X: Matrix,
        y: Vector,
        model_kind: str,
        model_ord: int,
        fit_params: dict | None = None,
        n_jobs_cross_fitting: int | None = None,
    ) -> Self:
        """Fit a given nuisance model of a MetaLearner.

        ``y`` represents the objective of the given nuisance model, not necessarily the outcome of the experiment.
        If pre-fitted models were passed at instantiation, these are never refitted.
        """
        if model_kind in self._prefitted_nuisance_models:
            return self
        X_filtered = _filter_x_columns(X, self.feature_set[model_kind])
        self._nuisance_models[model_kind][model_ord].fit(
            X_filtered,
            y,
            fit_params=fit_params,
            n_jobs_cross_fitting=n_jobs_cross_fitting,
        )
        return self

    def fit_treatment(
        self,
        X: Matrix,
        y: Vector,
        model_kind: str,
        model_ord: int,
        fit_params: dict | None = None,
        n_jobs_cross_fitting: int | None = None,
    ) -> Self:
        """Fit the treatment model of a MetaLearner.

        ``y`` represents the objective of the given treatment model, not necessarily the outcome of the experiment.
        """
        X_filtered = _filter_x_columns(X, self.feature_set[model_kind])
        self._treatment_models[model_kind][model_ord].fit(
            X_filtered,
            y,
            fit_params=fit_params,
            n_jobs_cross_fitting=n_jobs_cross_fitting,
        )
        return self

    @abstractmethod
    def fit(
        self,
        X: Matrix,
        y: Vector,
        w: Vector,
        n_jobs_cross_fitting: int | None = None,
        fit_params: dict | None = None,
    ) -> Self:
        """Fit all models of the MetaLearner.

        If pre-fitted models were passed at instantiation, these are never refitted.

        ``n_jobs_cross_fitting`` will be used at the cross-fitting level. For more information about
        parallelism check :ref:`parallelism`

        ``fit_params`` is an optional ``dict`` to be forwarded to base estimator ``fit`` calls. It supports
        two usages patterns:

        * .. code-block:: python

            fit_params={"parameter_of_interest": value_of_interest}

        * .. code-block:: python

            fit_params={
                "nuisance": {
                    "nuisance_model_kind1": {"parameter_of_interest1": value_of_interest1},
                    "nuisance_model_kind3": {"parameter_of_interest3": value_of_interest3},
                },
                "treatment": {"treatment_model_kind1": {"parameter_of_interest4": value_of_interest4}}
            }

        In the former approach, the parameter and value of interest are passed to all base models. In the
        the latter approach, the explicitly qualified parameter-value pairs are passed to respective base
        models and no fitting parameters are passed to base models not explicitly listed. Note that in this
        pattern, propensity models are considered a nuisance model.
        """
        ...

    def predict_nuisance(
        self,
        X: Matrix,
        model_kind: str,
        model_ord: int,
        is_oos: bool,
        oos_method: OosMethod = OVERALL,
    ) -> np.ndarray:
        """Estimate based on a given nuisance model.

        Importantly, this method needs to implement the subselection of ``X`` based on
        the ``feature_set`` field of ``MetaLearner``.
        """
        X_filtered = _filter_x_columns(X, self.feature_set[model_kind])
        predict_method_name = self.nuisance_model_specifications()[model_kind][
            "predict_method"
        ](self)
        predict_method = getattr(
            self._nuisance_models[model_kind][model_ord], predict_method_name
        )
        return predict_method(X_filtered, is_oos, oos_method)

    def predict_treatment(
        self,
        X: Matrix,
        model_kind: str,
        model_ord: int,
        is_oos: bool,
        oos_method: OosMethod = OVERALL,
    ) -> np.ndarray:
        """Estimate based on a given treatment model.

        Importantly, this method needs to implement the subselection of ``X`` based on
        the ``feature_set`` field of ``MetaLearner``.
        """
        X_filtered = _filter_x_columns(X, self.feature_set[model_kind])
        return self._treatment_models[model_kind][model_ord].predict(
            X_filtered, is_oos, oos_method
        )

    @abstractmethod
    def predict(
        self,
        X: Matrix,
        is_oos: bool,
        oos_method: OosMethod = OVERALL,
    ) -> np.ndarray:
        """Estimate the CATE.

        If ``is_oos``, an acronym for 'is out of sample', is ``False``,
        the estimates will stem from cross-fitting. Otherwise,
        various approaches exist, specified via ``oos_method``.

        The returned ndarray is of shape:

        * :math:`(n_{obs}, n_{variants} - 1, 1)` if the outcome is a scalar, i.e. in case
          of a regression problem.

        * :math:`(n_{obs}, n_{variants} - 1, n_{classes})` if the outcome is a class,
          i.e. in case of a classification problem.

        In the case of multiple treatment variants, the second dimension represents the
        CATE of the corresponding variant vs the control (variant 0).
        """
        ...

    @abstractmethod
    def evaluate(
        self,
        X: Matrix,
        y: Vector,
        w: Vector,
        is_oos: bool,
        oos_method: OosMethod = OVERALL,
    ) -> dict[str, float | int]:
        """Evaluate all models contained in a MetaLearner."""
        ...


class _ConditionalAverageOutcomeMetaLearner(MetaLearner, ABC):

    def __init__(
        self,
        is_classification: bool,
        # TODO: Consider whether we can make this not a state of the MetaLearner
        # but rather just a parameter of a predict call.
        n_variants: int,
        nuisance_model_factory: ModelFactory | None = None,
        treatment_model_factory: ModelFactory | None = None,
        propensity_model_factory: type[_ScikitModel] | None = None,
        nuisance_model_params: Params | dict[str, Params] | None = None,
        treatment_model_params: Params | dict[str, Params] | None = None,
        propensity_model_params: Params | None = None,
        fitted_nuisance_models: dict[str, list[CrossFitEstimator]] | None = None,
        fitted_propensity_model: CrossFitEstimator | None = None,
        feature_set: Features | dict[str, Features] | None = None,
        n_folds: int | dict[str, int] = 10,
        random_state: int | None = None,
    ):
        super().__init__(
            nuisance_model_factory=nuisance_model_factory,
            is_classification=is_classification,
            n_variants=n_variants,
            treatment_model_factory=treatment_model_factory,
            propensity_model_factory=propensity_model_factory,
            nuisance_model_params=nuisance_model_params,
            treatment_model_params=treatment_model_params,
            propensity_model_params=propensity_model_params,
            fitted_nuisance_models=fitted_nuisance_models,
            fitted_propensity_model=fitted_propensity_model,
            feature_set=feature_set,
            n_folds=n_folds,
            random_state=random_state,
        )
        self._treatment_variants_indices: list[np.ndarray] | None = None

    def predict_conditional_average_outcomes(
        self, X: Matrix, is_oos: bool, oos_method: OosMethod = OVERALL
    ) -> np.ndarray:
        r"""Predict the vectors of conditional average outcomes.

        These are defined as :math:`\mathbb{E}[Y_i(w) | X]` for each treatment variant
        :math:`w`.

        If ``is_oos``, an acronym for 'is out of sample' is ``False``,
        the estimates will stem from cross-fitting. Otherwise,
        various approaches exist, specified via ``oos_method``.

        The returned ndarray is of shape:

        * :math:`(n_{obs}, n_{variants}, 1)` if the outcome is a scalar, i.e. in case
          of a regression problem.

        * :math:`(n_{obs}, n_{variants}, n_{classes})` if the outcome is a class,
          i.e. in case of a classification problem.
        """
        if self._treatment_variants_indices is None:
            raise ValueError(
                "The metalearner needs to be fitted before predicting."
                "In particular, the MetaLearner's attribute _treatment_variant_indices, "
                "typically set during fitting, is None."
            )
        # TODO: Consider multiprocessing
        n_obs = len(X)
        nuisance_tensors = self._nuisance_tensors(n_obs)
        conditional_average_outcomes_list = nuisance_tensors[VARIANT_OUTCOME_MODEL]

        for tv in range(self.n_variants):
            if is_oos:
                conditional_average_outcomes_list[tv] = self.predict_nuisance(
                    X=X,
                    model_kind=VARIANT_OUTCOME_MODEL,
                    model_ord=tv,
                    is_oos=True,
                    oos_method=oos_method,
                )
            else:
                conditional_average_outcomes_list[tv][
                    self._treatment_variants_indices[tv]
                ] = self.predict_nuisance(
                    X=index_matrix(X, self._treatment_variants_indices[tv]),
                    model_kind=VARIANT_OUTCOME_MODEL,
                    model_ord=tv,
                    is_oos=False,
                )
                conditional_average_outcomes_list[tv][
                    ~self._treatment_variants_indices[tv]
                ] = self.predict_nuisance(
                    X=index_matrix(X, ~self._treatment_variants_indices[tv]),
                    model_kind=VARIANT_OUTCOME_MODEL,
                    model_ord=tv,
                    is_oos=True,
                    oos_method=oos_method,
                )
        return np.stack(conditional_average_outcomes_list, axis=1).reshape(
            n_obs, self.n_variants, -1
        )
