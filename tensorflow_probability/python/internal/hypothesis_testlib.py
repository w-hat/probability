# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Hypothesis strategies for TFP."""

import contextlib
import os
import re
import traceback
import unittest

# Dependency imports
import hypothesis as hp
from hypothesis.extra import numpy as hpnp
import hypothesis.strategies as hps
import numpy as np
import tensorflow.compat.v2 as tf

from tensorflow_probability.python.internal import dtype_util
from tensorflow_probability.python.internal import tensorshape_util
from tensorflow_probability.python.util.deferred_tensor import DeferredTensor


def randomize_hypothesis():
  # Use --test_env=TFP_RANDOMIZE_HYPOTHESIS=1 to get random coverage.
  # --test_env=TFP_HYPOTHESIS_RANDOMIZE=1 also works, so one needn't remember
  # the order.
  randomize_first = os.environ.get('TFP_RANDOMIZE_HYPOTHESIS', None)
  hypothesis_first = os.environ.get('TFP_HYPOTHESIS_RANDOMIZE', None)
  if randomize_first is None and hypothesis_first is None:
    return False
  if randomize_first is None:
    return bool(int(hypothesis_first))
  if hypothesis_first is None:
    return bool(int(randomize_first))
  if randomize_first == hypothesis_first:
    return bool(int(randomize_first))
  msg = ('Detected conflicting settings TFP_RANDOMIZE_HYPOTHESIS={} and '
         'TFP_HYPOTHESIS_RANDOMIZE={}.  Please set just one of these variables,'
         'or set them both to the same value.')
  raise ValueError(msg.format(randomize_first, hypothesis_first))


def hypothesis_max_examples(default=None):
  # Use --test_env=TFP_HYPOTHESIS_MAX_EXAMPLES=1000 to get fuller coverage.
  return int(os.environ.get('TFP_HYPOTHESIS_MAX_EXAMPLES', default or 20))


def hypothesis_timeout():
  # Use --test_env=TFP_HYPOTHESIS_TIMEOUT_SECS=600 to permit longer runs,
  # ergo deeper exploration of the search tree.
  return int(os.environ.get('TFP_HYPOTHESIS_TIMEOUT_SECS', 60))


def hypothesis_reproduction_seed():
  # Use --test_env=TFP_HYPOTHESIS_REPRODUCE=hexjunk to reproduce a failure.
  return os.environ.get('TFP_HYPOTHESIS_REPRODUCE', None)


def running_under_guitar():
  # The Guitar build sets --test_env=TFP_GUITAR=1
  return bool(int(os.environ.get('TFP_GUITAR', 0)))


def tfp_hp_settings(default_max_examples=None, **kwargs):
  """Default TFP-specific Hypothesis settings."""
  # Rationales for deviating from Hypothesis default settings
  # - Derandomize by default because flaky tests are horrible
  # - Turn off example database because
  #   - It makes tests flaky on our cluster even if derandomized at the current
  #     internal Hypothesis version.
  #   - In the future, derandomization will imply ignoring the database setting
  #     anyway
  #   - Having one can't make example runs any faster
  # - No deadline because our test functions are too slow
  # - No too_slow health check for the same reason
  # - Fewer examples by default for the same reason
  # - Always print `@reproduce_failure` blobs because one never doesn't want
  #   them in the logs
  kwds = dict(
      derandomize=not randomize_hypothesis(),
      database=None,
      deadline=None,
      suppress_health_check=[hp.HealthCheck.too_slow],
      max_examples=hypothesis_max_examples(default=default_max_examples),
      timeout=hypothesis_timeout(),
      print_blob=True)
  kwds.update(kwargs)
  def decorator(test_method):
    repro_seed = hypothesis_reproduction_seed()
    if repro_seed is not None:
      # This implements the semantics of TFP_HYPOTHESIS_REPRODUCE via
      # the `hp.reproduce_failure` decorator.
      # TODO(jburnim): Catch Hypothesis version mismatches.
      test_method = hp.reproduce_failure(hp.__version__,
                                         repro_seed)(test_method)
    elif randomize_hypothesis():
      # Hypothesis defaults to seeding its internal PRNG from the system time,
      # so since we actually want randomization (including across machines) we
      # have to force it.
      entropy = os.urandom(64)
      test_method = hp.seed(int.from_bytes(entropy, 'big'))(test_method)
    if hp.__version_info__ >= (4,):
      # TODO(jburnim): Comment this.
      timeout = kwds.pop('timeout')
      ret = hp.settings(**kwds)(test_method)
      object.__setattr__(ret._hypothesis_internal_use_settings,
                         'timeout', timeout)
    else:
      ret = hp.settings(**kwds)(test_method)
    return ret
  return decorator


