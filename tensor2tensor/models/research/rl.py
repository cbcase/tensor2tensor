# coding=utf-8
# Copyright 2018 The Tensor2Tensor Authors.
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

"""Reinforcement learning models and parameters."""

import collections
import functools
import operator
import gym

from tensor2tensor.layers import common_hparams
from tensor2tensor.layers import common_layers
from tensor2tensor.layers import discretization
from tensor2tensor.rl.envs.py_func_batch_env import PyFuncBatchEnv
from tensor2tensor.rl.envs.simulated_batch_env import SimulatedBatchEnv
from tensor2tensor.rl.envs.simulated_batch_gym_env import SimulatedBatchGymEnv
from tensor2tensor.utils import registry
from tensor2tensor.utils import t2t_model

import tensorflow as tf
import tensorflow_probability as tfp


@registry.register_hparams
def ppo_base_v1():
  """Set of hyperparameters."""
  hparams = common_hparams.basic_params1()
  hparams.learning_rate_schedule = "constant"
  hparams.learning_rate_constant = 1e-4
  hparams.clip_grad_norm = 0.5
  # If set, extends the LR warmup to all epochs except the final one.
  hparams.add_hparam("lr_decay_in_final_epoch", False)
  hparams.add_hparam("init_mean_factor", 0.1)
  hparams.add_hparam("init_logstd", 0.1)
  hparams.add_hparam("policy_layers", (100, 100))
  hparams.add_hparam("value_layers", (100, 100))
  hparams.add_hparam("clipping_coef", 0.2)
  hparams.add_hparam("gae_gamma", 0.99)
  hparams.add_hparam("gae_lambda", 0.95)
  hparams.add_hparam("entropy_loss_coef", 0.01)
  hparams.add_hparam("value_loss_coef", 1)
  hparams.add_hparam("optimization_epochs", 15)
  hparams.add_hparam("epoch_length", 200)
  hparams.add_hparam("epochs_num", 2000)
  hparams.add_hparam("eval_every_epochs", 10)
  hparams.add_hparam("save_models_every_epochs", 30)
  hparams.add_hparam("optimization_batch_size", 50)
  hparams.add_hparam("intrinsic_reward_scale", 0.)
  hparams.add_hparam("logits_clip", 0.0)
  hparams.add_hparam("dropout_ppo", 0.1)
  hparams.add_hparam("effective_num_agents", None)
  return hparams


@registry.register_hparams
def basic_policy_parameters():
  wrappers = None
  return tf.contrib.training.HParams(wrappers=wrappers)


@registry.register_hparams
def ppo_discrete_action_base():
  hparams = ppo_base_v1()
  hparams.add_hparam("policy_network", "feed_forward_categorical_policy")
  return hparams


@registry.register_hparams
def discrete_random_action_base():
  hparams = common_hparams.basic_params1()
  hparams.add_hparam("policy_network", "random_policy")
  return hparams


@registry.register_hparams
def ppo_atari_base():
  """Pong base parameters."""
  hparams = ppo_discrete_action_base()
  hparams.learning_rate_constant = 1e-4
  hparams.epoch_length = 200
  hparams.gae_gamma = 0.985
  hparams.gae_lambda = 0.985
  hparams.entropy_loss_coef = 0.003
  hparams.value_loss_coef = 1
  hparams.optimization_epochs = 3
  hparams.epochs_num = 1000
  hparams.policy_network = "feed_forward_cnn_small_categorical_policy"
  hparams.clipping_coef = 0.2
  hparams.optimization_batch_size = 20
  hparams.clip_grad_norm = 0.5
  return hparams


@registry.register_hparams
def ppo_original_params():
  """Parameters based on the original PPO paper."""
  hparams = ppo_atari_base()
  hparams.learning_rate_constant = 2.5e-4
  hparams.gae_gamma = 0.99
  hparams.gae_lambda = 0.95
  hparams.clipping_coef = 0.1
  hparams.value_loss_coef = 1
  hparams.entropy_loss_coef = 0.01
  hparams.eval_every_epochs = 200
  hparams.dropout_ppo = 0.1
  # The parameters below are modified to accommodate short epoch_length (which
  # is needed for model based rollouts).
  hparams.epoch_length = 50
  hparams.optimization_batch_size = 20
  return hparams


