# Lint as: python3
# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""TPU embedding layers."""

import math
import lingvo.compat as tf
from lingvo.core import base_layer
from lingvo.core import py_utils

# pylint:disable=g-direct-tensorflow-import
from tensorflow.python.tpu import tpu_embedding as tpu_embedding_lib
# pylint:enable=g-direct-tensorflow-import


def _AddTpuEmbeddingSummaryTensor(name, value, weight=1.0):
  tf.add_to_collection(py_utils.TPU_EMBEDDING_SUMMARY_TENSORS,
                       (name, value, tf.convert_to_tensor(weight)))


class _TPUEmbeddingOptimizer(base_layer.BaseLayer):
  """Base class for TPUEmbeddingLayer, TPUEmbeddingTable optimizers."""

  @classmethod
  def Params(cls):
    p = super().Params()
    p.Define('learning_rate', None, 'Used for updating embedding table.')
    p.Define('clip_weight_min', None,
             'The minimum value to clip by; None means -infinity.')
    p.Define('clip_weight_max', None,
             'The maximum value to clip by; None means +infinity.')
    p.Define(
        'weight_decay_factor', None,
        'Amount of weight decay to apply; None means that the weights are not '
        'decayed.')
    p.Define(
        'multiply_weight_decay_factor_by_learning_rate', None,
        'If true, weight_decay_factor is multiplied by the current learning '
        'rate.')
    return p

  def __init__(self, params):
    super().__init__(params)
    p = self.params
    assert p.name

  @property
  def tpu_embedding_optimizer_parameters(self):
    return self._tpu_embedding_optimizer_parameters

  def CreateSlotVariablesAndOps(self, table_vars, tpu_embedding_table):
    """Create slot variables and infeed/retrieval ops.

    Args:
      table_vars: A list of all embedding table shard variables.
      tpu_embedding_table: Parent TPUEmbeddingTable layer.

    Returns:
      List of load ops
      List of retrieve ops
    """
    return NotImplementedError()


class TPUEmbeddingSGDOptimizer(_TPUEmbeddingOptimizer):
  """SGD optimizer for TPUEmbeddingLayer, TPUEmbeddingTable."""

  def __init__(self, params):
    super().__init__(params)
    p = self.params
    self._tpu_embedding_optimizer_parameters = (
        tpu_embedding_lib.StochasticGradientDescentParameters(
            learning_rate=p.learning_rate,
            clip_weight_min=p.clip_weight_min,
            clip_weight_max=p.clip_weight_max,
            weight_decay_factor=p.weight_decay_factor,
            multiply_weight_decay_factor_by_learning_rate=p
            .multiply_weight_decay_factor_by_learning_rate))

  def CreateSlotVariablesAndOps(self, table_vars, tpu_embedding_table):
    load_op_list = []
    retrieve_op_list = []

    num_tpu_hosts = tpu_embedding_table.params.num_tpu_hosts
    table_name = tpu_embedding_table.table_name

    for host_id, table_var in zip(range(num_tpu_hosts), table_vars):
      # The slot vars should be on the same device as the table var.
      device_name = tpu_embedding_table.GetDeviceName(host_id)
      with tf.device(device_name), py_utils.outside_all_rewrites():
        # Only the Trainer needs these ops.
        if py_utils.use_tpu():
          # TPU Embedding load/retrieve ops need to be in the outer graph scope.
          with tf.init_scope():
            tf.logging.info('creating load and retrieve ops.')
            load_parameters_op = (
                tpu_embedding_lib.tpu_ops
                .load_tpu_embedding_stochastic_gradient_descent_parameters(
                    parameters=table_var,
                    table_name=table_name,
                    num_shards=num_tpu_hosts,
                    shard_id=host_id))
            load_op_list.append(load_parameters_op)

            retrieved_table = (
                tpu_embedding_lib.tpu_ops
                .retrieve_tpu_embedding_stochastic_gradient_descent_parameters(
                    table_name=table_name,
                    num_shards=num_tpu_hosts,
                    shard_id=host_id))
            retrieve_parameters_op = tpu_embedding_lib.control_flow_ops.group(
                tf.assign(table_var, retrieved_table))
            retrieve_op_list.append(retrieve_parameters_op)

    return load_op_list, retrieve_op_list


