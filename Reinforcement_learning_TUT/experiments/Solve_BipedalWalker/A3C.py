"""
Asynchronous Advantage Actor Critic (A3C), Reinforcement Learning.

The BipedalWalker example.

View more on [莫烦Python] : https://morvanzhou.github.io/tutorials/

Using:
tensorflow 1.0
gym 0.8.0
"""

import multiprocessing
import threading
import tensorflow as tf
import numpy as np
import gym
import os
import shutil


GAME = 'BipedalWalker-v2'
OUTPUT_GRAPH = False
LOG_DIR = './log'
N_WORKERS = multiprocessing.cpu_count()
MAX_GLOBAL_EP = 5000
GLOBAL_NET_SCOPE = 'Global_Net'
MEMORY_CAPACITY = 500
UPDATE_GLOBAL_ITER = 10
GAMMA = 0.99
ENTROPY_BETA = 0.01
LR_A = 0.001    # learning rate for actor
LR_C = 0.001    # learning rate for critic

env = gym.make(GAME)

N_S = env.observation_space.shape[0]
N_A = env.action_space.shape[0]
A_BOUND = [env.action_space.low, env.action_space.high]


class ACNet(object):
    def __init__(self, scope, globalAC=None):
        if scope == GLOBAL_NET_SCOPE:   # get global network
            with tf.variable_scope(scope):
                self.s = tf.placeholder(tf.float32, [None, N_S], 'S')
                self._build_net(N_A)
                self.a_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/actor')
                self.c_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/critic')
        else:   # local net, calculate losses
            self.memory = np.zeros((MEMORY_CAPACITY, N_S+N_A+1))    # local memory for replay
            self._memory_pointer = 0

            with tf.variable_scope(scope):
                self.s = tf.placeholder(tf.float32, [None, N_S], 'S')
                self.a_his = tf.placeholder(tf.float32, [None, N_A], 'A')
                self.v_target = tf.placeholder(tf.float32, [None, 1], 'Vtarget')

                mu, sigma, self.v = self._build_net(N_A)

                td = tf.subtract(self.v_target, self.v, name='TD_error')
                with tf.name_scope('c_loss'):
                    self.c_loss = tf.reduce_sum(tf.square(td))

                with tf.name_scope('wrap_a_out'):
                    mu, sigma = mu * A_BOUND[1], sigma + 1e-6
                self.test = sigma[0]
                normal_dist = tf.contrib.distributions.Normal(mu, sigma)

                with tf.name_scope('a_loss'):
                    log_prob = normal_dist.log_prob(self.a_his)
                    exp_v = log_prob * td
                    entropy = normal_dist.entropy()  # encourage exploration
                    self.exp_v = tf.reduce_sum(ENTROPY_BETA * entropy + exp_v)
                    self.a_loss = -self.exp_v

                with tf.name_scope('choose_a'):  # use local params to choose action
                    self.A = tf.clip_by_value(tf.squeeze(normal_dist.sample(1), axis=0), A_BOUND[0], A_BOUND[1])
                with tf.name_scope('local_grad'):
                    self.a_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/actor')
                    self.c_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/critic')
                    self.a_grads = tf.gradients(self.a_loss, self.a_params)  # get local gradients
                    self.c_grads = tf.gradients(self.c_loss, self.c_params)

            with tf.name_scope('sync'):
                with tf.name_scope('pull'):
                    self.pull_a_params_op = [l_p.assign(g_p) for l_p, g_p in zip(self.a_params, globalAC.a_params)]
                    self.pull_c_params_op = [l_p.assign(g_p) for l_p, g_p in zip(self.c_params, globalAC.c_params)]
                with tf.name_scope('push'):
                    self.update_a_op = OPT_A.apply_gradients(zip(self.a_grads, globalAC.a_params))
                    self.update_c_op = OPT_C.apply_gradients(zip(self.c_grads, globalAC.c_params))

    def _build_net(self, n_a):
        w_init = tf.random_normal_initializer(0., .01)
        with tf.variable_scope('actor'):
            l_a = tf.layers.dense(self.s, 400, tf.nn.relu, kernel_initializer=w_init, name='la')
            mu = tf.layers.dense(l_a, n_a, tf.nn.tanh, kernel_initializer=w_init, name='mu')
            sigma = tf.layers.dense(l_a, n_a, tf.nn.softplus, kernel_initializer=w_init, name='sigma',
                                    # bias_initializer=tf.constant_initializer(-1.),
                                    )
        with tf.variable_scope('critic'):
            l_c = tf.layers.dense(self.s, 300, tf.nn.relu, kernel_initializer=w_init, name='lc')
            v = tf.layers.dense(l_c, 1, kernel_initializer=w_init, name='v')  # state value
        return mu, sigma, v

    def store_batch_transitions(self, b_t):     # run by a local
        b_len = b_t.shape[0]
        space_left = MEMORY_CAPACITY - self._memory_pointer
        if space_left < b_len:
            b_t1 = b_t[:space_left]
            b_t2 = b_t[space_left:]
            self.memory[self._memory_pointer:] = b_t1
            self._memory_pointer = b_t2.shape[0]
            self.memory[:self._memory_pointer] = b_t2
        else:
            self._memory_pointer += b_len
            self.memory[self._memory_pointer-b_len: self._memory_pointer] = b_t

    def sample(self, n):
        index = np.random.choice(np.arange(MEMORY_CAPACITY, dtype=np.int32), n)
        return self.memory[index]

    def update_global(self, feed_dict):  # run by a local
        _, _, t = SESS.run([self.update_a_op, self.update_c_op, self.test], feed_dict)  # local grads applies to global net
        return t

    def pull_global(self):  # run by a local
        SESS.run([self.pull_a_params_op, self.pull_c_params_op])

    def choose_action(self, s):  # run by a local
        s = s[np.newaxis, :]
        return SESS.run(self.A, {self.s: s})[0]


