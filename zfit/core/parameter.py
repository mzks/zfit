"""Define Parameter which holds the value."""
#  Copyright (c) 2020 zfit
import functools
from collections import OrderedDict
from contextlib import suppress
from typing import Callable, Iterable, Union, Optional

import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
# TF backwards compatibility
from ordered_set import OrderedSet
from tensorflow.python import ops, array_ops
from tensorflow.python.client.session import register_session_run_conversion_functions
from tensorflow.python.ops.resource_variable_ops import ResourceVariable as TFVariable

from zfit import z
from zfit.util.container import convert_to_container
from . import interfaces as zinterfaces
from .interfaces import ZfitModel, ZfitParameter, ZfitIndependentParameter
from ..core.baseobject import BaseNumeric
from ..settings import ztypes, run
from ..util import ztyping
from ..util.cache import invalidates_cache
from ..util.exception import LogicalUndefinedOperationError, NameAlreadyTakenError, BreakingAPIChangeError, \
    WorkInProgressError, ParameterNotIndependentError, IllegalInGraphModeError
from ..util.temporary import TemporarilySet


class MetaBaseParameter(type(tf.Variable), type(zinterfaces.ZfitParameter)):  # resolve metaclasses
    pass


def register_tensor_conversion(convertable, overload_operators=True, priority=1):  # higher then any tf conversion
    fetch_function = lambda variable: ([variable.read_value()],
                                       lambda val: val[0])
    feed_function = lambda feed, feed_val: [(feed.read_value(), feed_val)]
    feed_function_for_partial_run = lambda feed: [feed.read_value()]
    ops.register_dense_tensor_like_type(convertable)

    register_session_run_conversion_functions(tensor_type=convertable, fetch_function=fetch_function,
                                              feed_function=feed_function,
                                              feed_function_for_partial_run=feed_function_for_partial_run)

    def _dense_var_to_tensor(var, dtype=None, name=None, as_ref=False):
        return var._dense_var_to_tensor(dtype=dtype, name=name, as_ref=as_ref)

    ops.register_tensor_conversion_function(convertable, _dense_var_to_tensor, priority=priority)
    if overload_operators:
        convertable._OverloadAllOperators()


class OverloadableMixin(ZfitParameter):

    def _dense_var_to_tensor(self, dtype=None, name=None, as_ref=False):
        del name
        if dtype and not dtype.is_compatible_with(self.dtype):
            raise ValueError(
                "Incompatible type conversion requested to type '%s' for variable "
                "of type '%s'" % (dtype.name, self.dtype.name))
        if as_ref:
            if hasattr(self, '_ref'):
                return self._ref()
            else:
                raise RuntimeError("Why is this needed?")
        else:
            return self.value()

    def _AsTensor(self):
        return self.value()

    @classmethod
    def _OverloadAllOperators(cls):  # pylint: disable=invalid-name
        """Register overloads for all operators."""
        for operator in ops.Tensor.OVERLOADABLE_OPERATORS:
            cls._OverloadOperator(operator)
        # For slicing, bind getitem differently than a tensor (use SliceHelperVar
        # instead)
        # pylint: disable=protected-access
        setattr(cls, "__getitem__", array_ops._SliceHelperVar)

    @classmethod
    def _OverloadOperator(cls, operator):  # pylint: disable=invalid-name
        """Defer an operator overload to `ops.Tensor`.
        We pull the operator out of ops.Tensor dynamically to avoid ordering issues.
        Args:
          operator: string. The operator name.
        """
        # We can't use the overload mechanism on __eq__ & __ne__ since __eq__ is
        # called when adding a variable to sets. As a result we call a.value() which
        # causes infinite recursion when operating within a GradientTape
        # TODO(gjn): Consider removing this
        if operator == "__eq__" or operator == "__ne__":
            return

        tensor_oper = getattr(ops.Tensor, operator)

        def _run_op(a, *args, **kwargs):
            # pylint: disable=protected-access
            return tensor_oper(a.value(), *args, **kwargs)

        functools.update_wrapper(_run_op, tensor_oper)
        setattr(cls, operator, _run_op)