class TPUEmbeddingAdagradOptimizer(_TPUEmbeddingOptimizer):
  """Adagrad optimizer for TPUEmbeddingLayer, TPUEmbeddingTable."""

  @classmethod
  def Params(cls):
    p = super().Params()
    p.Define('initial_accumulator', 0.1,
             'Initial value of Adagrad accumulator.')
    p.Define(
        'use_gradient_accumulation', True,
        'Setting this to False makes embedding gradients calculation less '
        'accurate but faster. See tpu_embedding_lib for more details.')
    return p

  def __init__(self, params):
    super().__init__(params)
    p = self.params
    self._tpu_embedding_optimizer_parameters = (
        tpu_embedding_lib.AdagradParameters(
            learning_rate=p.learning_rate,
            initial_accumulator=p.initial_accumulator,
            clip_weight_min=p.clip_weight_min,
            clip_weight_max=p.clip_weight_max,
            weight_decay_factor=p.weight_decay_factor,
            multiply_weight_decay_factor_by_learning_rate=p
            .multiply_weight_decay_factor_by_learning_rate))

  def CreateSlotVariablesAndOps(self, table_vars, tpu_embedding_table):
    p = self.params

    load_op_list = []
    retrieve_op_list = []

    num_tpu_hosts = tpu_embedding_table.params.num_tpu_hosts
    table_name = tpu_embedding_table.table_name
    slot_var_collections = [tpu_embedding_table.__class__.__name__ + '_vars']

    for host_id, table_var in zip(range(num_tpu_hosts), table_vars):
      # The slot vars should be on the same device as the table var.
      device_name = tpu_embedding_table.GetDeviceName(host_id)
      with tf.device(device_name), py_utils.outside_all_rewrites():
        w_ada = py_utils.WeightParams(
            shape=table_var.shape.as_list(),
            init=py_utils.WeightInit.Constant(p.initial_accumulator),
            dtype=p.dtype,
            collections=slot_var_collections)
        var_name = tpu_embedding_table.GetVariableName(host_id)
        tpu_embedding_table.CreateVariable(
            '%s/Adagrad' % var_name, w_ada, trainable=False)
        accumulator_var = tpu_embedding_table.vars['%s/Adagrad' % var_name]

        # Only the Trainer needs these ops.
        if py_utils.use_tpu():
          # TPU Embedding load/retrieve ops need to be in the outer graph scope.
          with tf.init_scope():
            tf.logging.info('creating load and retrieve ops.')
            load_parameters_op = (
                tpu_embedding_lib.tpu_ops.load_tpu_embedding_adagrad_parameters(
                    parameters=table_var,
                    accumulators=accumulator_var,
                    table_name=table_name,
                    num_shards=num_tpu_hosts,
                    shard_id=host_id))
            load_op_list.append(load_parameters_op)

            retrieved_table, retrieved_accumulator = (
                tpu_embedding_lib.tpu_ops
                .retrieve_tpu_embedding_adagrad_parameters(
                    table_name=table_name,
                    num_shards=num_tpu_hosts,
                    shard_id=host_id))
            retrieve_parameters_op = tpu_embedding_lib.control_flow_ops.group(
                tf.assign(table_var, retrieved_table),
                tf.assign(accumulator_var, retrieved_accumulator))
            retrieve_op_list.append(retrieve_parameters_op)

    return load_op_list, retrieve_op_list