def make_real_env_fn(env):
  """Creates a function returning a given real env, in or out of graph.

  Args:
    env: Environment to return from the function.

  Returns:
    Function in_graph -> env.
  """
  return lambda in_graph: PyFuncBatchEnv(env) if in_graph else env


def make_simulated_env_fn(**env_kwargs):
  """Returns a function creating a simulated env, in or out of graph.

  Args:
    **env_kwargs: kwargs to pass to the simulated env constructor.

  Returns:
    Function in_graph -> env.
  """
  def env_fn(in_graph):
    class_ = SimulatedBatchEnv if in_graph else SimulatedBatchGymEnv
    return class_(**env_kwargs)
  return env_fn


def get_policy(observations, hparams, action_space):
  """Get a policy network.

  Args:
    observations: observations
    hparams: parameters
    action_space: action space

  Returns:
    Tuple (action logits, value).
  """
  if not isinstance(action_space, gym.spaces.Discrete):
    raise ValueError("Expecting discrete action space.")

  model = registry.model(hparams.policy_network)(
      hparams, tf.estimator.ModeKeys.TRAIN
  )
  obs_shape = common_layers.shape_list(observations)
  features = {
      "inputs": observations,
      "target_action": tf.zeros(obs_shape[:2] + [action_space.n]),
      "target_value": tf.zeros(obs_shape[:2])
  }
  with tf.variable_scope(tf.get_variable_scope(), reuse=tf.AUTO_REUSE):
    (targets, _) = model(features)
  return (targets["target_action"], targets["target_value"])


@registry.register_hparams
def ppo_pong_ae_base():
  """Pong autoencoder base parameters."""
  hparams = ppo_original_params()
  hparams.learning_rate_constant = 1e-4
  hparams.network = "dense_bitwise_categorical_policy"
  return hparams


@registry.register_hparams
def dqn_atari_base():
  # These params are based on agents/dqn/configs/dqn.gin
  # with some modifications taking into account our code
  return tf.contrib.training.HParams(
      agent_gamma=0.99,
      agent_update_horizon=1,
      agent_min_replay_history=20000,  # agent steps
      agent_update_period=4,
      agent_target_update_period=8000,  # agent steps
      agent_epsilon_train=0.01,
      agent_epsilon_eval=0.001,
      agent_epsilon_decay_period=250000,  # agent steps
      agent_generates_trainable_dones=True,

      optimizer_class="RMSProp",
      optimizer_learning_rate=0.00025,
      optimizer_decay=0.95,
      optimizer_momentum=0.0,
      optimizer_epsilon=0.00001,
      optimizer_centered=True,

      replay_buffer_replay_capacity=1000000,
      replay_buffer_batch_size=32,

      time_limit=27000,
      save_every_steps=50000,
      num_frames=int(20 * 1e6),
  )


@registry.register_hparams
def dqn_original_params():
  """dqn_original_params."""
  hparams = dqn_atari_base()
  hparams.set_hparam("num_frames", int(1e6))
  return hparams


@registry.register_hparams
def rlmf_original():
  return tf.contrib.training.HParams(
      game="pong",
      base_algo="ppo",
      base_algo_params="ppo_original_params",
      batch_size=16,
      eval_batch_size=2,
      frame_stack_size=4,
      eval_sampling_temps=[0.0, 0.2, 0.5, 0.8, 1.0, 2.0],
      eval_max_num_noops=8,
      resize_height_factor=2,
      resize_width_factor=2,
      grayscale=0,
      rl_env_max_episode_steps=-1,
  )


@registry.register_hparams
def rlmf_base():
  """Base set of hparams for model-free PPO."""
  hparams = rlmf_original()
  hparams.add_hparam("ppo_epochs_num", 3000)
  hparams.add_hparam("ppo_eval_every_epochs", 100)
  return hparams


@registry.register_hparams
def rlmf_tiny():
  hparams = rlmf_base()
  hparams.ppo_epochs_num = 100
  hparams.ppo_eval_every_epochs = 10
  return hparams


class DiscretePolicyBase(t2t_model.T2TModel):

  @staticmethod
  def _get_num_actions(features):
    return common_layers.shape_list(features["target_action"])[2]


