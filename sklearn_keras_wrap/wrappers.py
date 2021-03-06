"""Wrapper for using the Scikit-Learn API with Keras models.
"""
import copy
import inspect
import warnings
from collections import defaultdict, namedtuple

import numpy as np
from sklearn.exceptions import NotFittedError
from sklearn.metrics import accuracy_score as sklearn_accuracy_score
from sklearn.metrics import r2_score as sklearn_r2_score
from sklearn.utils.multiclass import type_of_target
from sklearn.utils.validation import (
    check_X_y,
    check_array,
    _check_sample_weight,
)
from tensorflow.python.keras import backend as K
from tensorflow.python.keras.layers import deserialize, serialize
from tensorflow.python.keras.losses import is_categorical_crossentropy
from tensorflow.python.keras.models import Model, Sequential, clone_model
from tensorflow.python.keras.saving import saving_utils
from tensorflow.python.keras.utils.generic_utils import (
    has_arg,
    register_keras_serializable,
)
from tensorflow.python.keras.utils.np_utils import to_categorical


# namedtuple used for pickling Model instances
SavedKerasModel = namedtuple(
    "SavedKerasModel", "cls model training_config weights"
)

# known keras function names that will be added to _legal_params_fns if they
# exist in the generated model
KNOWN_KERAS_FN_NAMES = (
    "fit",
    "evaluate",
    "predict",
)

# used by inspect to resolve parameters of parent classes
ARGS_KWARGS_IDENTIFIERS = (
    inspect.Parameter.VAR_KEYWORD,
    inspect.Parameter.VAR_POSITIONAL,
)

_DEFAULT_TAGS = {
    "non_deterministic": True,  # can't easily set random_state
    "requires_positive_X": False,
    "requires_positive_y": False,
    "X_types": ["2darray"],
    "poor_score": True,
    "no_validation": False,
    "multioutput": True,
    "allow_nan": False,
    "stateless": False,
    "multilabel": False,
    "_skip_test": False,
    "multioutput_only": False,
    "binary_only": False,
    "requires_fit": True,
}


def _clone_prebuilt_model(build_fn):
    """Clones and compiles a pre-built model when build_fn is an existing
            Keras model instance.

    Arguments:
        build_fn : instance of Keras Model.

    Returns: copy of the input model with no training.
    """
    model = clone_model(build_fn)
    # clone_model does not compy over compilation parameters, do those manually
    model_metadata = saving_utils.model_metadata(build_fn)
    if "training_config" in model_metadata:
        training_config = model_metadata["training_config"]
    else:
        raise ValueError(
            "To use %s as `build_fn`, you must compile" "it first." % build_fn
        )

    model.compile(
        **saving_utils.compile_args_from_training_config(training_config)
    )

    return model