class TPUEmbeddingTable(base_layer.BaseLayer):
  """An embedding table controlled by TPUEmbeddingLayer.

  Note that all input_keys needs to be declared upfront.
  """

  @classmethod
  def Params(cls):
    p = super().Params()
    p.Define('vocab_size', 0, 'Depth of the input.')
    p.Define('embedding_dim', 0, 'Depth of the output.')
    p.Define('input_keys', None, 'Name of inputs in InputBatch.')
    p.Define(
        'combiner', 'mean',
        'Must be "sum", "sqrtn", "mean" or None in the case of a '
        '"sequence embedding "')
    p.Define(
        'max_sequence_length', None,
        'If not None or 0, embedding lookup will return a '
        '"sequence embedding" of shape '
        '`[batch, max_sequence_length, embedding_dim]` without applying a '
        'sequence  reducing combiner')
    p.Define('num_tpu_hosts', 0, 'Total number of TPU hosts.')
    p.Define(
        'optimizer', None,
        'Table optimizer parameters. Will override the optimizer parameters '
        'defined in this table\'s TPUEmbeddingLayer.')
    p.Define(
        'learning_rate', None, 'Static learning rate for this table. If '
        'learning_rate and lr_schedule are both `None`, static learning '
        'rate as specified in local optimization_parameters will be used. '
        'In case local optimization_parameters is None, TPUEmbeddingLayer '
        'optimization_parameters will be used. lr_schedule must be None '
        'if learning_rate is not None.')
    p.Define(
        'lr_schedule', None, 'Use dynamic learning rate given by a Lingvo '
        'lr schedule. If learning_rate and lr_schedule are both '
        'None, static learning rate as specified in '
        'optimization_parameters is used. learning_rate must be None if '
        'lr_schedule is not None.')
    return p

  def __init__(self, params):
    super().__init__(params)
    p = self.params
    assert p.vocab_size > 0
    assert p.embedding_dim > 0
    assert p.input_keys
    assert p.name
    assert p.num_tpu_hosts > 0
    if p.combiner is None:
      assert p.max_sequence_length
    if p.max_sequence_length is not None and p.max_sequence_length > 0:
      assert p.combiner is None

    self._ids_per_shard = int(math.ceil(float(p.vocab_size) / p.num_tpu_hosts))
    self._padded_vocab_size = self._ids_per_shard * p.num_tpu_hosts
    self._input_keys = p.input_keys

    self._max_sequence_length = 0
    if p.max_sequence_length:
      self._max_sequence_length = p.max_sequence_length

    self.CreateChild('optimizer', p.optimizer)

    def GetLearningRateFn():
      if p.lr_schedule is None:
        return None
      else:
        self.CreateChild('schedule', p.lr_schedule)

        def LearningRateFn(step):
          lr = self.schedule.Value(step)
          _AddTpuEmbeddingSummaryTensor('tpu_embedding_lr/{}'.format(p.name),
                                        lr)
          return lr

        return LearningRateFn

    self._table_name = '{}_table'.format(p.name)
    self._table_config = tpu_embedding_lib.TableConfig(
        self._padded_vocab_size,
        p.embedding_dim,
        combiner=p.combiner,
        learning_rate=p.learning_rate,
        learning_rate_fn=GetLearningRateFn(),
        optimization_parameters=self.optimizer
        .tpu_embedding_optimizer_parameters)

    self._load_op_list = []
    self._retrieve_op_list = []

  def _CreateLayerVariables(self):
    p = self.params
    w_pc = py_utils.WeightParams(
        shape=[self._ids_per_shard, p.embedding_dim],
        init=p.params_init,
        dtype=p.dtype,
        collections=[self.__class__.__name__ + '_vars'])

    embedding_table_vars = []
    for i in range(p.num_tpu_hosts):
      device_name = self.GetDeviceName(i)
      with tf.device(device_name), py_utils.outside_all_rewrites():
        var_name = self.GetVariableName(i)
        self.CreateVariable(var_name, w_pc)
        embedding_var = self.vars[var_name]
        embedding_table_vars.append(embedding_var)
        # Remove from _private_vars / _private_thetas to be added later as wm.
        del self._private_vars[var_name]
        del self._private_theta[var_name]

    if not py_utils.use_tpu():
      # We don't want to add this for TrainerTpu, otherwise the identity
      # reference leads to copying the embedding to the TPU for no reason.
      # However, this is needed for CPU (eval/decode/controller).
      self._private_vars['wm'] = embedding_table_vars
      self._private_theta['wm'] = [tf.identity(v) for v in embedding_table_vars]

    # Only trainer and controller need slot variables and load/retrieve ops.
    if not self.do_eval:
      self._load_op_list, self._retrieve_op_list = (
          self.optimizer.CreateSlotVariablesAndOps(embedding_table_vars, self))

  # Return device to place sharded variables on.
  def GetDeviceName(self, host_id):
    if self.do_eval:
      return None
    else:
      return '{}/replica:0/task:{}/device:CPU:0'.format(
          self.cluster.params.worker.name, host_id)

  # Return variable name for embedding table shards.
  def GetVariableName(self, host_id):
    return 'var_%d' % host_id

  @property
  def table_config(self):
    return self._table_config

  @property
  def table_name(self):
    return self._table_name

  @property
  def retrieve_op_list(self):
    return self._retrieve_op_list

  @property
  def load_op_list(self):
    return self._load_op_list

  @property
  def input_keys(self):
    return self._input_keys

  @property
  def max_sequence_length(self):
    return self._max_sequence_length

  def CpuEmbLookup(self, ids_map):
    """CPU evaluation embedding lookup.

    Args:
      ids_map: A dict of `input_key` string -> [batch, sequence] int32 Tensor.
        -1 is used as a padding id.

    Returns:
      An activations dict of string -> float32 Tensor.
      For non-sequence embeddings: [batch, 1, embedding_dim]
      For sequence embeddings: [batch, max_sequence_length, embedding_dim]

    """
    p = self.params
    rets = py_utils.NestedMap()
    if self.max_sequence_length > 0:
      # "Sequence embedding", no combiner case
      for k, ids in ids_map.items():
        embs = tf.nn.embedding_lookup(self.theta.wm, tf.reshape(ids, [-1]))
        out_shape = tf.concat([tf.shape(ids), [p.embedding_dim]], 0)
        rets[k] = tf.reshape(embs, out_shape)
    else:
      # Non-"Sequence embedding", combiner case
      for k, ids in ids_map.items():
        # Dense to sparse.
        dense_shape = tf.shape(ids, out_type=tf.int64)
        sample_indices = tf.cast(tf.where(tf.not_equal(ids, -1)), tf.int64)
        embedding_indices = tf.cast(tf.gather_nd(ids, sample_indices), tf.int64)
        sparse_ids = tf.SparseTensor(
            indices=sample_indices,
            values=embedding_indices,
            dense_shape=dense_shape)
        # [?, embedding_dim]
        # For tf.nn.embedding_lookup_sparse, output.dim0 might be different from
        # sparse_ids.dense_shape.dim0.
        # In fact, the '?' is the smallest span starting from the index=0 that
        # covers all the results.
        embs = tf.nn.embedding_lookup_sparse(
            self.theta.wm,
            sparse_ids,
            None,  # sp_weights
            combiner=p.combiner)
        batch_size = dense_shape[0]
        # Explicitly pad results to maintain dim0=batch.
        dim0_padlen = tf.cast(batch_size, tf.int32) - tf.shape(embs)[0]
        embs = tf.pad(embs, [[0, dim0_padlen], [0, 0]])
        # [batch, 1, embedding_dim]
        embs = py_utils.HasShape(embs, [batch_size], ndims=1)
        rets[k] = tf.expand_dims(embs, 1)
    return rets