NetworkOutput = collections.namedtuple(
    "NetworkOutput", "policy, value, action_postprocessing")


# TODO(koz4k): Translate it to T2TModel or remove.
def feed_forward_gaussian_fun(action_space, config, observations):
  """Feed-forward Gaussian."""
  if not isinstance(action_space, gym.spaces.box.Box):
    raise ValueError("Expecting continuous action space.")

  mean_weights_initializer = tf.contrib.layers.variance_scaling_initializer(
      factor=config.init_mean_factor)
  logstd_initializer = tf.random_normal_initializer(config.init_logstd, 1e-10)

  flat_observations = tf.reshape(observations, [
      tf.shape(observations)[0], tf.shape(observations)[1],
      functools.reduce(operator.mul, observations.shape.as_list()[2:], 1)])

  with tf.variable_scope("network_parameters"):
    with tf.variable_scope("policy"):
      x = flat_observations
      for size in config.policy_layers:
        x = tf.contrib.layers.fully_connected(x, size, tf.nn.relu)
      mean = tf.contrib.layers.fully_connected(
          x, action_space.shape[0], tf.tanh,
          weights_initializer=mean_weights_initializer)
      logstd = tf.get_variable(
          "logstd", mean.shape[2:], tf.float32, logstd_initializer)
      logstd = tf.tile(
          logstd[None, None],
          [tf.shape(mean)[0], tf.shape(mean)[1]] + [1] * (mean.shape.ndims - 2))
    with tf.variable_scope("value"):
      x = flat_observations
      for size in config.value_layers:
        x = tf.contrib.layers.fully_connected(x, size, tf.nn.relu)
      value = tf.contrib.layers.fully_connected(x, 1, None)[..., 0]
  mean = tf.check_numerics(mean, "mean")
  logstd = tf.check_numerics(logstd, "logstd")
  value = tf.check_numerics(value, "value")

  policy = tfp.distributions.MultivariateNormalDiag(mean, tf.exp(logstd))

  return NetworkOutput(policy, value, lambda a: tf.clip_by_value(a, -2., 2))


def clip_logits(logits, config):
  logits_clip = getattr(config, "logits_clip", 0.)
  if logits_clip > 0:
    min_logit = tf.reduce_min(logits)
    return tf.minimum(logits - min_logit, logits_clip)
  else:
    return logits


@registry.register_model
class FeedForwardCategoricalPolicy(DiscretePolicyBase):
  """Feed-forward categorical."""

  def body(self, features):
    observations = features["inputs"]
    flat_observations = tf.reshape(observations, [
        tf.shape(observations)[0], tf.shape(observations)[1],
        functools.reduce(operator.mul, observations.shape.as_list()[2:], 1)])
    with tf.variable_scope("policy"):
      x = flat_observations
      for size in self.hparams.policy_layers:
        x = tf.contrib.layers.fully_connected(x, size, tf.nn.relu)
      logits = tf.contrib.layers.fully_connected(
          x, self._get_num_actions(features), activation_fn=None
      )
    with tf.variable_scope("value"):
      x = flat_observations
      for size in self.hparams.value_layers:
        x = tf.contrib.layers.fully_connected(x, size, tf.nn.relu)
      value = tf.contrib.layers.fully_connected(x, 1, None)[..., 0]
    logits = clip_logits(logits, self.hparams)
    return {"target_action": logits, "target_value": value}


@registry.register_model
class FeedForwardCnnSmallCategoricalPolicy(DiscretePolicyBase):
  """Small cnn network with categorical output."""

  def body(self, features):
    observations = features["inputs"]
    obs_shape = common_layers.shape_list(observations)
    x = tf.reshape(observations, [-1] + obs_shape[2:])
    dropout = getattr(self.hparams, "dropout_ppo", 0.0)
    with tf.variable_scope("feed_forward_cnn_small"):
      x = tf.to_float(x) / 255.0
      x = tf.contrib.layers.conv2d(x, 32, [5, 5], [2, 2],
                                   activation_fn=tf.nn.relu, padding="SAME")
      x = tf.contrib.layers.conv2d(x, 32, [5, 5], [2, 2],
                                   activation_fn=tf.nn.relu, padding="SAME")

      flat_x = tf.reshape(
          x, [obs_shape[0], obs_shape[1],
              functools.reduce(operator.mul, x.shape.as_list()[1:], 1)])
      flat_x = tf.nn.dropout(flat_x, keep_prob=1.0 - dropout)
      x = tf.contrib.layers.fully_connected(flat_x, 128, tf.nn.relu)

      logits = tf.contrib.layers.fully_connected(
          x, self._get_num_actions(features), activation_fn=None
      )
      logits = clip_logits(logits, self.hparams)

      value = tf.contrib.layers.fully_connected(
          x, 1, activation_fn=None)[..., 0]
    return {"target_action": logits, "target_value": value}