def guitar_skip(reason):
  """Skips tests in the Guitar build.

  Why skip specifically in Guitar?  The Guitar build uses more compute to look
  for bad inputs than our regular test suite.  It is therefore common for a test
  to be failing in Guitar but passing on TAP.  This calls for disabling it in
  Guitar, to keep Guitar green, while leaving it enabled on TAP, to get its
  value as a presubmit.

  Args:
    reason: Python string.  The reason to skip, or a reference to the relevant
      bug.

  Returns nothing.

  Raises:
    SkipTest: If the test should be skipped.
  """
  if running_under_guitar():
    raise unittest.case.SkipTest(reason)


def guitar_skip_if_matches(pattern, name, reason):
  """Skips tests in the Guitar build if `name` matches `pattern`.

  This is an alternative to `guitar_skip` for parameterized tests, when the
  goal is to disable only some tests in a parameterized group.

  Args:
    pattern: Regex to apply to `name` to detect whether to skip.
    name: Python string giving a "name" for this test, e.g., the Bijector or
      Distribution being tested.  The test will be skipped if this matches the
      `pattern`.
    reason: Python string.  The reason to skip, or a reference to the relevant
      bug.

  Returns nothing.

  Raises:
    SkipTest: If the test should be skipped.
  """
  if running_under_guitar():
    if re.search(pattern, name):
      raise unittest.case.SkipTest(reason)


VAR_USAGES = {}


def usage_counting_identity(var):
  key = (id(var), var.name)
  VAR_USAGES[key] = VAR_USAGES.get(key, []) + [traceback.format_stack(limit=25)]
  return tf.identity(var)


def defer_and_count_usage(var):
  return DeferredTensor(var, usage_counting_identity)


@contextlib.contextmanager
def assert_no_excessive_var_usage(name, max_permissible=2):
  """Fails if a tagged DeferredTensor is convert_to_tensor'd too much.

  To set this up, wrap some Variables in `defer_and_count_usage`.  Then, if any
  of them is accessed more than `max_permissible` times in the wrapped block,
  this will signal an informative error.

  Args:
    name: Python `str` naming this var usage counter.
    max_permissible: Python `int` giving the maximum OK number of times
      each tagged DeferredTensor may be read.

  Yields:
    Nothing (it's a context manager).
  """
  VAR_USAGES.clear()
  yield
  # TODO(jvdillon): Reduce max_permissible to 1?
  var_nusages = {var_id_and_name: len(usages) for var_id_and_name,
                 usages in VAR_USAGES.items()}
  if any(len(usages) > max_permissible for usages in VAR_USAGES.values()):
    for (_, var_name), usages in VAR_USAGES.items():
      if len(usages) > max_permissible:
        print('While executing {}, saw {} Tensor conversions of {}:'.format(
            name, len(usages), var_name))
        for i, usage in enumerate(usages):
          print('Conversion {} of {}:\n{}'.format(i + 1, len(usages),
                                                  ''.join(usage)))
    raise AssertionError(
        'More than {} tensor conversions detected for {}: {}'.format(
            max_permissible, name, var_nusages))


class Support(object):
  """Classification of sample spaces and bijector domains and codomains."""
  SCALAR_UNCONSTRAINED = 'SCALAR_UNCONSTRAINED'
  SCALAR_NON_NEGATIVE = 'SCALAR_NON_NEGATIVE'
  SCALAR_NON_ZERO = 'SCALAR_NON_ZERO'
  SCALAR_POSITIVE = 'SCALAR_POSITIVE'
  SCALAR_GT_NEG1 = 'SCALAR_GT_NEG1'
  SCALAR_IN_NEG1_1 = 'SCALAR_IN_NEG1_1'
  SCALAR_IN_0_1 = 'SCALAR_IN_0_1'
  VECTOR_UNCONSTRAINED = 'VECTOR_UNCONSTRAINED'
  VECTOR_SIZE_TRIANGULAR = 'VECTOR_SIZE_TRIANGULAR'
  VECTOR_POSITIVE_WITH_L1_NORM_1_SIZE_GT1 = 'VECTOR_POSITIVE_WITH_L1_NORM_1_SIZE_GT1'
  VECTOR_STRICTLY_INCREASING = 'VECTOR_STRICTLY_INCREASING'
  MATRIX_UNCONSTRAINED = 'MATRIX_UNCONSTRAINED'
  MATRIX_LOWER_TRIL = 'MATRIX_LOWER_TRIL'
  MATRIX_LOWER_TRIL_POSITIVE_DEFINITE = 'MATRIX_LOWER_TRIL_POSITIVE_DEFINITE'
  MATRIX_POSITIVE_DEFINITE = 'MATRIX_POSITIVE_DEFINITE'
  CORRELATION_CHOLESKY = 'CORRELATION_CHOLESKY'
  OTHER = 'OTHER'

ALL_SUPPORTS = None


def all_supports():
  global ALL_SUPPORTS
  cls = Support
  ALL_SUPPORTS = [attr for attr in dir(cls)
                  if not callable(getattr(cls, attr))
                  and not attr.startswith('__')]
all_supports()
del all_supports