class BaseWrapper:
    """Base class for the Keras scikit-learn wrapper.

    Warning: This class should not be used directly.
    Use descendant classes instead.

    Arguments:
        build_fn: callable function or class instance
        **sk_params: model parameters & fitting parameters

    The `build_fn` should construct, compile and return a Keras model, which
    will then be used to fit/predict. One of the following
    three values could be passed to `build_fn`:
    1. A function
    2. An instance of a class that implements the `__call__` method
    3. An instance of a Keras Model. A copy of this instance will be made
    4. None. This means you implement a class that inherits from `BaseWrapper`,
    `KerasClassifier` or `KerasRegressor`. The `__call__` method of the
    present class will then be treated as the default `build_fn`.
    If `build_fn` has parameters X or y, these will be passed automatically.

    `sk_params` takes both model parameters and fitting parameters. Legal model
    parameters are the arguments of `build_fn`. Note that like all other
    estimators in scikit-learn, `build_fn` or your child class should provide
    default values for its arguments, so that you could create the estimator
    without passing any values to `sk_params`.

    `sk_params` could also accept parameters for calling `fit`, `predict`,
    `predict_proba`, and `score` methods (e.g., `epochs`, `batch_size`).
    fitting (predicting) parameters are selected in the following order:

    1. Values passed to the dictionary arguments of
    `fit`, `predict`, `predict_proba`, and `score` methods
    2. Values passed to `sk_params`
    3. The default values of the `keras.models.Sequential`
    `fit`, `predict`, `predict_proba` and `score` methods

    When using scikit-learn's `grid_search` API, legal tunable parameters are
    those you could pass to `sk_params`, including fitting parameters.
    In other words, you could use `grid_search` to search for the best
    `batch_size` or `epochs` as well as the model parameters.
    """

    # basic legal parameter set, based on functions that will normally be
    # called the model building function will be dynamically added
    _legal_params_fns = [
        Sequential.evaluate,
        Sequential.fit,
        Sequential.predict,
        Model.evaluate,
        Model.fit,
        Model.predict,
    ]

    _sk_params = None
    is_fitted_ = False

    def __init__(self, build_fn=None, **sk_params):

        self.build_fn = build_fn

        if sk_params:

            # for backwards compatibility

            # the sklearn API requires that all __init__ parameters be saved
            # as an instance attribute of the same name
            for name, val in sk_params.items():
                setattr(self, name, val)

            # save keys so that we can count these as __init__ params
            self._sk_params = list(sk_params.keys())

        # check that all __init__ parameters were assigned (as per sklearn API)
        for param in self.get_params(deep=False):
            if not hasattr(self, param):
                raise RuntimeError(
                    "Parameter %s was not assigned, this is req. by sklearn"
                )

    def _check_build_fn(self, build_fn):
        """Checks `build_fn`.

        Arguments:
            build_fn : method or callable class as defined in __init__

        Raises:
            ValueError: if `build_fn` is not valid.
        """
        if build_fn is None:
            # no build_fn, use this class' __call__method
            if not hasattr(self, "__call__"):
                raise ValueError(
                    "If not using the `build_fn` param, "
                    "you must implement `__call__`"
                )
            final_build_fn = self.__call__
        elif isinstance(build_fn, Model):
            # pre-built Keras model
            final_build_fn = _clone_prebuilt_model
        elif inspect.isfunction(build_fn):
            if hasattr(self, "__call__"):
                raise ValueError(
                    "This class cannot implement `__call__` if"
                    " using the `build_fn` parameter"
                )
            # a callable method/function
            final_build_fn = build_fn
        elif (
            callable(build_fn)
            and hasattr(build_fn, "__class__")
            and hasattr(build_fn.__class__, "__call__")
        ):
            if hasattr(self, "__call__"):
                raise ValueError(
                    "This class cannot implement `__call__` if"
                    " using the `build_fn` parameter"
                )
            # an instance of a class implementing __call__
            final_build_fn = build_fn.__call__
        else:
            raise ValueError("`build_fn` must be a callable or None")
        # append legal parameters
        self._legal_params_fns.append(final_build_fn)

        return final_build_fn

    def _build_keras_model(self, X, y, sample_weight, **kwargs):
        """Build the Keras model.

        This method will process all arguments and call the model building
        function with appropriate arguments.

        Arguments:
            X : array-like, shape `(n_samples, n_features)`
                Training samples where `n_samples` is the number of samples
                and `n_features` is the number of features.
            y : array-like, shape `(n_samples,)` or `(n_samples, n_outputs)`
                True labels for `X`.
            sample_weight : array-like of shape (n_samples,)
                Sample weights. The Keras Model must support this.
            **kwargs: dictionary arguments
                Legal arguments are the arguments `build_fn`.
        Returns:
            self : object
                a reference to the instance that can be chain called
                (ex: instance.fit(X,y).transform(X) )
        Raises:
            ValuError : In case sample_weight != None and the Keras model's
                `fit` method does not support that parameter.
        """
        # dynamically build model, i.e. final_build_fn builds a Keras model

        # determine what type of build_fn to use
        final_build_fn = self._check_build_fn(self.build_fn)

        # get model arguments
        model_args = self._filter_params(final_build_fn)

        # add `sample_weight` param
        # while it is not usually needed to build the model, some Keras models
        # require knowledge of the type of sample_weight to be built.
        sample_weight_arg = self._filter_params(
            final_build_fn, params_to_check={"sample_weight": sample_weight}
        )

        # check if the model building function requires X and/or y to be passed
        X_y_args = self._filter_params(
            final_build_fn, params_to_check={"X": X, "y": y}
        )

        # filter kwargs
        kwargs = self._filter_params(final_build_fn, params_to_check=kwargs)

        # combine all arguments
        build_args = {**model_args, **X_y_args, **sample_weight_arg, **kwargs}

        # build model
        model = final_build_fn(**build_args)

        # append legal parameter names from model
        for known_keras_fn in KNOWN_KERAS_FN_NAMES:
            if hasattr(model, known_keras_fn):
                self._legal_params_fns.append(getattr(model, known_keras_fn))

        return model

    def _fit_keras_model(self, X, y, sample_weight, **kwargs):
        """Fits the Keras model.

        This method will process all arguments and call the Keras
        model's `fit` method with approriate arguments.

        Arguments:
            X : array-like, shape `(n_samples, n_features)`
                Training samples where `n_samples` is the number of samples
                and `n_features` is the number of features.
            y : array-like, shape `(n_samples,)` or `(n_samples, n_outputs)`
                True labels for `X`.
            sample_weight : array-like of shape (n_samples,)
                Sample weights. The Keras Model must support this.
            **kwargs: dictionary arguments
                Legal arguments are the arguments of the keras model's
                `fit` method.
        Returns:
            self : object
                a reference to the instance that can be chain called
                (ex: instance.fit(X,y).transform(X) )
        Raises:
            ValuError : In case sample_weight != None and the Keras model's
                        `fit` method does not support that parameter.
        """
        # add `sample_weight` param, required to be explicit by some sklearn
        # functions that use inspect.signature on the `score` method
        if sample_weight is not None:
            # avoid pesky Keras warnings if sample_weight is not used
            kwargs.update({"sample_weight": sample_weight})

        # filter kwargs down to those accepted by self.model_.fit
        kwargs = self._filter_params(self.model_.fit, params_to_check=kwargs)

        if sample_weight is not None and "sample_weight" not in kwargs:
            raise ValueError(
                "Parameter `sample_weight` is unsupported by Keras model %s"
                % self.model_
            )

        # get model.fit's arguments (allows arbitrary model use)
        fit_args = self._filter_params(self.model_.fit)

        # fit model and save history
        # order implies kwargs overwrites fit_args
        fit_args = {**fit_args, **kwargs}

        self.history_ = self.model_.fit(x=X, y=y, **fit_args)

        self.is_fitted_ = True

        # return self to allow fit_transform and such to work
        return self

    def _check_output_model_compatibility(self, y):
        """Checks that the model output number and y shape match, reshape as needed.
        """
        # check if this is a multi-output model
        if self.n_outputs_keras_ != len(y):
            raise RuntimeError(
                "Detected a model with %s ouputs, but y has incompatible"
                " shape %s" % (self.n_outputs_keras_, len(y))
            )

        # tf v1 does not accept single item lists
        # tf v2 does
        if len(y) == 1:
            y = y[0]
        else:
            y = tuple(np.squeeze(y_) for y_ in y)
        return y

    @staticmethod
    def _pre_process_y(y):
        """Handles manipulation of y inputs to fit or score.

        By default, this just makes sure y is 2D.

        Arguments:
            y : 1D or 2D numpy array

        Returns:
            y : numpy array of shape (n_samples, n_ouputs)
            extra_args : dictionary of output attributes, ex: n_outputs_
                    These parameters are added to `self` by `fit` and
                    consumed (but not reset) by `score`.
        """
        if y.ndim == 1:
            y = y.reshape(-1, 1)

        extra_args = dict()

        return y, extra_args

    @staticmethod
    def _post_process_y(y):
        """Handles manipulation of predicted `y` values.

        By default, it joins lists of predictions for multi-ouput models
        into a single numpy array.
        Subclass and override this method to customize processing.

        Arguments:
            y : 2D numpy array or list of numpy arrays
                (the latter is for multi-ouput models)

        Returns:
            y : 2D numpy array with singular dimensions stripped
                or 1D numpy array
            extra_args : attributes of output `y` such as probabilites.
                Currently unused by KerasRegressor but kept for flexibility.
        """
        y = np.column_stack(y)

        extra_args = dict()
        return np.squeeze(y), extra_args

    @staticmethod
    def _pre_process_X(X):
        """Handles manipulation of X before fitting.

        Subclass and override this method to process X, for example
        accomodate a multi-input model.

        Arguments:
            X : 2D numpy array

        Returns:
            X : unchanged 2D numpy array
            extra_args : attributes of output `y` such as probabilites.
                    Currently unused by KerasRegressor but kept for
                    flexibility.
        """
        extra_args = dict()
        return X, extra_args

    def fit(self, X, y, sample_weight=None, **kwargs):
        """Constructs a new model with `build_fn` & fit the model to `(X, y)`.

        Arguments:
            X : array-like, shape `(n_samples, n_features)`
                Training samples where `n_samples` is the number of samples
                and `n_features` is the number of features.
            y : array-like, shape `(n_samples,)` or `(n_samples, n_outputs)`
                True labels for `X`.
            sample_weight : array-like of shape (n_samples,), default=None
                Sample weights. The Keras Model must support this.
            **kwargs: dictionary arguments
                Legal arguments are the arguments of the keras model's `fit`
                method.
        Returns:
            self : object
                a reference to the instance that can be chain called
                (ex: instance.fit(X,y).transform(X) )
        Raises:
            ValueError : In case of invalid shape for `y` argument.
            ValuError : In case sample_weight != None and the Keras model's
                `fit` method does not support that parameter.
        """
        # basic checks
        X, y = check_X_y(
            X,
            y,
            allow_nd=True,  # allow X to have more than 2 dimensions
            multi_output=True,  # allow y to be 2D
        )

        X = check_array(X, allow_nd=True, dtype=["float64", "int"])

        if sample_weight is not None:
            sample_weight = _check_sample_weight(
                sample_weight, X, dtype=["float64", "int"]
            )

        # pre process X, y
        X, _ = self._pre_process_X(X)
        y, extra_args = self._pre_process_y(y)
        # update self.classes_, self.n_outputs_, self.n_classes_ and
        #  self.cls_type_
        for attr_name, attr_val in extra_args.items():
            setattr(self, attr_name, attr_val)

        # build model
        self.model_ = self._build_keras_model(
            X, y, sample_weight=sample_weight, **kwargs
        )

        y = self._check_output_model_compatibility(y)

        # fit model
        return self._fit_keras_model(
            X, y, sample_weight=sample_weight, **kwargs
        )

    def predict(self, X, **kwargs):
        """Returns predictions for the given test data.

        Arguments:
            X: array-like, shape `(n_samples, n_features)`
                Test samples where `n_samples` is the number of samples
                and `n_features` is the number of features.
            **kwargs: dictionary arguments
                Legal arguments are the arguments of `self.model_.predict`.

        Returns:
            preds: array-like, shape `(n_samples,)`
                Predictions.
        """
        # check if fitted
        if not self.is_fitted_:
            raise NotFittedError(
                "Estimator %s needs to be fit before `predict` "
                "can be called" % self
            )

        # basic input checks
        X = check_array(X, allow_nd=True, dtype=["float64", "int"])

        # pre process X
        X, _ = self._pre_process_X(X)

        # filter kwargs and get attributes for predict
        kwargs = self._filter_params(
            self.model_.predict, params_to_check=kwargs
        )
        predict_args = self._filter_params(self.model_.predict)

        # predict with Keras model
        pred_args = {**predict_args, **kwargs}
        y_pred = self.model_.predict(X, **pred_args)

        # post process y
        y, _ = self._post_process_y(y_pred)
        return y

    def score(self, X, y, sample_weight=None, **kwargs):
        """Returns the mean accuracy on the given test data and labels.

        Arguments:
            X: array-like, shape `(n_samples, n_features)`
                Test samples where `n_samples` is the number of samples
                and `n_features` is the number of features.
            y: array-like, shape `(n_samples,)` or `(n_samples, n_outputs)`
                True labels for `X`.
            sample_weight : array-like of shape (n_samples,), default=None
                Sample weights. The Keras Model must support this.
            **kwargs: dictionary arguments
                Legal arguments are those of self.model_.evaluate.

        Returns:
            score: float
                Mean accuracy of predictions on `X` wrt. `y`.

        Raises:
            ValueError: If the underlying model isn't configured to
                compute accuracy. You should pass `metrics=["accuracy"]` to
                the `.compile()` method of the model.
        """
        # validate sample weights
        if sample_weight is not None:
            sample_weight = _check_sample_weight(
                sample_weight, X, dtype=["float64", "int"]
            )

        # pre process X, y
        _, extra_args = self._pre_process_y(y)

        # compute Keras model score
        y_pred = self.predict(X, **kwargs)

        return self._scorer(y, y_pred, sample_weight=sample_weight)

    def _filter_params(self, fn, params_to_check=None):
        """Filters all instance attributes (parameters) and
             returns those in `fn`'s arguments.

        Arguments:
            fn : arbitrary function
            params_to_check : dictionary, parameters to check.
                Defaults to checking all attributes of this estimator.

        Returns:
            res : dictionary containing variables
                in both self and `fn`'s arguments.
        """
        res = {}
        for name, value in (params_to_check or self.__dict__).items():
            if has_arg(fn, name):
                res.update({name: value})
        return res

    def _get_param_names(self):
        """Get parameter names for the estimator"""
        # collect all __init__ params for this base class as well as
        # all child classes
        parameters = []
        # reverse the MRO, we want the 1st one to overwrite the nth
        # for class_ in reversed(inspect.getmro(self.__class__)):
        for p in inspect.signature(
            self.__class__.__init__
        ).parameters.values():
            if p.kind not in ARGS_KWARGS_IDENTIFIERS and p.name != "self":
                parameters.append(p)

        # Extract and sort argument names excluding 'self'
        if self._sk_params:
            return sorted([p.name for p in parameters] + self._sk_params)

        return sorted([p.name for p in parameters])

    def get_params(self, deep=True):
        """Get parameters for this estimator.

        This method mimics sklearn.base.BaseEstimator.get_params

        Arguments:
            deep : bool, default=True
                If True, will return the parameters for this estimator and
                contained subobjects that are estimators.

        Returns:
            params : mapping of string to any
                Parameter names mapped to their values.
        """
        out = dict()
        for key in self._get_param_names():
            value = getattr(self, key)
            if deep and hasattr(value, "get_params"):
                deep_items = value.get_params().items()
                out.update((key + "__" + k, val) for k, val in deep_items)
            out[key] = value
        return out

    def set_params(self, **params):
        """Set the parameters of this estimator.

        The method works on simple estimators as well as on nested objects
        (such as in sklearn Pipelines). The latter have parameters of the form
        ``<component>__<parameter>`` so that it's possible to update each
        component of a nested object.

        This method mimics sklearn.base.BaseEstimator.set_params

        Arguments:
            **params : dict
                Estimator parameters.
        Returns:
            self : object
                Estimator instance.
        """
        if not params:
            # Simple optimization to gain speed
            return self
        valid_params = self.get_params(deep=True)

        nested_params = defaultdict(dict)  # grouped by prefix
        for key, value in params.items():
            key, delim, sub_key = key.partition("__")
            if key not in valid_params:
                raise ValueError(
                    "Invalid parameter %s for estimator %s. "
                    "Check the list of available parameters "
                    "with `estimator.get_params().keys()`." % (key, self)
                )
            if delim:
                nested_params[key][sub_key] = value
            else:
                setattr(self, key, value)
                valid_params[key] = value

        for key, sub_params in nested_params.items():
            valid_params[key].set_params(**sub_params)

        return self

    def _more_tags(self):
        return _DEFAULT_TAGS

    def _get_tags(self):
        collected_tags = {}
        for base_class in reversed(inspect.getmro(self.__class__)):
            if hasattr(base_class, "_more_tags"):
                # need the if because mixins might not have _more_tags
                # but might do redundant work in estimators
                # (i.e. calling more tags on BaseEstimator multiple times)
                more_tags = base_class._more_tags(self)
                collected_tags.update(more_tags)
        return collected_tags

    def __getstate__(self):
        """Get state of instance as a picklable/copyable dict.

             Used by various scikit-learn methods to clone estimators. Also
             used for pickling.
             Because some objects (mainly Keras `Model` instances) are not
             pickleable, it is necessary to iterate through all attributes
             and clone the unpicklables manually.

        Returns:
            state : dictionary containing a copy of all attributes of this
                    estimator with Keras Model instances being saved as
                    HDF5 binary objects.
        """

        def _pack_obj(obj):
            """Recursively packs objects.
            """
            try:
                return copy.deepcopy(obj)
            except TypeError:
                pass  # is this a Keras serializable?
            try:
                model_metadata = saving_utils.model_metadata(obj)
                training_config = model_metadata["training_config"]
                model = serialize(obj)
                weights = obj.get_weights()
                return SavedKerasModel(
                    cls=obj.__class__,
                    model=model,
                    weights=weights,
                    training_config=training_config,
                )
            except (TypeError, AttributeError):
                pass  # try manually packing the object
            if hasattr(obj, "__dict__"):
                for key, val in obj.__dict__.items():
                    obj.__dict__[key] = _pack_obj(val)
                return obj
            if isinstance(obj, (list, tuple)):
                obj_type = type(obj)
                new_obj = obj_type([_pack_obj(o) for o in obj])
                return new_obj

            return obj

        state = self.__dict__.copy()
        for key, val in self.__dict__.items():
            state[key] = _pack_obj(val)
        return state

    def __setstate__(self, state):
        """Set state of live object from state saved via __getstate__.

             Because some objects (mainly Keras `Model` instances) are not
             pickleable, it is necessary to iterate through all attributes
             and clone the unpicklables manually.

        Arguments:
            state : dict
                dictionary from a previous call to `get_state` that will be
                unpacked to this instance's `__dict__`.
        """

        def _unpack_obj(obj):
            """Recursively unpacks objects.
            """
            if isinstance(obj, SavedKerasModel):
                restored_model = deserialize(obj.model)
                training_config = obj.training_config
                restored_model.compile(
                    **saving_utils.compile_args_from_training_config(
                        training_config
                    )
                )
                restored_model.set_weights(obj.weights)
                return restored_model
            if hasattr(obj, "__dict__"):
                for key, val in obj.__dict__.items():
                    obj.__dict__[key] = _unpack_obj(val)
                return obj
            if isinstance(obj, (list, tuple)):
                obj_type = type(obj)
                new_obj = obj_type([_unpack_obj(o) for o in obj])
                return new_obj

            return obj  # not much we can do at this point, cross fingers

        for key, val in state.items():
            setattr(self, key, _unpack_obj(val))


