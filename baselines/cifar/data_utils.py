# coding=utf-8
# Copyright 2021 The Uncertainty Baselines Authors.
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

"""Data utilities for CIFAR-10 and CIFAR-100."""

import functools

from absl import logging
import tensorflow as tf
import tensorflow_datasets as tfds
import tensorflow_probability as tfp
from uncertainty_baselines.datasets import augment_utils
from uncertainty_baselines.datasets import augmix
tfd = tfp.distributions


def normalize_convert_image(input_image, dtype):
  input_image = tf.image.convert_image_dtype(input_image, dtype)
  mean = tf.constant([0.4914, 0.4822, 0.4465])
  std = tf.constant([0.2023, 0.1994, 0.2010])
  return (input_image - mean) / std


def load_dataset(split,
                 batch_size,
                 name,
                 use_bfloat16,
                 normalize=True,
                 drop_remainder=True,
                 proportion=1.0,
                 validation_set=False,
                 validation_proportion=0.05,
                 aug_params=None):
  """Loads CIFAR dataset for training or testing.

  Args:
    split: tfds.Split.
    batch_size: The global batch size to use.
    name: A string indicates whether it is cifar10 or cifar100.
    use_bfloat16: data type, bfloat16 precision or float32.
    normalize: Whether to apply mean-std normalization on features.
    drop_remainder: bool.
    proportion: float, the proportion of dataset to be used.
    validation_set: bool, whether to split a validation set from training data.
    validation_proportion: float, the proportion of training dataset to be used
      as the validation split, if validation_set is set to True.
    aug_params: dict, data augmentation hyper parameters.

  Returns:
    Input function which returns a locally-sharded dataset batch.
  """
  if proportion < 0. or proportion > 1.:
    raise ValueError('proportion needs to lie in the range [0, 1]')
  if validation_proportion < 0. or validation_proportion > 1.:
    raise ValueError('validation_proportion needs to lie in the range [0, 1]')
  if use_bfloat16:
    dtype = tf.bfloat16
  else:
    dtype = tf.float32
  ds_info = tfds.builder(name).info
  image_shape = ds_info.features['image'].shape
  dataset_size = ds_info.splits['train'].num_examples
  num_classes = ds_info.features['label'].num_classes
  if aug_params is None:
    aug_params = {}
  adaptive_mixup = aug_params.get('adaptive_mixup', False)
  random_augment = aug_params.get('random_augment', False)
  mixup_alpha = aug_params.get('mixup_alpha', 0)
  ensemble_size = aug_params.get('ensemble_size', 1)
  label_smoothing = aug_params.get('label_smoothing', 0.)
  if adaptive_mixup and 'mixup_coeff' not in aug_params:
    # Hard target in the first epoch!
    aug_params['mixup_coeff'] = tf.ones([ensemble_size, num_classes])
  if mixup_alpha > 0 or label_smoothing > 0:
    onehot = True
  else:
    onehot = False

  def preprocess(image, label):
    """Image preprocessing function."""
    if split == tfds.Split.TRAIN:
      image = tf.image.resize_with_crop_or_pad(
          image, image_shape[0] + 4, image_shape[1] + 4)
      image = tf.image.random_crop(image, image_shape)
      image = tf.image.random_flip_left_right(image)

      # Only random augment for now.
      if random_augment:
        count = aug_params['aug_count']
        augmenter = augment_utils.RandAugment()
        augmented = [augmenter.distort(image) for _ in range(count)]
        image = tf.stack(augmented)

    if split == tfds.Split.TRAIN and aug_params['augmix']:
      augmenter = augment_utils.RandAugment()
      image = _augmix(image, aug_params, augmenter, dtype)
    elif normalize:
      image = normalize_convert_image(image, dtype)

    if split == tfds.Split.TRAIN and onehot:
      label = tf.cast(label, tf.int32)
      label = tf.one_hot(label, num_classes)
    else:
      label = tf.cast(label, dtype)
    return image, label

  if proportion == 1.0:
    if validation_set:
      new_name = '{}:3.*.*'.format(name)
      if split == 'validation':
        new_split = 'train[{}%:]'.format(
            int(100 * (1. - validation_proportion)))
        dataset = tfds.load(new_name, split=new_split, as_supervised=True)
      elif split == tfds.Split.TRAIN:
        new_split = 'train[:{}%]'.format(
            int(100 * (1. - validation_proportion)))
        dataset = tfds.load(name, split='train[:95%]', as_supervised=True)
      # split == tfds.Split.TEST case
      else:
        dataset = tfds.load(name, split=split, as_supervised=True)
    else:
      dataset = tfds.load(name, split=split, as_supervised=True)
  else:
    logging.warning(
        'Subset of training dataset is being used without a validation set.')
    new_name = '{}:3.*.*'.format(name)
    if split == tfds.Split.TRAIN:
      new_split = 'train[:{}%]'.format(int(100 * proportion))
    else:
      new_split = 'test[:{}%]'.format(int(100 * proportion))
    dataset = tfds.load(new_name, split=new_split, as_supervised=True)
  if split == tfds.Split.TRAIN:
    dataset = dataset.shuffle(buffer_size=dataset_size).repeat()

  dataset = dataset.map(preprocess,
                        num_parallel_calls=tf.data.experimental.AUTOTUNE)
  dataset = dataset.batch(batch_size, drop_remainder=drop_remainder)

  if mixup_alpha > 0 and split == tfds.Split.TRAIN:
    if adaptive_mixup:
      dataset = dataset.map(
          functools.partial(augmix.adaptive_mixup_aug, batch_size, aug_params),
          num_parallel_calls=8)
    else:
      dataset = dataset.map(
          functools.partial(augmix.mixup, batch_size, aug_params),
          num_parallel_calls=8)
  dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)
  return dataset


def augment_and_mix(image, depth, width, prob_coeff, augmenter, dtype):
  """Apply mixture of augmentations to image."""

  mix_weight = tf.squeeze(tfd.Beta([prob_coeff], [prob_coeff]).sample([1]))

  if width > 1:
    branch_weights = tf.squeeze(tfd.Dirichlet([prob_coeff] * width).sample([1]))
  else:
    branch_weights = tf.constant([1.])

  if depth < 0:
    depth = tf.random.uniform([width],
                              minval=1,
                              maxval=4,
                              dtype=tf.dtypes.int32)
  else:
    depth = tf.constant([depth] * width)

  mix = tf.cast(tf.zeros_like(image), tf.float32)
  for i in tf.range(width):
    branch_img = tf.identity(image)
    for _ in tf.range(depth[i]):
      branch_img = augmenter.distort(branch_img)
    branch_img = normalize_convert_image(branch_img, dtype)
    mix += branch_weights[i] * branch_img

  return mix_weight * mix + (
      1 - mix_weight) * normalize_convert_image(image, dtype)


def _augmix(image, params, augmenter, dtype):
  """Apply augmix augmentation to image."""
  depth = params['augmix_depth']
  width = params['augmix_width']
  prob_coeff = params['augmix_prob_coeff']
  count = params['aug_count']

  augmented = [
      augment_and_mix(image, depth, width, prob_coeff, augmenter, dtype)
      for _ in range(count)
  ]
  image = normalize_convert_image(image, dtype)
  return tf.stack([image] + augmented, 0)
