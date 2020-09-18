# Lint as: python3
# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
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
"""Input preprocessors tests."""

from lingvo import compat as tf
from lingvo.core import py_utils
from lingvo.core import test_utils
from lingvo.tasks.car import input_preprocessors
import numpy as np

FLAGS = tf.flags.FLAGS


class ConstantPreprocessor(input_preprocessors.Preprocessor):
  """Preprocessor that produces a specified constant value under p.key_name."""

  @classmethod
  def Params(cls):
    p = super().Params()
    p.Define('constant_value', 0, 'Value to output')
    p.Define('key_name', 'value', 'Key under which value is output.')
    return p

  def TransformFeatures(self, features):
    features[self.params.key_name] = tf.constant(self.params.constant_value)
    return features

  def TransformShapes(self, shapes):
    shapes[self.params.key_name] = tf.TensorShape(
        np.array(self.params.constant_value).shape)
    return shapes

  def TransformDTypes(self, dtypes):
    dtypes[self.params.key_name] = tf.as_dtype(
        np.array(self.params.constant_value).dtype)
    return dtypes


class InputPreprocessorsTest(test_utils.TestCase):

  def testRandomChoicePreprocessor(self):
    p = input_preprocessors.RandomChoicePreprocessor.Params()
    p.weight_tensor_key = 'weights'
    # Construct 4 preprocessors each producing a different value.
    p.subprocessors = [
        ConstantPreprocessor.Params().Set(constant_value=1),
        ConstantPreprocessor.Params().Set(constant_value=2),
        ConstantPreprocessor.Params().Set(constant_value=3),
        ConstantPreprocessor.Params().Set(constant_value=4)
    ]

    preprocessor = p.Instantiate()

    # Construct test data.
    features = py_utils.NestedMap()
    features.weights = tf.constant([1., 2., 3., 4.])
    shapes = py_utils.NestedMap()
    shapes.weights = tf.TensorShape([4])
    dtypes = py_utils.NestedMap()
    dtypes.weights = tf.float32

    # Verify shape / dtypes.
    new_shapes = preprocessor.TransformShapes(shapes)
    new_dtypes = preprocessor.TransformDTypes(dtypes)
    self.assertEqual(new_shapes.value, tf.TensorShape([]))
    self.assertEqual(new_dtypes.value, tf.int64)

    new_features = preprocessor.TransformFeatures(features)

    counts = [0, 0, 0, 0]
    with self.session() as sess:
      # Run 10000 times to get probability distribution.
      for _ in range(10000):
        new_features_np = sess.run(new_features)
        counts[new_features_np.value - 1] += 1

      # Check distribution roughly matches [0.1, 0.2, 0.3, 0.4]
      self.assertTrue(counts[0] > 800 and counts[0] < 1200)
      self.assertTrue(counts[1] > 1800 and counts[1] < 2200)
      self.assertTrue(counts[2] > 2800 and counts[2] < 3200)
      self.assertTrue(counts[3] > 3800 and counts[3] < 4200)

  def testRandomChoicePreprocessorErrors(self):
    p = input_preprocessors.RandomChoicePreprocessor.Params()
    p.weight_tensor_key = 'weights'
    # Subprocessors produce different shapes
    p.subprocessors = [
        ConstantPreprocessor.Params().Set(constant_value=1),
        ConstantPreprocessor.Params().Set(constant_value=[2, 3]),
    ]
    preprocessor = p.Instantiate()
    # Construct test data.
    shapes = py_utils.NestedMap()
    shapes.weights = tf.TensorShape([2])
    with self.assertRaises(ValueError):
      preprocessor.TransformShapes(shapes)

    # Subprocessors produce different keys
    p.subprocessors = [
        ConstantPreprocessor.Params().Set(constant_value=1, key_name='foo'),
        ConstantPreprocessor.Params().Set(constant_value=2),
    ]
    preprocessor = p.Instantiate()
    # Construct test data.
    shapes = py_utils.NestedMap()
    shapes.weights = tf.TensorShape([2])
    with self.assertRaises(ValueError):
      preprocessor.TransformShapes(shapes)

    # Subprocessors produce different dtypes
    p.subprocessors = [
        ConstantPreprocessor.Params().Set(constant_value=1),
        ConstantPreprocessor.Params().Set(constant_value=2.),
    ]
    preprocessor = p.Instantiate()
    # Construct test data.
    dtypes = py_utils.NestedMap()
    dtypes.weights = tf.float32
    with self.assertRaises(ValueError):
      preprocessor.TransformDTypes(dtypes)


if __name__ == '__main__':
  tf.test.main()