def _scalar_constrainer(support):
  """Helper for `constrainer` for scalar supports."""

  def nonzero(x):
    return tf.where(tf.equal(x, 0), 1e-6, x)

  constrainers = {
      Support.SCALAR_IN_0_1: tf.math.sigmoid,
      Support.SCALAR_GT_NEG1: softplus_plus_eps(-1 + 1e-6),
      Support.SCALAR_NON_ZERO: nonzero,
      Support.SCALAR_IN_NEG1_1: lambda x: tf.math.tanh(x) * (1 - 1e-6),
      Support.SCALAR_NON_NEGATIVE: tf.math.softplus,
      Support.SCALAR_POSITIVE: softplus_plus_eps(),
      Support.SCALAR_UNCONSTRAINED: tf.identity,
  }
  if support not in constrainers:
    raise NotImplementedError(support)
  return constrainers[support]


def _vector_constrainer(support):
  """Helper for `constrainer` for vector supports."""

  def l1norm(x):
    x = tf.concat([x, tf.ones_like(x[..., :1]) * 1e-6], axis=-1)
    x = x / tf.linalg.norm(x, ord=1, axis=-1, keepdims=True)
    return tf.math.abs(x)

  constrainers = {
      Support.VECTOR_UNCONSTRAINED:
          identity_fn,
      Support.VECTOR_STRICTLY_INCREASING:
          lambda x: tf.cumsum(tf.abs(x) + 1e-3, axis=-1),
      Support.VECTOR_POSITIVE_WITH_L1_NORM_1_SIZE_GT1:
          l1norm,
      Support.VECTOR_SIZE_TRIANGULAR:
          identity_fn,
  }
  if support not in constrainers:
    raise NotImplementedError(support)
  return constrainers[support]


def _matrix_constrainer(support):
  """Helper for `constrainer` for matrix supports."""
  constrainers = {
      Support.MATRIX_UNCONSTRAINED:
          identity_fn,
      Support.MATRIX_POSITIVE_DEFINITE:
          positive_definite,
      Support.MATRIX_LOWER_TRIL_POSITIVE_DEFINITE:
          lower_tril_positive_definite,
      Support.MATRIX_LOWER_TRIL:
          lower_tril,
  }
  if support not in constrainers:
    raise NotImplementedError(support)
  return constrainers[support]


def constrainer(support):
  """Determines a constraining transformation into the given support."""
  if support.startswith('SCALAR_'):
    return _scalar_constrainer(support)
  if support.startswith('VECTOR_'):
    return _vector_constrainer(support)
  if support.startswith('MATRIX_'):
    return _matrix_constrainer(support)
  raise NotImplementedError(support)


def min_rank_for_support(support):
  """Reports the minimum rank of a Tensor in the given support."""
  if support.startswith('SCALAR_'):
    return 0
  if support.startswith('VECTOR_'):
    return 1
  if support.startswith('MATRIX_'):
    return 2
  raise NotImplementedError(support)


def constrained_tensors(constraint_fn, shape, dtype=np.float32, elements=None):
  """Strategy for drawing a constrained Tensor.

  Args:
    constraint_fn: Function mapping the unconstrained space to the desired
      constrained space.
    shape: Shape of the desired Tensors as a Python list.
    dtype: Dtype for constrained Tensors.
    elements: Optional strategy for selecting array elements.

  Returns:
    tensors: A strategy for drawing constrained Tensors of the given shape.
  """
  # TODO(bjp): Allow a wider range of floats.
  # float32s = hps.floats(
  #     np.finfo(np.float32).min / 2, np.finfo(np.float32).max / 2,
  #     allow_nan=False, allow_infinity=False)
  if elements is None:
    if dtype_util.is_floating(dtype):
      width = np.dtype(dtype_util.as_numpy_dtype(dtype)).itemsize * 8
      elements = hps.floats(
          -200, 200, allow_nan=False, allow_infinity=False, width=width)
    elif dtype_util.is_bool(dtype):
      elements = hps.booleans()
    else:
      raise NotImplementedError(dtype)

  def mapper(x):
    x = constraint_fn(tf.convert_to_tensor(x, dtype_hint=dtype))
    if dtype_util.is_floating(x.dtype) and tf.executing_eagerly():
      # We'll skip this check in graph mode; too expensive.
      if not np.all(np.isfinite(np.array(x))):
        raise AssertionError('{} generated non-finite param value: {}'.format(
            constraint_fn, np.array(x)))
    return x

  return hpnp.arrays(dtype=dtype, shape=shape, elements=elements).map(mapper)


# pylint: disable=no-value-for-parameter