register_tensor_conversion(OverloadableMixin, overload_operators=True)


class WrappedVariable(metaclass=MetaBaseParameter):

    def __init__(self, initial_value, constraint, *args, **kwargs):

        super().__init__(*args, **kwargs)
        self.variable = tf.Variable(initial_value=initial_value, constraint=constraint, name=self.name,
                                    dtype=self.dtype)

    @property
    def name(self):
        raise NotImplementedError

    @property
    def constraint(self):
        return self.variable.constraint

    @property
    def dtype(self):
        return self.variable.dtype

    def value(self):
        return self.variable.value()

    def read_valu(self):
        return self.variable.read_value()

    @property
    def shape(self):
        return self.variable.shape

    def numpy(self):
        return self.variable.numpy()

    def assign(self, value, use_locking=False, name=None, read_value=True):
        return self.variable.assign(value=value, use_locking=use_locking,
                                    name=name, read_value=read_value)

    def _dense_var_to_tensor(self, dtype=None, name=None, as_ref=False):
        del name
        if dtype is not None and dtype != self.dtype:
            return NotImplemented
        if as_ref:
            return self.variable.read_value().op.inputs[0]
        else:
            return self.variable.value()

    def _AsTensor(self):
        return self.variable.value()

    @staticmethod
    def _OverloadAllOperators():  # pylint: disable=invalid-name
        """Register overloads for all operators."""
        for operator in ops.Tensor.OVERLOADABLE_OPERATORS:
            WrappedVariable._OverloadOperator(operator)
        # For slicing, bind getitem differently than a tensor (use SliceHelperVar
        # instead)
        # pylint: disable=protected-access
        setattr(WrappedVariable, "__getitem__", array_ops._SliceHelperVar)

    @staticmethod
    def _OverloadOperator(operator):  # pylint: disable=invalid-name
        """Defer an operator overload to `ops.Tensor`.
        We pull the operator out of ops.Tensor dynamically to avoid ordering issues.
        Args:
          operator: string. The operator name.
        """

        tensor_oper = getattr(ops.Tensor, operator)

        def _run_op(a, *args):
            # pylint: disable=protected-access
            value = a._AsTensor()
            return tensor_oper(value, *args)

        # Propagate __doc__ to wrapper
        try:
            _run_op.__doc__ = tensor_oper.__doc__
        except AttributeError:
            pass

        setattr(WrappedVariable, operator, _run_op)


register_tensor_conversion(WrappedVariable, overload_operators=True)