class Worker(object):
    def __init__(self, env, name, globalAC):
        self.env = env
        self.name = name
        self.AC = ACNet(name, globalAC)

    def work(self):
        total_step = 1
        buffer_s, buffer_a, buffer_r = [], [], []
        while not COORD.should_stop() and GLOBAL_EP.eval(SESS) < MAX_GLOBAL_EP:
            s = self.env.reset()
            ep_r = 0
            while True:
                if self.name == 'W_0' and total_step % 30 == 0:
                    self.env.render()
                a = self.AC.choose_action(s)
                s_, r, done, info = self.env.step(a)

                if r == -100: r = -2     # normalize reward

                ep_r += r
                buffer_s.append(s)
                buffer_a.append(a)
                buffer_r.append(r)

                if total_step % UPDATE_GLOBAL_ITER == 0 or done:   # update global and assign to local net
                    if done:
                        v_s_ = 0   # terminal
                    else:
                        v_s_ = SESS.run(self.AC.v, {self.AC.s: s_[np.newaxis, :]})[0, 0]
                    buffer_v_target = []
                    for r in buffer_r[::-1]:    # reverse buffer r
                        v_s_ = r + GAMMA * v_s_
                        buffer_v_target.append(v_s_)
                    buffer_v_target.reverse()

                    buffer_s, buffer_a, buffer_v_target = np.vstack(buffer_s), np.vstack(buffer_a), np.vstack(buffer_v_target)
                    buffer_t = np.hstack((buffer_s, buffer_a, buffer_v_target))
                    self.AC.store_batch_transitions(buffer_t)

                    buffer_s, buffer_a, buffer_r = [], [], []

                if total_step > MEMORY_CAPACITY and total_step % UPDATE_GLOBAL_ITER == 0:
                    sampled_batch = self.AC.sample(UPDATE_GLOBAL_ITER)
                    feed_dict = {
                        self.AC.s: sampled_batch[:, :N_S],
                        self.AC.a_his: sampled_batch[:, N_S: N_S+N_A],
                        self.AC.v_target: sampled_batch[:, -1:],
                    }
                    test = self.AC.update_global(feed_dict)
                    self.AC.pull_global()

                    if done:
                        achieve = '| Achieve' if self.env.unwrapped.hull.position[0] >= 88 else '| -------'
                        print(
                            self.name,
                            "Ep:", GLOBAL_EP.eval(SESS),
                            achieve,
                            "| Pos: %i" % self.env.unwrapped.hull.position[0],
                            "| Ep_r: %.2f" % ep_r,
                            '| var:', test,
                        )

                s = s_
                total_step += 1
                if done:
                    SESS.run(COUNT_GLOBAL_EP)
                    break


if __name__ == "__main__":
    SESS = tf.Session()

    with tf.device("/cpu:0"):
        GLOBAL_EP = tf.Variable(0, dtype=tf.int32, name='global_ep', trainable=False)
        COUNT_GLOBAL_EP = tf.assign(GLOBAL_EP, tf.add(GLOBAL_EP, tf.constant(1), name='step_ep'))
        OPT_A = tf.train.RMSPropOptimizer(LR_A, name='RMSPropA', decay=0.95)
        OPT_C = tf.train.RMSPropOptimizer(LR_C, name='RMSPropC', decay=0.95)
        GLOBAL_AC = ACNet(GLOBAL_NET_SCOPE)  # we only need its params
        workers = []
        # Create worker
        for i in range(N_WORKERS):
            i_name = 'W_%i' % i   # worker name
            workers.append(Worker(gym.make(GAME), i_name, GLOBAL_AC))

    COORD = tf.train.Coordinator()
    SESS.run(tf.global_variables_initializer())

    if OUTPUT_GRAPH:
        if os.path.exists(LOG_DIR):
            shutil.rmtree(LOG_DIR)
        tf.summary.FileWriter(LOG_DIR, SESS.graph)

    worker_threads = []
    for worker in workers:
        t = threading.Thread(target=worker.work)
        t.start()
        worker_threads.append(t)
    COORD.join(worker_threads)