@registry.register_model
class FeedForwardCnnSmallCategoricalPolicyNew(DiscretePolicyBase):
  """Small cnn network with categorical output."""

  def body(self, features):
    observations = features["inputs"]
    obs_shape = common_layers.shape_list(observations)
    x = tf.reshape(observations, [-1] + obs_shape[2:])
    dropout = getattr(self.hparams, "dropout_ppo", 0.0)
    with tf.variable_scope("feed_forward_cnn_small"):
      x = tf.to_float(x) / 255.0
      x = tf.nn.dropout(x, keep_prob=1.0 - dropout)
      x = tf.layers.conv2d(
          x, 32, (4, 4), strides=(2, 2), name="conv1",
          activation=common_layers.belu, padding="SAME")
      x = tf.nn.dropout(x, keep_prob=1.0 - dropout)
      x = tf.layers.conv2d(
          x, 64, (4, 4), strides=(2, 2), name="conv2",
          activation=common_layers.belu, padding="SAME")
      x = tf.nn.dropout(x, keep_prob=1.0 - dropout)
      x = tf.layers.conv2d(
          x, 128, (4, 4), strides=(2, 2), name="conv3",
          activation=common_layers.belu, padding="SAME")

      flat_x = tf.reshape(
          x, [obs_shape[0], obs_shape[1],
              functools.reduce(operator.mul, x.shape.as_list()[1:], 1)])
      flat_x = tf.nn.dropout(flat_x, keep_prob=1.0 - dropout)
      x = tf.layers.dense(flat_x, 128, activation=tf.nn.relu, name="dense1")

      logits = tf.layers.dense(
          x, self._get_num_actions(features), name="dense2"
      )
      logits = clip_logits(logits, self.hparams)

      value = tf.layers.dense(x, 1, name="value")[..., 0]
    return {"target_action": logits, "target_value": value}


@registry.register_model
class DenseBitwiseCategoricalPolicy(DiscretePolicyBase):
  """Dense network with bitwise input and categorical output."""

  def body(self, features):
    observations = features["inputs"]
    obs_shape = common_layers.shape_list(observations)
    x = tf.reshape(observations, [-1] + obs_shape[2:])
    with tf.variable_scope("dense_bitwise"):
      x = discretization.int_to_bit_embed(x, 8, 32)
      flat_x = tf.reshape(
          x, [obs_shape[0], obs_shape[1],
              functools.reduce(operator.mul, x.shape.as_list()[1:], 1)])

      x = tf.contrib.layers.fully_connected(flat_x, 256, tf.nn.relu)
      x = tf.contrib.layers.fully_connected(flat_x, 128, tf.nn.relu)

      logits = tf.contrib.layers.fully_connected(
          x, self._get_num_actions(features), activation_fn=None
      )

      value = tf.contrib.layers.fully_connected(
          x, 1, activation_fn=None)[..., 0]

    return {"target_action": logits, "target_value": value}


@registry.register_model
class RandomPolicy(DiscretePolicyBase):
  """Random policy with categorical output."""

  def body(self, features):
    observations = features["inputs"]
    obs_shape = observations.shape.as_list()
    # Just so Saver doesn't complain because of no variables.
    tf.get_variable("dummy_var", initializer=0.0)
    num_actions = self._get_num_actions(features)
    logits = tf.constant(
        1. / float(num_actions),
        shape=(obs_shape[:2] + [num_actions])
    )
    value = tf.zeros(obs_shape[:2])
    return {"target_action": logits, "target_value": value}