# class ComposedVariable(metaclass=MetaBaseParameter):
# class ComposedVariable:
#
#     def __init__(self, name: str, value_fn: Callable, **kwargs):
#         super().__init__(name=name, **kwargs)
#
#         if not callable(value_fn):
#             raise TypeError("`value_fn` is not callable.")
#         self._value_fn = value_fn
#
#     @property
#     def name(self):
#         raise RuntimeError
#
#     @property
#     def shape(self):
#         raise RuntimeError
#
#     @property
#     def dtype(self):
#         raise NotImplementedError
#
#     def value(self):
#         return tf.convert_to_tensor(self._value_fn(), dtype=self.dtype)
#
#     def read_value(self):
#         return tf.identity(self.value())
#
#     def numpy(self):
#         return self.value().numpy()
#
#     # TODO: move to userwarning class?
#     def assign(self, value, use_locking=False, name=None, read_value=True):
#         raise LogicalUndefinedOperationError("Cannot assign to a fixed/composed parameter")
#
#     def _dense_var_to_tensor(self, dtype=None, name=None, as_ref=False):
#         del name
#         if dtype is not None and dtype != self.dtype:
#             return NotImplemented
#         if as_ref:
#             # return "NEVER READ THIS"
#             raise LogicalUndefinedOperationError("There is no ref for the fixed/composed parameter")
#         else:
#             return self.value()
#
#     def _AsTensor(self):
#         return self.value()
#
#     @staticmethod
#     def _OverloadAllOperators():  # pylint: disable=invalid-name
#         """Register overloads for all operators."""
#         for operator in ops.Tensor.OVERLOADABLE_OPERATORS:
#             ComposedVariable._OverloadOperator(operator)
#         # For slicing, bind getitem differently than a tensor (use SliceHelperVar
#         # instead)
#         # pylint: disable=protected-access
#         setattr(ComposedVariable, "__getitem__", array_ops._SliceHelperVar)
#
#     @staticmethod
#     def _OverloadOperator(operator):  # pylint: disable=invalid-name
#         """Defer an operator overload to `ops.Tensor`.
#         We pull the operator out of ops.Tensor dynamically to avoid ordering issues.
#         Args:
#           operator: string. The operator name.
#         """
#
#         tensor_oper = getattr(ops.Tensor, operator)
#
#         def _run_op(a, *args):
#             # pylint: disable=protected-access
#             value = a._AsTensor()
#             return tensor_oper(value, *args)
#
#         # Propagate __doc__ to wrapper
#         try:
#             _run_op.__doc__ = tensor_oper.__doc__
#         except AttributeError:
#             pass
#
#         setattr(ComposedVariable, operator, _run_op)
#
#
# register_tensor_conversion(ComposedVariable, overload_operators=True)


class BaseParameter(ZfitParameter, metaclass=MetaBaseParameter):
    pass
    # @property
    # def dtype(self) -> tf.DType:
    #     return self.value().dtype
    #
    # @property
    # def shape(self):
    #     return self.value().shape


class ZfitParameterMixin(BaseNumeric):
    _existing_params = OrderedDict()

    def __init__(self, name, **kwargs):
        if name in self._existing_params:
            raise NameAlreadyTakenError("Another parameter is already named {}. "
                                        "Use a different, unique one.".format(name))
        self._existing_params.update({name: self})
        self._name = name
        super().__init__(name=name, **kwargs)

    # property needed here to overwrite the name of tf.Variable
    @property
    def name(self) -> str:
        return self._name

    def __del__(self):
        with suppress(NotImplementedError):  # PY36 bug, gets stuck
            if self.name in self._existing_params:  # bug, creates segmentation fault in unittests
                del self._existing_params[self.name]
        with suppress(AttributeError, NotImplementedError):  # if super does not have a __del__
            super().__del__(self)

    def __add__(self, other):
        if isinstance(other, (ZfitModel, ZfitParameter)):
            from . import operations
            with suppress(NotImplementedError):
                return operations.add(self, other)
        return super().__add__(other)

    def __radd__(self, other):
        if isinstance(other, (ZfitModel, ZfitParameter)):
            from . import operations
            with suppress(NotImplementedError):
                return operations.add(other, self)
        return super().__radd__(other)

    def __mul__(self, other):
        if isinstance(other, (ZfitModel, ZfitParameter)):
            from . import operations
            with suppress(NotImplementedError):
                return operations.multiply(self, other)
        return super().__mul__(other)

    def __rmul__(self, other):
        if isinstance(other, (ZfitModel, ZfitParameter)):
            from . import operations
            with suppress(NotImplementedError):
                return operations.multiply(other, self)
        return super().__rmul__(other)

    def __eq__(self, other):
        return id(self) == id(other)

    def __hash__(self):
        return id(self)


class TFBaseVariable(TFVariable, metaclass=MetaBaseParameter):
    # class TFBaseVariable(WrappedVariable, metaclass=MetaBaseParameter):

    # Needed, otherwise tf variable complains about the name not having a ':' in there
    @property
    def _shared_name(self):
        return self.name