class KerasClassifier(BaseWrapper):
    """Implementation of the scikit-learn classifier API for Keras.
    """

    _estimator_type = "classifier"
    _scorer = staticmethod(sklearn_accuracy_score)

    def _more_tags(self):
        return {"multilabel": True}

    @staticmethod
    def _pre_process_y(y):
        """Handles manipulation of y inputs to fit or score.

             For KerasClassifier, this handles interpreting classes from `y`.

        Arguments:
            y : 1D or 2D numpy array

        Returns:
            y : modified 2D numpy array with 0 indexed integer class labels.
            classes_ : list of original class labels.
            n_classes_ : number of classes.
            one_hot_encoded : True if input y was one-hot-encoded.
        """
        y, _ = super(KerasClassifier, KerasClassifier)._pre_process_y(y)

        cls_type_ = type_of_target(y)

        n_outputs_ = y.shape[1]

        if cls_type_ == "binary":
            # y = array([1, 0, 1, 0])
            # single task, single label, binary classification
            n_outputs_keras_ = 1  # single sigmoid output expected
            classes_ = np.unique(y)
            # convert to 0 indexed classes
            y = np.searchsorted(classes_, y)
            classes_ = [classes_]
            y = [y]
        elif cls_type_ == "multiclass":
            # y = array([1, 5, 2])
            n_outputs_keras_ = 1  # single softmax output expected
            classes_ = np.unique(y)
            # convert to 0 indexed classes
            y = np.searchsorted(classes_, y)
            classes_ = [classes_]
            y = [y]
        elif cls_type_ == "multilabel-indicator":
            # y = array([1, 1, 1, 0], [0, 0, 1, 1])
            # split into lists for multi-output Keras
            # will be processed as multiple binary classifications
            classes_ = [np.array([0, 1])] * y.shape[1]
            y = np.split(y, y.shape[1], axis=1)
            n_outputs_keras_ = len(y)
        elif cls_type_ == "multiclass-multioutput":
            # y = array([1, 0, 5], [2, 1, 3])
            # split into lists for multi-output Keras
            # each will be processesed as a seperate multiclass problem
            y = np.split(y, y.shape[1], axis=1)
            classes_ = [np.unique(y_) for y_ in y]
            n_outputs_keras_ = len(y)
        else:
            raise ValueError("Unknown label type: %r" % cls_type_)

        # self.classes_ is kept as an array when n_outputs==1 for compatibility
        # with ensembles and other meta estimators
        # which do not support multioutput
        if len(classes_) == 1:
            n_classes_ = classes_[0].size
            classes_ = classes_[0]
            n_outputs_ = 1
        else:
            n_classes_ = [class_.shape[0] for class_ in classes_]
            n_outputs_ = len(n_classes_)

        extra_args = {
            "classes_": classes_,
            "n_outputs_": n_outputs_,
            "n_outputs_keras_": n_outputs_keras_,
            "n_classes_": n_classes_,
            "cls_type_": cls_type_,
        }

        return y, extra_args

    def _post_process_y(self, y):
        """Reverts _pre_process_inputs to return predicted probabilites
             in formats sklearn likes as well as retrieving the original
             classes.
        """
        if not isinstance(y, list):
            # convert single-target y to a list for easier processing
            y = [y]

        # self.classes_ is kept as an array when n_outputs==1 for compatibility
        # with meta estimators
        if self.n_outputs_ == 1:
            cls_ = [self.classes_]
        else:
            cls_ = self.classes_

        y = copy.deepcopy(y)
        cls_type_ = self.cls_type_

        class_predictions = []
        for i, (y_, classes_) in enumerate(zip(y, cls_)):
            if cls_type_ == "binary":
                if y_.shape[1] == 1:
                    # result from a single sigmoid output
                    class_predictions.append(
                        classes_[np.where(y_ > 0.5, 1, 0)]
                    )
                    # reformat so that we have 2 columns
                    y[i] = np.concatenate([1 - y_, y_], axis=1)
                else:
                    # array([0.9, 0.1], [.2, .8]) -> array(['yes', 'no'])
                    class_predictions.append(
                        classes_[np.argmax(np.where(y_ > 0.5, 1, 0), axis=1)]
                    )
            elif cls_type_ == "multiclass":
                # array([0.8, 0.1, 0.1], [.1, .8, .1]) ->
                # array(['apple', 'orange'])
                class_predictions.append(classes_[np.argmax(y_, axis=1)])
            elif cls_type_ == "multilabel-indicator":
                class_predictions.append(np.where(y_ > 0.5, 1, 0))
            elif cls_type_ == "multiclass-multioutput":
                # array([0.9, 0.1], [.2, .8]) -> array(['apple', 'fruit'])
                class_predictions.append(classes_[np.argmax(y_, axis=1)])
            else:
                raise ValueError(
                    "Unknown classification task type '%s'" % cls_
                )

        class_probabilities = np.squeeze(np.column_stack(y))

        y = np.squeeze(np.column_stack(class_predictions))

        extra_args = {"class_probabilities": class_probabilities}

        return y, extra_args

    def _check_output_model_compatibility(self, y):
        """Checks that the model output number and loss functions match y.
        """
        # check loss function to adjust the encoding of the input
        # we need to do this to mimick scikit-learn behavior
        if isinstance(self.model_.loss, list):
            losses = self.model_.loss
        else:
            losses = [self.model_.loss] * self.n_outputs_
        for i, (loss, y_) in enumerate(zip(losses, y)):
            if is_categorical_crossentropy(loss) and (
                y_.ndim == 1 or y_.shape[1] == 1
            ):
                y[i] = to_categorical(y_)

        return super()._check_output_model_compatibility(y)

    def predict_proba(self, X, **kwargs):
        """Returns class probability estimates for the given test data.

        Arguments:
            X: array-like, shape `(n_samples, n_features)`
                Test samples where `n_samples` is the number of samples
                and `n_features` is the number of features.
            **kwargs: dictionary arguments
                Legal arguments are the arguments
                of `Sequential.predict_classes`.

        Returns:
            proba: array-like, shape `(n_samples, n_outputs)`
                Class probability estimates.
                In the case of binary classification,
                to match the scikit-learn API,
                will return an array of shape `(n_samples, 2)`
                (instead of `(n_sample, 1)` as in Keras).
        """
        # check if fitted
        if not self.is_fitted_:
            raise NotFittedError(
                "Estimator %s needs to be fit before `predict` "
                "can be called" % self
            )

        # basic input checks
        X = check_array(X, allow_nd=True, dtype=["float64", "int"])

        # pre process X
        X, _ = self._pre_process_X(X)

        # filter kwargs and get attributes that are inputs to model.predict
        kwargs = self._filter_params(
            self.model_.predict, params_to_check=kwargs
        )
        predict_args = self._filter_params(self.model_.predict)

        # call the Keras model
        predict_args = {**predict_args, **kwargs}
        outputs = self.model_.predict(X, **predict_args)

        # join list of outputs into single output array
        _, extra_args = self._post_process_y(outputs)

        class_probabilities = extra_args["class_probabilities"]

        return class_probabilities


