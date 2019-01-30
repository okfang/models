# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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
"""tf.data.Dataset builder.

Creates data sources for DetectionModels from an InputReader config. See
input_reader.proto for options.

Note: If users wishes to also use their own InputReaders with the Object
Detection configuration framework, they should define their own builder function
that wraps the build function.
"""
import functools
import tensorflow as tf

from object_detection.data_decoders import tf_example_decoder
from object_detection.protos import input_reader_pb2


def make_initializable_iterator(dataset):
  """Creates an iterator, and initializes tables.

  This is useful in cases where make_one_shot_iterator wouldn't work because
  the graph contains a hash table that needs to be initialized.

  Args:
    dataset: A `tf.data.Dataset` object.

  Returns:
    A `tf.data.Iterator`.
  """
  iterator = dataset.make_initializable_iterator()
  tf.add_to_collection(tf.GraphKeys.TABLE_INITIALIZERS, iterator.initializer)
  return iterator


def read_dataset(file_read_func, input_files, config):
  """Reads a dataset, and handles repetition and shuffling.

  Args:
    file_read_func: Function to use in tf.contrib.data.parallel_interleave, to
      read every individual file into a tf.data.Dataset.
    input_files: A list of file paths to read.
    config: A input_reader_builder.InputReader object.

  Returns:
    A tf.data.Dataset of (undecoded) tf-records based on config.
  """
  # Shard, shuffle, and read files.
  # ********************************************1.根据输入字符串模式，找到所有的输入
  filenames = tf.gfile.Glob(input_files)
  # ********************************************2.使用多线程读取文件
  num_readers = config.num_readers
  if num_readers > len(filenames):
    num_readers = len(filenames)
    tf.logging.warning('num_readers has been reduced to %d to match input file '
                       'shards.' % num_readers)
  #**********************************************3.将filenames构造成dataset
  filename_dataset = tf.data.Dataset.from_tensor_slices(filenames)
  if config.shuffle:
    filename_dataset = filename_dataset.shuffle(
        config.filenames_shuffle_buffer_size)
  elif num_readers > 1:
    tf.logging.warning('`shuffle` is false, but the input data stream is '
                       'still slightly shuffled since `num_readers` > 1.')
  filename_dataset = filename_dataset.repeat(config.num_epochs or None)
  #******************************************4.实现并行，并真正读取数据集
  records_dataset = filename_dataset.apply(
      tf.contrib.data.parallel_interleave(
          file_read_func,#处理dataset数据的函数
          cycle_length=num_readers,
          block_length=config.read_block_length,
          sloppy=config.shuffle))
  if config.shuffle:
    records_dataset = records_dataset.shuffle(config.shuffle_buffer_size)
  #*******************************************5.返回原始records数据的dataset
  return records_dataset


def build(input_reader_config, batch_size=None, transform_input_data_fn=None):
  """Builds a tf.data.Dataset.

  Builds a tf.data.Dataset by applying the `transform_input_data_fn` on all
  records. Applies a padded batch to the resulting dataset.

  Args:
    input_reader_config: A input_reader_pb2.InputReader object.
    batch_size: Batch size. If batch size is None, no batching is performed.
    transform_input_data_fn: Function to apply transformation to all records,
      or None if no extra decoding is required.

  Returns:
    A tf.data.Dataset based on the input_reader_config.

  Raises:
    ValueError: On invalid input reader proto.
    ValueError: If no input paths are specified.
  """
  if not isinstance(input_reader_config, input_reader_pb2.InputReader):
    raise ValueError('input_reader_config not of type '
                     'input_reader_pb2.InputReader.')

  if input_reader_config.WhichOneof('input_reader') == 'tf_record_input_reader':
    config = input_reader_config.tf_record_input_reader
    if not config.input_path:
      raise ValueError('At least one input path must be specified in '
                       '`input_reader_config`.')
    # *******************************************1.首先读取label_map_path
    label_map_proto_file = None
    if input_reader_config.HasField('label_map_path'):
      label_map_proto_file = input_reader_config.label_map_path
    # ******************************************2.构造TfExampleDecoder,可以把ExampleString转成tensor:使用了slim的解码api
    # 通过decoder可以控制只解码哪些内容
    decoder = tf_example_decoder.TfExampleDecoder(
        load_instance_masks=input_reader_config.load_instance_masks,#默认false
        instance_mask_type=input_reader_config.mask_type,
        label_map_proto_file=label_map_proto_file,
        use_display_name=input_reader_config.use_display_name,
        num_additional_channels=input_reader_config.num_additional_channels)#默认0

    def process_fn(value):
      """Sets up tf graph that decodes, transforms and pads input data."""
      # ----------------------------------------1.解码
      processed_tensors = decoder.decode(value)
      # ———————————————————  2.执行预处理和转换
      if transform_input_data_fn is not None:
        processed_tensors = transform_input_data_fn(processed_tensors)
      return processed_tensors
    # ******************************************3.先构造读取tfrecord文件的dataset
    dataset = read_dataset(
        functools.partial(tf.data.TFRecordDataset, buffer_size=8 * 1000 * 1000),#读取文件的函数
        config.input_path[:], input_reader_config)
    #******************************************4.设定只使用多少 tfrecord shards：可以只使用部分的samp，例如在验证集的时候
    if input_reader_config.sample_1_of_n_examples > 1:
      dataset = dataset.shard(input_reader_config.sample_1_of_n_examples, 0)
    # TODO(rathodv): make batch size a required argument once the old binaries
    # are deleted.
    #*******************************************5.使用转换函数process_fn，设置并发执行个数
    if batch_size:
      num_parallel_calls = batch_size * input_reader_config.num_parallel_batches
    else:
      num_parallel_calls = input_reader_config.num_parallel_map_calls
    dataset = dataset.map(
        process_fn,
        num_parallel_calls=num_parallel_calls)#使用多线程处理dataset的每个element
    # ****************************************6.进行batch, 并决定是否保留最后一个不完整batch
    if batch_size:
      dataset = dataset.apply(
          tf.contrib.data.batch_and_drop_remainder(batch_size))
    #*******************************************7.预取多少个batch？还是多少个element(可以做实验测测)
    dataset = dataset.prefetch(input_reader_config.num_prefetch_batches)
    return dataset

  raise ValueError('Unsupported input_reader_config.')