class Parameter(ZfitParameterMixin, TFBaseVariable, BaseParameter, ZfitIndependentParameter):
    """Class for fit parameters, derived from TF Variable class.
    """
    _independent = True
    _independent_params = []
    DEFAULT_STEP_SIZE = 0.001

    def __init__(self, name: str,
                 value: ztyping.NumericalScalarType,
                 lower_limit: Optional[ztyping.NumericalScalarType] = None,
                 upper_limit: Optional[ztyping.NumericalScalarType] = None,
                 step_size: Optional[ztyping.NumericalScalarType] = None,
                 floating: bool = True,
                 dtype: tf.DType = ztypes.float, **kwargs):
        """
            name : name of the parameter,
            value : starting value
            lower_limit : lower limit
            upper_limit : upper limit
            step_size : step size
        """
        self._independent_params.append(self)
        # TODO: sanitize input for TF2
        self._lower_limit_neg_inf = None
        self._upper_limit_neg_inf = None
        if lower_limit is None:
            self._lower_limit_neg_inf = tf.cast(-np.infty, dtype)
        if upper_limit is None:
            self._upper_limit_neg_inf = tf.cast(np.infty, dtype)
        value = tf.cast(value, dtype=ztypes.float)

        def constraint(x):
            return tfp.math.clip_by_value_preserve_gradient(x, clip_value_min=self.lower,
                                                            clip_value_max=self.upper)

        super().__init__(initial_value=value, dtype=dtype, name=name, constraint=constraint,
                         params={}, **kwargs)

        self.lower = tf.cast(lower_limit, dtype=ztypes.float) if lower_limit is not None else lower_limit
        self.upper = tf.cast(upper_limit, dtype=ztypes.float) if upper_limit is not None else upper_limit
        self.floating = floating
        self.step_size = step_size

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._independent = True  # overwriting independent only for subclass/instance

    @property
    def lower(self):
        limit = self._lower_limit
        if limit is None:
            limit = self._lower_limit_neg_inf
        return limit

    @lower.setter
    @invalidates_cache
    def lower(self, value):
        if value is None and self._lower_limit_neg_inf is None:
            self._lower_limit_neg_inf = tf.cast(-np.infty, dtype=ztypes.float)
        self._lower_limit = value

    @property
    def upper(self):
        limit = self._upper_limit
        if limit is None:
            limit = self._upper_limit_neg_inf
        return limit

    @upper.setter
    @invalidates_cache
    def upper(self, value):
        if value is None and self._upper_limit_neg_inf is None:
            self._upper_limit_neg_inf = tf.cast(np.infty, dtype=ztypes.float)
        self._upper_limit = value

    @property
    def has_limits(self) -> bool:
        """If the parameter has limits set or not

        Returns:
            bool
        """
        no_limits = self._lower_limit is None and self._upper_limit is None
        return not no_limits

    @property
    def at_limit(self) -> tf.Tensor:
        """If the value is at the limit (or over it).

        The precision is up to 1e-5 relative.

        Returns:
            `tf.Tensor`: Boolean `tf.Tensor` that tells whether the value is at the limits.

        """
        if not self.has_limits:
            return tf.constant(False)

        # Adding a slight tolerance to make sure we're not tricked by numerics
        at_lower = z.unstable.less_equal(self.value(), self.lower + (tf.math.abs(self.lower * 1e-5)))
        at_upper = z.unstable.greater_equal(self.value(), self.upper - (tf.math.abs(self.upper * 1e-5)))
        return z.unstable.logical_or(at_lower, at_upper)

    def value(self):
        value = super().value()
        if self.has_limits:
            value = self.constraint(value)
        return value

    def read_value(self):
        value = super().read_value()
        if self.has_limits:
            value = self.constraint(value)
        return value

    @property
    def floating(self):
        if self._floating and (hasattr(self, 'trainable') and not self.trainable):
            raise RuntimeError("Floating is set to true but tf Variable is not trainable.")
        return self._floating

    @floating.setter
    def floating(self, value):
        if not isinstance(value, bool):
            raise TypeError("floating has to be a boolean.")
        self._floating = value

    def _get_dependents(self):
        return {self}

    @property
    def independent(self):
        return self._independent

    @property
    def step_size(self) -> tf.Tensor:  # TODO: improve default step_size?
        """Step size of the parameter, the estimated order of magnitude of the uncertainty.

        This can be crucial to tune for the minimization. A too large `step_size` can produce NaNs, a too small won't
        converge.

        If the step size is not set, the `DEFAULT_STEP_SIZE` is used.

        Returns:
            :py:class:`tf.Tensor`: the step size
        """
        step_size = self._step_size
        if step_size is None:
            #     # auto-infer from limits
            #     step_splits = 1e5
            #     if self.has_limits:
            #         step_size = (self.upper_limit - self.lower_limit) / step_splits  # TODO improve? can be tensor?
            #     else:
            #         step_size = self.DEFAULT_STEP_SIZE
            #     if np.isnan(step_size):
            #         if self.lower_limit == -np.infty or self.upper_limit == np.infty:
            #             step_size = self.DEFAULT_STEP_SIZE
            #         else:
            #             raise ValueError("Could not set step size. Is NaN.")
            #     # step_size = z.to_real(step_size)
            #     self.step_size = step_size
            step_size = self.DEFAULT_STEP_SIZE
        step_size = z.convert_to_tensor(step_size)
        return step_size

    @step_size.setter
    def step_size(self, value):
        if value is not None:
            value = z.convert_to_tensor(value, preferred_dtype=ztypes.float)
            value = tf.cast(value, dtype=ztypes.float)
        self._step_size = value

    def set_value(self, value: ztyping.NumericalScalarType):
        """Set the :py:class:`~zfit.Parameter` to `value` (temporarily if used in a context manager).

        This operation won't, compared to the assign, return the read value but an object that *can* act as a context
        manager.

        Args:
            value (float): The value the parameter will take on.
        """
        super_assign = super().assign

        def getter():
            return self.value()

        def setter(value):
            super_assign(value=value, read_value=False)

        return TemporarilySet(value=value, setter=setter, getter=getter)

    def randomize(self, minval: Optional[ztyping.NumericalScalarType] = None,
                  maxval: Optional[ztyping.NumericalScalarType] = None,
                  sampler: Callable = tf.random.uniform) -> tf.Tensor:
        """Update the parameter with a randomised value between minval and maxval and return it.


        Args:
            minval (Numerical): The lower bound of the sampler. If not given, `lower_limit` is used.
            maxval (Numerical): The upper bound of the sampler. If not given, `upper_limit` is used.
            sampler (): A sampler with the same interface as `tf.random.uniform`

        Returns:
            `tf.Tensor`: The sampled value
        """
        if not run.experimental_is_eager():
            raise IllegalInGraphModeError("Randomizing values in a parameter within Graph mode is most probably not"
                                          " what is ")
        if minval is None:
            minval = self.lower
        else:
            minval = tf.cast(minval, dtype=self.dtype)
        if maxval is None:
            maxval = self.upper
        else:
            maxval = tf.cast(maxval, dtype=self.dtype)

        value = sampler(size=self.shape, low=minval, high=maxval)

        self.set_value(value=value)
        return value

    def __del__(self):
        self._independent_params.remove(self)
        super().__del__()

    def __repr__(self):
        if hasattr(self, "numpy"):  # more explicit: we check for exactly this attribute, nothing inside numpy
            value = self.numpy()
        else:
            value = "graph-node"
        return f"<zfit.{self.__class__.__name__} '{self.name}' floating={self.floating} value={value:.4g}>"

    # LEGACY, deprecate?
    @property
    def lower_limit(self):
        return self.lower

    @lower_limit.setter
    def lower_limit(self, value):
        self.lower = value

    @property
    def upper_limit(self):
        return self.upper

    @upper_limit.setter
    def upper_limit(self, value):
        self.upper = value