class KerasRegressor(BaseWrapper):
    """Implementation of the scikit-learn regressor API for Keras.
    """

    _estimator_type = "regressor"
    _scorer = staticmethod(sklearn_r2_score)

    n_outputs_ = None

    def fit(self, X, y, sample_weight=None, **kwargs):
        """Convert y to float, regressors cannot accept ints."""
        y = check_array(y, dtype="float64", ensure_2d=False)
        return super().fit(X, y, sample_weight=sample_weight, **kwargs)

    def _post_process_y(self, y):
        """Ensures output is float64 and squeeze."""
        return np.squeeze(y.astype("float64")), dict()

    def _pre_process_y(self, y):
        """Split y for multi-output tasks.
        """
        y, _ = super(KerasRegressor, self)._pre_process_y(y)

        n_outputs_ = y.shape[1]
        # for regression, multi-output is handled by single Keras output
        n_outputs_keras_ = 1

        extra_args = {
            "n_outputs_": n_outputs_,
            "n_outputs_keras_": n_outputs_keras_,
        }

        y = [y]  # pack into single output list

        return y, extra_args

    def score(self, X, y, sample_weight=None, **kwargs):
        """Returns the mean loss on the given test data and labels.

        Arguments:
            X: array-like, shape `(n_samples, n_features)`
                Test samples where `n_samples` is the number of samples
                and `n_features` is the number of features.
            y: array-like, shape `(n_samples,)`
                True labels for `X`.
            **kwargs: dictionary arguments
                Legal arguments are the arguments of `Sequential.evaluate`.

        Returns:
            score: float
                Mean accuracy of predictions on `X` wrt. `y`.
        """
        res = super(KerasRegressor, self).score(X, y, sample_weight, **kwargs)

        # check loss function and warn if it is not the same as score function
        if self.model_.loss not in (
            "mean_squared_error",
            self.root_mean_squared_error,
        ):
            warnings.warn(
                "R^2 is used to compute the score, it is advisable to use"
                " a compatible loss function. This class provides an R^2"
                " implementation in `KerasRegressor"
                ".root_mean_squared_error`."
            )

        return res

    @staticmethod
    @register_keras_serializable()
    def root_mean_squared_error(y_true, y_pred):
        """A simple Keras implementation of R^2 that can be used as a Keras
             loss function.

             Since `score` uses R^2, it is
             advisable to use the same loss/metric when optimizing the model.
        """
        ss_res = K.sum(K.square(y_true - y_pred), axis=0)
        ss_tot = K.sum(K.square(y_true - K.mean(y_true, axis=0)), axis=0)
        return K.mean(1 - ss_res / (ss_tot + K.epsilon()), axis=-1)
