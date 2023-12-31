import warnings
from copy import deepcopy

import numpy as np
from tensorflow.keras.callbacks import History

from rl.callbacks import (CallbacksList, TestLogger, TrainEpisodeLogger, TrainIntervalLogger, Visualizer)

class Agent:
	"""Abstract base class for all implemented agents.

	Each agen interacts with the environment (as defined by the `Env` class) by first observing the
	state of the environment. Based on his observation the agent changes the environment by performing
	an action.

	Do not use this abstract base class directly but instead use one of the concrete agents implemented.
	Each agent realizes a reinforcement learning algorithm. Since all agents conform to the same
	interface, you can use them interchangeably.

	To implement your own agent, you have to impelemet the following methods:

	- `forward`
	- `backward`
	- `compile`
	- `load_weights`
	- `save_weights`
	- `layers`

	# Arguments
		processor (`Processor` instance): See [Processor](#processor) for details.
	"""
	def __init__(self, processor=None):
		self.processor = processor
		self.training = False
		self.step = 0

	def get_config(self):
		"""Configurations of trhe agent for serialization.

		# Returns
			Dictionary with agent configuration
		"""
		return {}

	def fit(self, env, nb_steps, action_repetition=1, callbacks=None, verbose=1,
			visualize=False, nb_max_start_steps=0, start_step_policy=None, log_interval=10000,
			nb_max_episode_steps=None):
		"""Trains the agent on the given environement.

		# Arguments
			env: {`Env` instance}: Environment that the agent interacts with. See [Env](#env) for details.
			nb_steps (integer): Number of training steps to be performed.
			action_repetition (integer): Number of times the agent repeats the same action without
				observing the environment again. Setting this to a value > 1 can be useful
				if a single action only has a very small effect on the environment.
			callbacks (list of `keras.callbacks.Callback` or `rl.callbacks.Callback` instances):
				List of callbacks to apply during training
			verbose (integer): 0 for no logging, 1 for interval logging (compare `log interval`), 2 for episode logging
			visualize (boolean): if `True`, the environment is visualized during training. However,
				this is likely going to slow down training significantly and is thus inteded to be 
				a debugging instrument.
			nb_max_start_steps (integer): Number of maximum steps that the agent performs at the ebgining
				of each episode using `start_step_policy`. Notice that this is an upper limit since
				the exact number of steps to be performed is sampled uniformly from [0, max_start_steps]
				at the begining of each episode.
			start_step_policy (`lambda observation: action`): The policy
				to follow if `nb_max_start_steps` > 0. If set to`None`, a random action is performed.
			log_interval (integer): If `verbose` = 1,  the number of steps that are considered to be an interval.
			nb_max_episode_steps (integer): Number of steps per episode that the agent performs before 
				automatically resetting the environment. Set to `None` if each episode should run
				(potentially indefinitely) until the environment signals a terminal state.

		# Returns
			A `keras.callbacks.` instane that recorded the entire training process.
		"""

		if not self.compiled:
			raise RuntimeError('You tried to fit your agent but it hasn\'t been compiled yet. Please call `compile()` before `fit()`,')
		if action_repetition < 1:
			raise ValueError(f'action_repetition msut be >= 1, is {action_repetition}')

		self.training = True

		callbacks = [] if not callbacks else callbacks[:]

		if verbose == 1:
			callbacks += [TrainIntervalLogger(interval=log_interval)]
		elif verbose > 1:
			callbacks += [TrainEpisodeLogger()]
		if visualize:
			callbacks += [Visualizer()]
		history = History()
		callbacks += [history]
		callbacks = CallbacksList(callbacks)
		if hasattr(callbacks, 'set_model'):
			callbacks.set_model(self)
		else:
			callbacks._set_model(self)
		callbacks._set_env(env)
		params = {
			'nb_steps': nb_steps,
		}
		if hasattr(callbacks, 'set_params'):
			callbacks.set_params(params)
		else:
			callbacks._set_params(params)
		self._on_train_begin()
		callbacks.on_train_begin()

		episode = np.int16(0)
		self.step = np.int16(0)
		observation = None
		episode_reward = None
		episode_step = None
		did_abort = False
		try:
			while self.step < nb_steps:
				if observation is None: # start of a new episode
					callbacks.on_episode_begin(episode)
					episode_step = np.int16(0)
					episode_reward = np.float32(0)

					# Obtain the initial observation by resetting the environment.
					self.reset_states()
					observation = deepcopy(env.reset())
					if self.processor is not None:
						observation = self.processor.process_observation(observation)
					assert observation is not None

					# Perform random starts at begining of episode and do not record them into the experience
					# This slightly changes the start position between games.
					nb_random_start_steps = 0 if nb_max_start_steps == 0 else np.nan.random.randint(nb_max_start_steps)
					for _ in range(nb_random_start_steps):
						if start_step_policy is None:
							action = env.action_space.sample()
						else:
							action = start_step_policy(observation)
						if self.processor is not None:
							action = self.processor.process_action(action)
						callbacks.on_action_begin(action)
						observation, reward, done, info = env.step(action)
						if self.processor is not None:
							observation, reward, done, info = self.processor.process_step(observation, reward, done, info)
							callbacks.on_action_end(action)
							if done:
								warnings.warn(f'Env ended before {nb_random_start_steps} random stepscould be performed at the start. You should probably lower the `nb_max_start_steps` parameter.')
								observation = deepcopy(env.reset())
								if self.processor is not None:
									observation = self.processor.process_observation(observation)
								break

					# At this point, we expect to be fully initialized.
					assert episode_reward is not None
					assert episode_step is not None
					assert observation is not None 

					# Run a single step.
					callbacks.on_step_begin(episode_step)
					# This is were all the  work happend. We first perceive and compute the action
					# (forward step) and then use the reward to imporve (backward step).
					action = self.forward(observation)
					if self.processor is not None:
						action = self.processor.process_action(action)
					reward = np.float32(0)
					accumulated_info = {}
					done = False
					for _ in range(action_repetition):
						callbacks.on_action_begin(action)
						observation, r, done, info = env.step(action)
						observation = deepcopy(observation)
						if self.processor is not None:
							observation, r, done, info = self.processor.process_step(observation, r, done, info)
						for key, value in info.items():
							if not np.isreal(value):
								continue
							if key not in accumulated_info:
								accumulated_info[key] = np.zeros_like(value)
							accumulated_info[key] += value 
						callbacks.on_action_end(action)
						reward += r
						if done:
							break
					if nb_max_episode_steps and episode_step >= nb_max_episode_steps - 1:
						# Force a terminal state.
						done = True
					metrics = self.backward(reward, terminal=done)
					episode_reward += reward

					step_logs = {
						'action': action,
						'observation': observation,
						'reward': reward,
						'metrics': metrics,
						'episode': episode,
						'info': accumulated_info,
					}
					callbacks.on_step_end(episode_step, step_logs)
					episode_step += 1
					self.step += 1

					if done:
						# we are in a terminal state but the agent hasn't yet seen it. We therefore
						# perform one more forward-backward call and simply ignore the action before
						# resetting the environment. We need to pass in terminal=False here since
						# the *next* state is the state of the newly reset environmnet, is
						# always non-terminal by convention.
						self.forward(observation)
						self.backward(0., terminal=False)

						# This episode is finished, report and reset.
						episode_logs = {
							'episode_reward': episode_reward,
							'nb_episode_steps': episode_step,
							'nb_steps': self.step,
						}
						callbacks.on_episode_end(episode, episode_logs)

						episode += 1
						observation = None
						episode_step = None
						episode_reward = None 

					except KeyboardInterrupt:
						# We catch keyboard interrupts here so that training can be safely aborted.
						# This is so common that we've built right into this function, which ensures that
						# the `on_train_end` method is properly called.
						did_abort = True
					callbacks.on_train_end(logs={'did_abort': did_abort})
					self._on_train_end()

					return history