class BaseComposedParameter(ZfitParameterMixin, OverloadableMixin, BaseParameter):

    def __init__(self, params, value_fn, name="BaseComposedParameter", **kwargs):
        # 0.4 breaking
        if 'value' in kwargs:
            raise BreakingAPIChangeError("'value' cannot be provided any longer, `value_fn` is needed.")
        super().__init__(name=name, params=params, **kwargs)
        if not callable(value_fn):
            raise TypeError("`value_fn` is not callable.")
        self._value_fn = value_fn

    def _get_dependents(self):
        dependents = self._extract_dependents(list(self.params.values()))
        return dependents

    @property
    def floating(self):
        raise LogicalUndefinedOperationError("Cannot be floating or not. Look at the dependents.")

    @property
    def params(self):
        return self._params

    def value(self):
        return tf.convert_to_tensor(self._value_fn(), dtype=self.dtype)

    def read_value(self):
        return tf.identity(self.value())

    def numpy(self):
        return self.value().numpy()

    @property
    def independent(self):
        return False


class ConstantParameter(OverloadableMixin, ZfitParameterMixin, BaseParameter):

    def __init__(self, name, value, dtype=ztypes.float):
        super().__init__(name=name, params={}, dtype=dtype)
        static_value = tf.get_static_value(value, partial=True)
        if static_value is None:
            raise RuntimeError("Cannot convert input to static value. If you encounter this, please open a bug report"
                               " on Github: https://github.com/zfit/zfit")
        self._value_np = static_value

        self._value = tf.guarantee_const(tf.convert_to_tensor(value, dtype=dtype))

    @property
    def shape(self):
        return self.value().shape

    def value(self) -> tf.Tensor:
        return self._value

    def read_value(self) -> tf.Tensor:
        return self.value()

    @property
    def floating(self):
        return False

    @property
    def independent(self) -> bool:
        return False

    def _get_dependents(self) -> ztyping.DependentsType:
        return OrderedSet()

    def __repr__(self):
        value = self._value_np
        return f"<zfit.param.{self.__class__.__name__} '{self.name}' dtype={self.dtype.name} value={value:.4g}>"