@hps.composite
def tensors_in_support(draw, support, batch_shape=None, event_dim=None,
                       dtype=np.float32):
  """Strategy for drawing Tensors in the given support.

  Supports have a notion of event shape, which is the trailing dimensions in
  which the support region may not be axis-aligned (e.g., the event ndims of
  `VECTOR_STRICTLY_INCREASING` is 1).  This strategy produces Tensors with at
  least the support's event rank, and also an optional batch shape.

  Args:
    draw: Hypothesis strategy sampler supplied by `@hps.composite`.
    support: The `Support` in which the Tensor should live.
    batch_shape: Optional shape.  The returned Tensors will have this batch
      shape.  Hypothesis will pick one if omitted.
    event_dim: Optional Python int giving the size of each event dimension.
      This is shared across all event dimensions, permitting square event
      matrices, etc. If omitted, Hypothesis will choose one.
    dtype: DType to use in generating tensor data.
      Default value: `np.float32`.

  Returns:
    tensors: A strategy for drawing such Tensors.
  """
  if event_dim is None:
    event_dim = draw(hps.integers(min_value=2, max_value=6))
  if batch_shape is None:
    batch_shape = tensorshape_util.as_list(draw(shapes()))
  shape = batch_shape + [event_dim] * min_rank_for_support(support)
  constraint_fn = constrainer(support)
  return draw(constrained_tensors(constraint_fn, shape, dtype=dtype))


@hps.composite
def shapes(draw, min_ndims=0, max_ndims=3, min_lastdimsize=1, max_side=None):
  """Strategy for drawing TensorShapes with some control over rank/dim sizes.

  Args:
    draw: Hypothesis strategy sampler supplied by `@hps.composite`.
    min_ndims: Python `int` giving the minimum rank.
    max_ndims: Python `int` giving the maximum rank.
    min_lastdimsize: Python `int`.  The trailing dimension will always be at
      least this large.  Ignored if the rank turns out to be 0.
    max_side: Python `int` giving the maximum size of each dimension

  Returns:
    shapes: A strategy for drawing fully-specified TensorShapes obeying
      these constraints.
  """
  rank = draw(hps.integers(min_value=min_ndims, max_value=max_ndims))
  shape = tf.TensorShape(None).with_rank(rank)
  if rank > 0:

    def resize_lastdim(x):
      return x[:-1] + (max(x[-1], min_lastdimsize),)

    if max_side is None:
      # Apparently we can't pass an explicit None to the Hypothesis strategy?
      shps = hpnp.array_shapes(min_dims=rank, max_dims=rank)
    else:
      shps = hpnp.array_shapes(min_dims=rank, max_dims=rank, max_side=max_side)
    shape = draw(shps.map(resize_lastdim).map(tf.TensorShape))
  return shape


def identity_fn(x):
  return x


@hps.composite
def broadcasting_params(draw,
                        batch_shape,
                        params_event_ndims,
                        event_dim=None,
                        enable_vars=False,
                        constraint_fn_for=lambda param: identity_fn,
                        mutex_params=(),
                        param_strategy_fn=None,
                        dtype=np.float32):
  """Streategy for drawing parameters which jointly have the given batch shape.

  Specifically, the batch shapes of the returned parameters will broadcast to
  the requested batch shape.

  The dtypes of the returned parameters are determined by their respective
  constraint functions.

  Args:
    draw: Hypothesis strategy sampler supplied by `@hps.composite`.
    batch_shape: A `TensorShape`.  The returned parameters' batch shapes will
      broadcast to this.
    params_event_ndims: Python `dict` mapping the name of each parameter to a
      Python `int` giving the event ndims for that parameter.
    event_dim: Optional Python int giving the size of each parameter's event
      dimensions (except where overridden by any applicable constraint
      functions).  This is shared across all parameters, permitting square event
      matrices, compatible location and scale Tensors, etc. If omitted,
      Hypothesis will choose one.
    enable_vars: TODO(bjp): Make this `True` all the time and put variable
      initialization in slicing_test. If `False`, the returned parameters are
      all `tf.Tensor`s and not {`tf.Variable`, `tfp.util.DeferredTensor`
      `tfp.util.TransformedVariable`}.
    constraint_fn_for: Python callable mapping parameter name to constraint
      function.  The latter is itself a Python callable which converts an
      unconstrained Tensor (currently with float32 values from -200 to +200)
      into one that meets the parameter's validity constraints.
    mutex_params: Python iterable of Python sets.  Each set gives a clique of
      mutually exclusive parameters (e.g., the 'probs' and 'logits' of a
      Categorical).  At most one parameter from each set will appear in the
      result.
    param_strategy_fn: Optional callable with signature
      `strategy = param_strategy_fn(shape, dtype, constraint_fn)`. If provided,
      overrides the default strategy for generating float-valued parameters.
      Default value: `constrained_tensors`.
    dtype: Dtype for generated parameters.

  Returns:
    params: A Hypothesis strategy for drawing Python `dict`s mapping parameter
      name to a `tf.Tensor`, `tf.Variable`, `tfp.util.DeferredTensor`, or
      `tfp.util.TransformedVariable`.  The batch shapes of the returned
      parameters broadcast together to the supplied `batch_shape`.  Only
      parameters whose names appear as keys in `params_event_ndims` will appear
      (but possibly not all of them, depending on `mutex_params`).
  """
  if event_dim is None:
    event_dim = draw(hps.integers(min_value=2, max_value=6))
  if param_strategy_fn is None:
    param_strategy_fn = constrained_tensors

  params_event_ndims = params_event_ndims or {}
  remaining_params = set(params_event_ndims.keys())
  params_to_use = []
  while remaining_params:
    param = draw(hps.sampled_from(sorted(remaining_params)))
    params_to_use.append(param)
    remaining_params.remove(param)
    for mutex_set in mutex_params:
      if param in mutex_set:
        remaining_params -= mutex_set

  param_batch_shapes = draw(
      broadcasting_named_shapes(batch_shape, params_to_use))
  params_kwargs = dict()
  for param in params_to_use:
    param_batch_shape = param_batch_shapes[param]
    param_event_rank = params_event_ndims[param]
    param_shape = (tensorshape_util.as_list(param_batch_shape) +
                   [event_dim] * param_event_rank)

    # Reduce our risk of exceeding TF kernel broadcast limits.
    hp.assume(len(param_shape) < 6)

    # TODO(axch): Can I replace `params_event_ndims` and `constraint_fn_for`
    # with a map from params to `Suppport`s, and use `tensors_in_support` here?
    param_strategy = param_strategy_fn(constraint_fn=constraint_fn_for(param),
                                       shape=param_shape,
                                       dtype=dtype)
    params_kwargs[param] = draw(maybe_variable(param_strategy,
                                               enable_vars=enable_vars,
                                               dtype=dtype,
                                               name=param))
  return params_kwargs