class TPUEmbeddingLayer(base_layer.BaseLayer):
  """Monolithic interface to TPU embedding.

  This layer has some important caveats, due to the interface of the
  TPU embedding hardware. Its behavior most closely mimics that of
  tf.nn.embedding_lookup_sparse.

  Supports multiple tables and multiple input_keys per table.
  Requires its own optimizer parameters.
  """

  @classmethod
  def Params(cls):
    p = super().Params()
    p.Define('tables', None, 'TPUEmbeddingTables')
    p.Define('pipeline_execution_with_tensor_core', False,
             'Set to True to be faster. See tpu_embedding.py for details.')
    p.Define('batch_size', 0, 'Per-core batch size.')
    p.Define(
        'optimizer', TPUEmbeddingAdagradOptimizer.Params(),
        'Layer optimizer parameters. Will be used for any TPUEmbeddingTables '
        'with None optimizer parameters.')
    return p

  def __init__(self, params):
    super().__init__(params)
    p = self.params

    assert p.tables
    assert p.batch_size > 0
    assert p.name

    num_tpu_hosts = p.tables[0].num_tpu_hosts
    assert all([t.num_tpu_hosts == num_tpu_hosts for t in p.tables])

    # Stop if a table has no optimizer parameters and the layer also has no
    # optimizer parameters
    table_optimizer_missing = any(
        table_params.optimizer is None for table_params in p.tables)
    if not p.optimizer and table_optimizer_missing:
      raise ValueError(
          'A table is missing optimizer parameters, and no layer-level '
          'optimizer parameters were given.')
    elif table_optimizer_missing:
      for table_params in p.tables:
        if table_params.optimizer is None:
          table_params.optimizer = p.optimizer.Copy()

    self.CreateChildren('tables', p.tables)

  def _CreateChildrenVariables(self):
    # Backwards compatibility: manually call child.InstantiateVariables()
    # outside of tf.variable_scope(p.name).
    for table in self.tables:
      table.InstantiateVariables()
    super()._CreateChildrenVariables()

  def _CreateLayerVariables(self):
    super()._CreateLayerVariables()

    load_op_list = []
    retrieve_op_list = []

    # At the feature level, track which are associated
    # with "sequence embeddings".
    self._sequence_features = {}

    if py_utils.use_tpu():
      num_cores = self.cluster.params.worker.tpus_per_replica
      global_batch_size = (
          self.params.batch_size * self.cluster.num_splits_per_client)
      table_to_config_dict = {}
      feature_to_config_dict = {}
      for table in self.tables:
        table_to_config_dict[table.table_name] = table.table_config
        load_op_list += table.load_op_list
        retrieve_op_list += table.retrieve_op_list
        for feature in table.input_keys:
          if table.max_sequence_length > 0:
            self._sequence_features[feature] = True
          feature_to_config_dict[feature] = tpu_embedding_lib.FeatureConfig(
              table.table_name, max_sequence_length=table.max_sequence_length)
      tf.logging.info('adding load and retrieve ops to collection.')
      tf.add_to_collection(py_utils.TPU_EMBEDDING_LOAD_OPS, load_op_list)
      tf.add_to_collection(py_utils.TPU_EMBEDDING_RETRIEVE_OPS,
                           retrieve_op_list)

      tpu_embedding_collection = tf.get_collection(py_utils.TPU_EMBEDDING)
      assert len(tpu_embedding_collection) <= 1
      if len(tpu_embedding_collection) == 1:
        tf.logging.info('TPUEmbedding API singleton already exists, reusing')
        self._tpu_embedding = tpu_embedding_collection[0]
      else:
        mode = tpu_embedding_lib.TRAINING
        device_config = tpu_embedding_lib.DeviceConfig(
            num_cores=num_cores,
            num_hosts=self.params.tables[0].num_tpu_hosts,
            job_name=self.cluster.params.worker.name)
        self._tpu_embedding = tpu_embedding_lib.TPUEmbedding(
            table_to_config_dict,
            feature_to_config_dict,
            global_batch_size,
            mode,
            master=None,
            pipeline_execution_with_tensor_core=(
                self.params.pipeline_execution_with_tensor_core),
            device_config=device_config)
        tf.add_to_collection(py_utils.TPU_EMBEDDING, self._tpu_embedding)

  def EmbLookup(self, ids_map):
    """Looks up embedding vectors for each entry in ids_map.

    Since the TPUEmbedding is monolothic, and consulted once per
    FProp/BPRop, we must centralize the lookup. Thus, for multiple
    features, we contain them into a single-lookup rather than allowing
    the caller to call Lookup multiple times.

    Currently, there's also an implied combination step which combines
    the sequence into a single set of activations by sum, mean or
    sqrtn.

    Args:
      ids_map: A dict of `input_key` string -> [batch, sequence] int32 Tensor.
        -1 is used as a padding id.

    Returns:
      Activations dict of string ->
      For non-sequence embeddings:  [batch, 1, embedding_dim],
      For sequence embeddings: [batch, max_sequence_length, embedding_dim]
      float32 Tensor.
    """

    def TpuEmbLookup(ids_map):
      """TPU Embedding lookup."""
      del ids_map
      activations = self._tpu_embedding.get_activations()
      tf.add_to_collection(py_utils.TPU_EMBEDDING_ACTIVATIONS, activations)
      ret = py_utils.NestedMap()
      for k, v in activations.items():
        if k in self._sequence_features:
          ret[k] = v
        else:
          # Non-sequence embeddings, we fill the "time" dimension with 1.
          ret[k] = tf.expand_dims(v, axis=[1])
      return ret

    def CpuEmbLookup(ids_map):
      """CPU evaluation embedding lookup."""
      rets = py_utils.NestedMap()
      for table in self.tables:
        table_id_map = {}
        for key in table.input_keys:
          table_id_map[key] = ids_map[key]
        table_rets = table.CpuEmbLookup(table_id_map)
        # Merge table_rets with rets
        for k, v in table_rets.items():
          rets[k] = v
      return rets

    if not py_utils.use_tpu():
      return CpuEmbLookup(ids_map)
    else:
      return TpuEmbLookup(ids_map)