register_tensor_conversion(ConstantParameter, overload_operators=True)


class ComposedParameter(BaseComposedParameter):
    def __init__(self, name, value_fn, dependents, dtype=ztypes.float, **kwargs):  # TODO: automatize dependents
        dependents = convert_to_container(dependents)
        if dependents is None:
            params = OrderedSet()
        else:
            params = self._extract_dependents(dependents)
        params = {p.name: p for p in params}
        super().__init__(params=params, value_fn=value_fn, name=name, dtype=dtype, **kwargs)

    def __repr__(self):
        value = self.value()
        return f"<zfit.{self.__class__.__name__} '{self.name}' dtype={self.dtype.name} value={value:.4g}>"


class ComplexParameter(ComposedParameter):
    def __init__(self, name, value_fn, dependents, dtype=ztypes.complex, **kwargs):
        super().__init__(name, value_fn=value_fn, dependents=dependents, dtype=dtype, **kwargs)
        self._conj = None
        self._mod = None
        self._arg = None
        self._imag = None
        self._real = None

    @staticmethod
    def from_cartesian(name, real, imag, dtype=ztypes.complex, floating=True,
                       **kwargs):  # TODO: correct dtype handling, also below
        real = convert_to_parameter(real, name=name + "_real", prefer_constant=not floating)
        imag = convert_to_parameter(imag, name=name + "_imag", prefer_constant=not floating)
        param = ComplexParameter(name=name, value_fn=lambda: tf.cast(tf.complex(real, imag), dtype=dtype),
                                 dependents=[real, imag],
                                 **kwargs)
        param._real = real
        param._imag = imag
        return param

    @staticmethod
    def from_polar(name, mod, arg, dtype=ztypes.complex, floating=True, **kwargs):
        mod = convert_to_parameter(mod, name=name + "_mod", prefer_constant=not floating)
        arg = convert_to_parameter(arg, name=name + "_arg", prefer_constant=not floating)
        param = ComplexParameter(name=name,
                                 value_fn=lambda: tf.cast(tf.complex(mod * tf.math.cos(arg),
                                                                     mod * tf.math.sin(arg)),
                                                          dtype=dtype),
                                 dependents=[mod, arg],
                                 **kwargs)
        param._mod = mod
        param._arg = arg
        return param

    @property
    def conj(self):
        if self._conj is None:
            self._conj = ComplexParameter(name='{}_conj'.format(self.name), value_fn=lambda: tf.math.conj(self),
                                          dependents=self.get_dependents(),
                                          dtype=self.dtype)
        return self._conj

    @property
    def real(self):
        real = self._real
        if real is None:
            real = z.to_real(self)
        return real

    @property
    def imag(self):
        imag = self._imag
        if imag is None:
            imag = tf.math.imag(tf.convert_to_tensor(value=self, dtype_hint=self.dtype))  # HACK tf bug #30029
        return imag

    @property
    def mod(self):
        mod = self._mod
        if mod is None:
            mod = tf.math.abs(self)
        return mod

    @property
    def arg(self):
        arg = self._arg
        if arg is None:
            arg = tf.math.atan(self.imag / self.real)
        return arg