@hps.composite
def maybe_variable(draw,
                   strategy,
                   enable_vars=False,
                   dtype=None,
                   name=None):
  """Strategy for drawing objects that should sometimes be tf.Variables.

  Args:
    draw: Hypothesis strategy sampler supplied by `@hps.composite`.
    strategy: Hypothesis strategy for drawing suitable values
    enable_vars: TODO(bjp): Make this `True` all the time and put variable
      initialization in slicing_test. If `False`, the returned parameters are
      never {`tf.Variable`, `tfp.util.DeferredTensor`
      `tfp.util.TransformedVariable`}.
    dtype: Dtype for generated parameters.
    name: Name for the produced `Tensor`s and `Variable`s, if any.

  Returns:
    strategy: A Hypothesis strategy for drawing a value, `tf.Variable`,
      `tfp.util.DeferredTensor`, or `tfp.util.TransformedVariable`.  The
      `DeferredTensor`s are sometimes instrumented to count how many times they
      are concretized.
  """
  result = tf.convert_to_tensor(draw(strategy), dtype_hint=dtype, name=name)
  if enable_vars and draw(hps.booleans()):
    result = tf.Variable(result, name=name)
    if name is None:
      alt_name = None
    else:
      alt_name = '{}_alt_value'.format(name)
    alt_value = tf.convert_to_tensor(
        draw(strategy), dtype=result.dtype, name=alt_name)
    # This field provides an acceptable alternate value, to enable tests that
    # mutate the Variable (once).
    setattr(result, '_tfp_alt_value', alt_value)
    if draw(hps.booleans()):
      result = defer_and_count_usage(result)
  return result


@hps.composite
def broadcasting_named_shapes(draw, batch_shape, param_names):
  """Strategy for drawing a set of batch shapes that broadcast to `batch_shape`.

  For each parameter we need to choose its batch rank, and whether or not each
  axis i is 1 or batch_shape[i]. This function chooses a set of shapes that
  have possibly mismatched ranks, and possibly broadcasting axes, with the
  promise that the broadcast of the set of all shapes matches `batch_shape`.

  Args:
    draw: Hypothesis strategy sampler supplied by `@hps.composite`.
    batch_shape: `tf.TensorShape`, the target (fully-defined) batch shape.
    param_names: Iterable of `str`, the parameters whose batch shapes need
      determination.

  Returns:
    param_batch_shapes: A strategy for drawing `dict`s of `str->tf.TensorShape`
      where the set of shapes broadcast to `batch_shape`. The shapes are fully
      defined.
  """
  n = len(param_names)
  return dict(
      zip(draw(hps.permutations(param_names)),
          draw(broadcasting_shapes(batch_shape, n, no_error_n_eq_1=True))))


