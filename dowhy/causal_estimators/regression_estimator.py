from typing import Any, List, Optional

import numpy as np
import pandas as pd
import statsmodels.api as sm

from dowhy.causal_estimator import CausalEstimate, CausalEstimator


class RegressionEstimator(CausalEstimator):
    """Compute effect of treatment using some regression function.

    Fits a regression model for estimating the outcome using treatment(s) and
    confounders.

    Base class for all regression models, inherited by
    LinearRegressionEstimator and GeneralizedLinearModelEstimator.

    """

    def __init__(
        self,
        identified_estimand,
        test_significance=False,
        evaluate_effect_strength=False,
        confidence_intervals=False,
        num_null_simulations=CausalEstimator.DEFAULT_NUMBER_OF_SIMULATIONS_STAT_TEST,
        num_simulations=CausalEstimator.DEFAULT_NUMBER_OF_SIMULATIONS_CI,
        sample_size_fraction=CausalEstimator.DEFAULT_SAMPLE_SIZE_FRACTION,
        confidence_level=CausalEstimator.DEFAULT_CONFIDENCE_LEVEL,
        need_conditional_estimates="auto",
        num_quantiles_to_discretize_cont_cols=CausalEstimator.NUM_QUANTILES_TO_DISCRETIZE_CONT_COLS,
        **kwargs,
    ):
        """For a list of standard args and kwargs, see documentation for
        :class:`~dowhy.causal_estimator.CausalEstimator`.

        """

        super().__init__(
            identified_estimand=identified_estimand,
            test_significance=test_significance,
            evaluate_effect_strength=evaluate_effect_strength,
            confidence_intervals=confidence_intervals,
            num_null_simulations=num_null_simulations,
            num_simulations=num_simulations,
            sample_size_fraction=sample_size_fraction,
            confidence_level=confidence_level,
            need_conditional_estimates=need_conditional_estimates,
            num_quantiles_to_discretize_cont_cols=num_quantiles_to_discretize_cont_cols,
            **kwargs,
        )

        self.model = None

    def fit(
        self,
        data: pd.DataFrame,
        treatment_name: str,
        outcome_name: str,
        effect_modifier_names: Optional[List[str]] = None,
    ):
        self.set_data(data, treatment_name, outcome_name)
        self.set_effect_modifiers(effect_modifier_names)
        self.logger.debug("Back-door variables used:" + ",".join(self._target_estimand.get_backdoor_variables()))
        self._observed_common_causes_names = self._target_estimand.get_backdoor_variables()
        if len(self._observed_common_causes_names) > 0:
            self._observed_common_causes = self._data[self._observed_common_causes_names]
            self._observed_common_causes = pd.get_dummies(self._observed_common_causes, drop_first=True)
        else:
            self._observed_common_causes = None

        self.symbolic_estimator = self.construct_symbolic_estimator(self._target_estimand)
        self.logger.info(self.symbolic_estimator)

        return self

    def estimate_effect(
        self, treatment_value: Any = 1, control_value: Any = 0, target_units=None, need_conditional_estimates=None, **_
    ):
        self._target_units = target_units
        self._treatment_value = treatment_value
        self._control_value = control_value
        # TODO make treatment_value and control value also as local parameters
        if need_conditional_estimates is None:
            need_conditional_estimates = self.need_conditional_estimates
        # Checking if the model is already trained
        if not self.model:
            # The model is always built on the entire data
            _, self.model = self._build_model()
            coefficients = self.model.params[1:]  # first coefficient is the intercept
            self.logger.debug("Coefficients of the fitted model: " + ",".join(map(str, coefficients)))
            self.logger.debug(self.model.summary())
        # All treatments are set to the same constant value
        effect_estimate = self._do(treatment_value, self._data) - self._do(control_value, self._data)
        conditional_effect_estimates = None
        if need_conditional_estimates:
            conditional_effect_estimates = self._estimate_conditional_effects(
                self._estimate_effect_fn, effect_modifier_names=self._effect_modifier_names
            )
        intercept_parameter = self.model.params[0]
        estimate = CausalEstimate(
            estimate=effect_estimate,
            control_value=control_value,
            treatment_value=treatment_value,
            conditional_estimates=conditional_effect_estimates,
            target_estimand=self._target_estimand,
            realized_estimand_expr=self.symbolic_estimator,
            intercept=intercept_parameter,
        )

        estimate.add_estimator(self)
        return estimate

    def _build_features(self, treatment_values=None, data_df=None):
        # Using all data by default
        if data_df is None:
            data_df = self._data
            treatment_vals = pd.get_dummies(self._treatment, drop_first=True)
            observed_common_causes_vals = self._observed_common_causes
            effect_modifiers_vals = self._effect_modifiers
        else:
            treatment_vals = pd.get_dummies(data_df[self._treatment_name], drop_first=True)
            if len(self._observed_common_causes_names) > 0:
                observed_common_causes_vals = data_df[self._observed_common_causes_names]
                observed_common_causes_vals = pd.get_dummies(observed_common_causes_vals, drop_first=True)
            if self._effect_modifier_names:
                effect_modifiers_vals = data_df[self._effect_modifier_names]
                effect_modifiers_vals = pd.get_dummies(effect_modifiers_vals, drop_first=True)
        # Fixing treatment value to the specified value, if provided
        if treatment_values is not None:
            treatment_vals = treatment_values
        if type(treatment_vals) is not np.ndarray:
            treatment_vals = treatment_vals.to_numpy()
        # treatment_vals and data_df should have same number of rows
        if treatment_vals.shape[0] != data_df.shape[0]:
            raise ValueError("Provided treatment values and dataframe should have the same length.")
        # Bulding the feature matrix
        n_treatment_cols = 1 if len(treatment_vals.shape) == 1 else treatment_vals.shape[1]
        n_samples = treatment_vals.shape[0]
        treatment_2d = treatment_vals.reshape((n_samples, n_treatment_cols))
        if len(self._observed_common_causes_names) > 0:
            features = np.concatenate((treatment_2d, observed_common_causes_vals), axis=1)
        else:
            features = treatment_2d
        if self._effect_modifier_names:
            for i in range(treatment_2d.shape[1]):
                curr_treatment = treatment_2d[:, i]
                new_features = curr_treatment[:, np.newaxis] * effect_modifiers_vals.to_numpy()
                features = np.concatenate((features, new_features), axis=1)
        features = features.astype(
            float, copy=False
        )  # converting to float in case of binary treatment and no other variables
        features = sm.add_constant(features, has_constant="add")  # to add an intercept term
        return features

    def _do(self, treatment_val, data_df=None):
        if data_df is None:
            data_df = self._data
        if not self.model:
            # The model is always built on the entire data
            _, self.model = self._build_model()
        # Replacing treatment values by given x
        # First, create interventional tensor in original space
        interventional_treatment_values = np.full((data_df.shape[0], len(self._treatment_name)), treatment_val)
        # Then, use pandas to ensure that the dummies are assigned correctly for a categorical treatment
        interventional_treatment_2d = pd.concat(
            [
                self._treatment.copy(),
                pd.DataFrame(data=interventional_treatment_values, columns=self._treatment.columns),
            ],
            axis=0,
        ).astype(self._treatment.dtypes, copy=False)
        interventional_treatment_2d = pd.get_dummies(interventional_treatment_2d, drop_first=True)
        interventional_treatment_2d = interventional_treatment_2d[self._treatment.shape[0] :]

        new_features = self._build_features(treatment_values=interventional_treatment_2d, data_df=data_df)
        interventional_outcomes = self.predict_fn(self.model, new_features)
        return interventional_outcomes.mean()