_auto_number = 0


def get_auto_number():
    global _auto_number
    auto_number = _auto_number
    _auto_number += 1
    return auto_number


def convert_to_parameter(value, name=None, prefer_constant=True, dependents=None) -> "ZfitParameter":
    """Convert a *numerical* to a constant/floating parameter or return if already a parameter.

    Args:
        value ():
        name ():
        prefer_constant: If True, create a ConstantParameter instead of a Parameter _if possible_.

    """
    is_python = False
    if name is not None:
        name = str(name)

    if callable(value):
        if dependents is None:
            raise ValueError("If the value is a callable, the dependents have to be specified as an empty list/tuple")
        return ComposedParameter(f"Composed_autoparam_{get_auto_number()}", value_fn=value, dependents=dependents)

    if isinstance(value, ZfitParameter):  # TODO(Mayou36): autoconvert variable. TF 2.0?
        return value
    elif isinstance(value, tf.Variable):
        raise TypeError("Currently, cannot autoconvert tf.Variable to zfit.Parameter.")

    # convert to Tensor
    if not isinstance(value, tf.Tensor):
        is_python = True
        if isinstance(value, complex):
            value = z.to_complex(value)
        else:
            value = z.to_real(value)

    if not run._enable_parameter_autoconversion:
        return value

    if value.dtype.is_complex:
        if name is None:
            name = "FIXED_complex_autoparam_" + str(get_auto_number())
        if prefer_constant:
            raise WorkInProgressError("Constant complex param not here yet, complex Mixin?")
        value = ComplexParameter(name, value_fn=value, floating=not prefer_constant)

    else:
        if prefer_constant:
            if name is None:
                name = "FIXED_autoparam_" + str(get_auto_number()) if name is None else name
            value = ConstantParameter(name, value=value)

        else:
            name = "autoparam_" + str(get_auto_number()) if name is None else name
            value = Parameter(name=name, value=value)

    return value


def set_values(params: Union[Parameter, Iterable[Parameter]],
               values: Union[ztyping.NumericalScalarType,
                             Iterable[ztyping.NumericalScalarType]]):
    """Set the values (using a context manager or not) of multiple parameters.

    Args:
        params: Parameters to set the values
        values: list-like object that supports indexing

    Returns:

    """
    params = convert_to_container(params)
    if len(params) > 1:
        if not tf.is_tensor(values) or isinstance(values, np.ndarray):
            values = convert_to_container(values)
            if not len(params) == len(values):
                raise ValueError(f"Incompatible length of parameters and values: {params}, {values}")
    if not all(param.independent for param in params):
        raise ParameterNotIndependentError(f'tryping to set parameters that are not independend '
                                           f'{[param for param in params if not param.independent]}')

    def setter(values):
        for i, param in enumerate(params):
            param.set_value(values[i])

    def getter():
        return [param.read_value() for param in params]

    return TemporarilySet(values, setter=setter, getter=getter)