def _compute_rank_and_fullsize_reqd(draw, target_shape, current_shape, is_last):
  """Returns a param rank and a list of bools for full-size-required by axis.

  Args:
    draw: Hypothesis data sampler.
    target_shape: `tf.TensorShape`, the target broadcasted shape.
    current_shape: `tf.TensorShape`, the broadcasted shape of the shapes
      selected thus far. This is ignored for non-last shapes.
    is_last: bool indicator of whether this is the last shape (in which case, we
      must achieve the target shape).

  Returns:
    next_rank: Sampled rank for the next shape.
    force_fullsize_dim: `next_rank`-sized list of bool indicating whether the
      corresponding axis of the shape must be full-sized (True) or is allowed to
      be 1 (i.e., broadcast) (False).
  """
  target_rank = tensorshape_util.rank(target_shape)
  if is_last:
    # We must force full size dim on any mismatched axes, and proper rank.
    full_rank_current = tf.broadcast_static_shape(
        current_shape, tf.TensorShape([1] * target_rank))
    # Identify axes in which the target shape is not yet matched.
    full_rank_current_list = tensorshape_util.as_list(full_rank_current)
    target_shape_list = tensorshape_util.as_list(target_shape)
    axis_is_mismatched = [
        full_rank_current_list[i] !=
        target_shape_list[i] for i in range(target_rank)
    ]
    min_rank = target_rank
    if tensorshape_util.rank(current_shape) == target_rank:
      # Current rank might be already correct, but we could have a case like
      # batch_shape=[4,3,2] and current_batch_shape=[4,1,2], in which case
      # we must have at least 2 axes on this param's batch shape.
      min_rank -= (axis_is_mismatched + [True]).index(True)
    next_rank = draw(hps.integers(min_value=min_rank, max_value=target_rank))
    # Get the last param_batch_rank (possibly 0!) items.
    force_fullsize_dim = axis_is_mismatched[target_rank - next_rank:]
  else:
    # There are remaining params to be drawn, so we will be able to force full
    # size axes on subsequent params.
    next_rank = draw(hps.integers(min_value=0, max_value=target_rank))
    force_fullsize_dim = [False] * next_rank
  return next_rank, force_fullsize_dim


def broadcast_compatible_shape(shape):
  """Strategy for drawing shapes broadcast-compatible with `shape`."""
  # broadcasting_shapes draws a sequence of shapes, so that the last "completes"
  # the broadcast to fill out batch_shape. Here we just draw two and take the
  # first (incomplete) one.
  return broadcasting_shapes(shape, 2).map(lambda shapes: shapes[0])


@hps.composite
def broadcasting_shapes(draw, target_shape, n, no_error_n_eq_1=False):
  """Strategy for drawing a set of `n` shapes that broadcast to `target_shape`.

  For each shape we need to choose its rank, and whether or not each axis i is 1
  or target_shape[i]. This function chooses a set of `n` shapes that have
  possibly mismatched ranks, and possibly broadcasting axes, with the promise
  that the broadcast of the set of all shapes matches `target_shape`.

  Args:
    draw: Hypothesis strategy sampler supplied by `@hps.composite`.
    target_shape: The target (fully-defined) batch shape.
    n: Python `int`, the number of shapes to draw.
    no_error_n_eq_1: If True, don't raise ValueError when n==1.

  Returns:
    shapes: A strategy for drawing sequences of `tf.TensorShape` such that the
      set of shapes in each sequence broadcast to `target_shape`. The shapes are
      fully defined.
  """
  if n == 1 and not no_error_n_eq_1:
    raise ValueError('`broadcasting_shapes(shp, n=1) is just `shp`. '
                     'Did you want `broadcast_compatible_shape`? If `n` '
                     'is stochastic, add arg `no_error_n_eq_1=True`.')
  target_shape = tf.TensorShape(target_shape)
  target_rank = tensorshape_util.rank(target_shape)
  result = []
  current_shape = tf.TensorShape([])
  for is_last in [False] * (n - 1) + [True]:
    next_rank, force_fullsize_dim = _compute_rank_and_fullsize_reqd(
        draw, target_shape, current_shape, is_last=is_last)

    # Get the last next_rank (possibly 0!) dimensions.
    next_shape = tensorshape_util.as_list(
        target_shape[target_rank - next_rank:])
    for i, force_fullsize in enumerate(force_fullsize_dim):
      if not force_fullsize and draw(hps.booleans()):
        # Choose to make this param broadcast against some other param.
        next_shape[i] = 1
    next_shape = tf.TensorShape(next_shape)
    current_shape = tf.broadcast_static_shape(current_shape, next_shape)
    result.append(next_shape)
  return result


def _rank_broadcasting_error_pattern(left_rank, right_rank, op=None):
  ans = (r'Broadcast between \[([0-9]*,){' + str(left_rank - 1) + r',}[0-9]*\] '
         r'and \[([0-9]*,){' + str(right_rank - 1) + r',}[0-9]*\] '
         r'is not supported yet')
  if op is not None:
    ans += r'. \[' + op + r'\]'
  return ans


@contextlib.contextmanager
def no_tf_rank_errors():
  # TODO(axch): Instead of catching and `assume`ing away rank errors, could try
  # harder to avoid generating them in the first place.  For instance, could add
  # a parameter to `valid_slices` that limits how much the rank may increase
  # after the slice.  Trouble is, predicting what limits to set is difficult
  # because rank handling is non-uniform, and actually meeting them is also
  # non-trivial because in addition to the batch shape there is the event shape,
  # and at least one more dimention added by `sample`, and the parameters may
  # have larger "event" shapes than the distribution itself.
  input_dims_pat = r'Unhandled input dimensions (8|9|[1-9][0-9]+)'
  input_rank_pat = r'inputs rank not in \[0,([6-9]|[1-9][0-9]+)\]'
  is_not_implemented_pat = (r'is not implemented for tensors of '
                            r'rank > (8|9|[1-9][0-9]+).')
  does_not_work_pat = (r'does not work on tensors with '
                       r'more than (8|9|[1-9][0-9]+) dimensions')
  only_support_pat = r'only support up to 7 input dimensions'
  pat_1 = _rank_broadcasting_error_pattern(1, 6)
  pat_2 = _rank_broadcasting_error_pattern(6, 1)
  try:
    yield
  except tf.errors.UnimplementedError as e:
    # TODO(b/138385438): This really shouldn't be so complicated.
    # Bug requesting that TF increase the rank limit: b/137689241.
    # See also b/148230377.
    msg = str(e)
    if re.search(pat_1, msg) or re.search(pat_2, msg):
      # We asked some op to broadcast Tensors one of whose ranks >= 6.
      hp.assume(False)
    elif re.search(input_dims_pat, msg):
      # We asked some TF op (StridedSlice?) to operate on a Tensor of rank >= 8.
      hp.assume(False)
    elif re.search(input_rank_pat, msg):
      # We asked some TF op (PadV2?) to operate on a Tensor of rank >= 7.
      hp.assume(False)
    elif re.search(is_not_implemented_pat, msg):
      # We asked some different TF op (ReverseV2?) to operate on a Tensor of
      # rank >= 8.
      hp.assume(False)
    else:
      raise
  except tf.errors.InvalidArgumentError as e:
    msg = str(e)
    if re.search(only_support_pat, msg):
      # We asked some TF op (Argmin/Argmax/...) to operate on a Tensor of
      # rank >= 8.
      hp.assume(False)
    else:
      raise
  except ValueError as e:
    msg = str(e)
    if re.search(does_not_work_pat, msg):
      # We asked some yet different TF op (ReverseV2 in graph mode?) to operate
      # on a Tensor of rank >= 8.
      hp.assume(False)
    else:
      raise

# Slicing


@hps.composite
def valid_slices(draw, batch_shape):
  """Samples a legal (possibly empty) slice for shape batch_shape."""
  # We build up a list of slices in several stages:
  # 1. Choose 0 to batch_rank slices to come before an Ellipsis (...).
  # 2. Decide whether or not to add an Ellipsis; if using, updating the indexing
  #    used (e.g. batch_shape[i]) to identify safe bounds.
  # 3. Choose 0 to [remaining_dims] slices to come last.
  # 4. Decide where to insert between 0 and 3 newaxis slices.
  batch_shape = tf.TensorShape(batch_shape).as_list()
  slices = []
  batch_rank = len(batch_shape)
  arbitrary_slices = hps.tuples(
      hps.one_of(hps.just(None), hps.integers(min_value=-100, max_value=100)),
      hps.one_of(hps.just(None), hps.integers(min_value=-100, max_value=100)),
      hps.one_of(
          hps.just(None),
          hps.integers(min_value=-100, max_value=100).filter(lambda x: x != 0))
  ).map(lambda tup: slice(*tup))

  # 1. Choose 0 to batch_rank slices to come before an Ellipsis (...).
  nslc_before_ellipsis = draw(hps.integers(min_value=0, max_value=batch_rank))
  for i in range(nslc_before_ellipsis):
    slc = draw(
        hps.one_of(
            hps.integers(min_value=0, max_value=batch_shape[i] - 1),
            arbitrary_slices))
    slices.append(slc)
  # 2. Decide whether or not to add an Ellipsis; if using, updating the indexing
  #    used (e.g. batch_shape[i]) to identify safe bounds.
  has_ellipsis = draw(hps.booleans().map(lambda x: (Ellipsis, x)))[1]
  nslc_after_ellipsis = draw(
      hps.integers(min_value=0, max_value=batch_rank - nslc_before_ellipsis))
  if has_ellipsis:
    slices.append(Ellipsis)
    remain_start, remain_end = (batch_rank - nslc_after_ellipsis, batch_rank)
  else:
    remain_start = nslc_before_ellipsis
    remain_end = nslc_before_ellipsis + nslc_after_ellipsis
  # 3. Choose 0 to [remaining_dims] slices to come last.
  for i in range(remain_start, remain_end):
    slc = draw(
        hps.one_of(
            hps.integers(min_value=0, max_value=batch_shape[i] - 1),
            arbitrary_slices))
    slices.append(slc)
  # 4. Decide where to insert between 0 and 3 newaxis slices.
  newaxis_positions = draw(
      hps.lists(hps.integers(min_value=0, max_value=len(slices)), max_size=3))
  for i in sorted(newaxis_positions, reverse=True):
    slices.insert(i, tf.newaxis)
  slices = tuple(slices)
  # Since `d[0]` ==> `d.__getitem__(0)` instead of `d.__getitem__((0,))`;
  # and similarly `d[:3]` ==> `d.__getitem__(slice(None, 3))` instead of
  # `d.__getitem__((slice(None, 3),))`; it is useful to test such scenarios.
  if len(slices) == 1 and draw(hps.booleans()):
    # Sometimes only a single item non-tuple.
    return slices[0]
  return slices


def stringify_slices(slices):
  """Returns a list of strings describing the items in `slices`.

  Each returned string (in order) encodes what to do with one dimension of the
  slicee:

  - That number for a single integer slice;
  - 'a:b:c' for a start-stop-step slice, omitting any missing components;
  - 'tf.newaxis' for an axis insertion; or
  - The ellipsis '...' for an arbitrary-rank gap.

  Args:
    slices: A single-dimension slice or a Python tuple of single-dimension
      slices.

  Returns:
    pretty_slices: A list of Python strings encoding each slice.
  """
  pretty_slices = []
  slices = slices if isinstance(slices, tuple) else (slices,)
  for slc in slices:
    if slc == Ellipsis:
      pretty_slices.append('...')
    elif isinstance(slc, slice):
      pretty_slices.append('{}:{}:{}'.format(
          *['' if s is None else s for s in (slc.start, slc.stop, slc.step)]))
    elif isinstance(slc, int) or tf.is_tensor(slc):
      pretty_slices.append(str(slc))
    elif slc is tf.newaxis:
      pretty_slices.append('tf.newaxis')
    else:
      raise ValueError('Unexpected slice type: {}'.format(type(slc)))
  return pretty_slices


@contextlib.contextmanager
def no_cholesky_decomposition_errors():
  # Use this to suppress Cholesky errors if needed in tests where the
  # numerics are beside the point.
  pat = ('Cholesky decomposition was not successful. '
         'The input might not be valid. [Op:Cholesky]')
  try:
    yield
  except tf.errors.InvalidArgumentError as e:
    msg = str(e)
    if pat in msg:
      # Tried an input regime where a Cholesky decomposition failed
      # and crashed the program.
      hp.assume(False)
    else:
      raise


@contextlib.contextmanager
def finite_ground_truth_only():
  # Recognizing the error message from python/internal/numerics_testing.py
  pat = 'Cannot check accuracy if ground truth or derivatives are not finite'
  try:
    yield
  except tf.errors.InvalidArgumentError as e:
    msg = str(e)
    if re.search(pat, msg):
      # Tried an input regime where the 64-bit computation produced a
      # non-finite value or gradient
      hp.assume(False)
    else:
      raise


# Utility functions for constraining parameters and/or domain/codomain members.


def ensure_high_gt_low(low, high):
  """Returns a value with shape matching `high` and gt broadcastable `low`."""
  new_high = tf.maximum(low + tf.abs(low) * .1 + .1, high)
  reduce_dims = []
  if (tensorshape_util.rank(new_high.shape) >
      tensorshape_util.rank(high.shape)):
    reduced_leading_axes = tf.range(
        tensorshape_util.rank(new_high.shape) -
        tensorshape_util.rank(high.shape))
    new_high = tf.math.reduce_max(
        new_high, axis=reduced_leading_axes)
  reduce_dims = [
      d for d in range(tensorshape_util.rank(high.shape))
      if high.shape[d] < new_high.shape[d]
  ]
  if reduce_dims:
    new_high = tf.math.reduce_max(
        new_high, axis=reduce_dims, keepdims=True)
  return new_high


def softplus_plus_eps(eps=1e-6):
  return lambda x: tf.nn.softplus(x) + eps


def symmetric(x):
  return (x + tf.linalg.matrix_transpose(x)) / 2


def positive_definite(x):
  shp = tensorshape_util.as_list(x.shape)
  psd = (
      tf.matmul(x, x, transpose_b=True) +
      .1 * tf.linalg.eye(shp[-1], batch_shape=shp[:-2]))
  return symmetric(psd)


def orthonormal(x):
  return tf.linalg.qr(positive_definite(x)).q


def lower_tril_positive_definite(x):
  return tf.linalg.band_part(
      tf.linalg.set_diag(x, softplus_plus_eps(1e-1)(tf.linalg.diag_part(x))),
      num_lower=-1,
      num_upper=0)


def lower_tril(x):
  return tf.linalg.band_part(x, -1, 0)


def _monkey_patch_hypothesis_timeout():
  # pylint: disable=g-import-not-at-top
  import time
  from hypothesis.internal.conjecture.engine import ConjectureRunner
  # pylint: enable=g-import-not-at-top

  def _should_generate_more(self):
    # Inserted/new code for adding timeouts to Hypothesis 4.
    timeout = None
    try:
      timeout = object.__getattribute__(self.settings, 'timeout')
    except AttributeError:
      pass
    if timeout is not None:
      if not hasattr(self, 'start_time'):
        self.start_time = time.perf_counter()
      else:
        if (time.perf_counter() - self.start_time) > timeout:
          return False

    return self._original_should_generate_more()

  ConjectureRunner._original_should_generate_more = (
      ConjectureRunner.should_generate_more)
  ConjectureRunner.should_generate_more = _should_generate_more

if hp.__version_info__ >= (4,):
  # Monkey patch the timeout feature back in to Hypothesis.
  # (This feature was removed in Hypothesis 4.)
  _monkey_patch_hypothesis_timeout()
