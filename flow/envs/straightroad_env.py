"""Environment for training vehicles to reduce congestion in the I210."""

from gym.spaces import Box
import numpy as np

from flow.core.rewards import average_velocity
from flow.envs.base import Env

# largest number of lanes on any given edge in the network
MAX_LANES = 6
MAX_NUM_VEHS = 8
SPEED_SCALE = 50
HEADWAY_SCALE = 1000

ADDITIONAL_ENV_PARAMS = {
    # maximum acceleration for autonomous vehicles, in m/s^2
    "max_accel": 1,
    # maximum deceleration for autonomous vehicles, in m/s^2
    "max_decel": 1,
    # whether we use an obs space that contains adjacent lane info or just the lead obs
    "lead_obs": True,
    # whether the reward should come from local vehicles instead of global rewards
    "local_reward": True,
    # if the environment terminates once a wave has occurred
    "terminate_on_wave": True,
    # the environment is not allowed to terminate below this horizon length
    'wave_termination_horizon': 500,
    # the speed below which we consider a wave to have occured
    'wave_termination_speed': 10.0
}


class I210SingleEnv(Env):
    """Partially observable single-agent environment for the I-210 subnetworks.

    The policy is shared among the agents, so there can be a non-constant
    number of RL vehicles throughout the simulation.

    Required from env_params:

    * max_accel: maximum acceleration for autonomous vehicles, in m/s^2
    * max_decel: maximum deceleration for autonomous vehicles, in m/s^2

    The following states, actions and rewards are considered for one autonomous
    vehicle only, as they will be computed in the same way for each of them.

    States
        The observation consists of the speeds and bumper-to-bumper headways of
        the vehicles immediately preceding and following autonomous vehicles in
        all of the preceding lanes as well, a binary value indicating which of
        these vehicles is autonomous, and the speed of the autonomous vehicle.
        Missing vehicles are padded with zeros.

    Actions
        The action consists of an acceleration, bound according to the
        environment parameters, as well as three values that will be converted
        into probabilities via softmax to decide of a lane change (left, none
        or right). NOTE: lane changing is currently not enabled. It's a TODO.

    Rewards
        The reward function encourages proximity of the system-level velocity
        to a desired velocity specified in the environment parameters, while
        slightly penalizing small time headways among autonomous vehicles.

    Termination
        A rollout is terminated if the time horizon is reached or if two
        vehicles collide into one another.
    """

    def __init__(self, env_params, sim_params, network, simulator='traci'):
        super().__init__(env_params, sim_params, network, simulator)
        self.lead_obs = env_params.additional_params.get("lead_obs")
        self.max_lanes = MAX_LANES
        self.total_reward = 0.0

    @property
    def observation_space(self):
        """See class definition."""
        # speed, speed of leader, headway
        if self.lead_obs:
            return Box(
                low=-float('inf'),
                high=float('inf'),
                shape=(3 * MAX_NUM_VEHS,),
                dtype=np.float32
            )
        # speed, dist to ego vehicle, binary value which is 1 if the vehicle is
        # an AV
        else:
            leading_obs = 3 * self.max_lanes
            follow_obs = 3 * self.max_lanes

            # speed and lane
            self_obs = 2

            return Box(
                low=-float('inf'),
                high=float('inf'),
                shape=(leading_obs + follow_obs + self_obs,),
                dtype=np.float32
            )

    @property
    def action_space(self):
        """See class definition."""
        return Box(
            low=-np.abs(self.env_params.additional_params['max_decel']),
            high=self.env_params.additional_params['max_accel'],
            shape=(1 * MAX_NUM_VEHS,),  # (4,),
            dtype=np.float32)

    def _apply_rl_actions(self, rl_actions):
        """See class definition."""
        # in the warmup steps, rl_actions is None
        if rl_actions is not None:
            accels = []
            veh_ids = []
            rl_ids = self.get_sorted_rl_ids()

            for i, rl_id in enumerate(rl_ids):
                accels.append(rl_actions[i])
                veh_ids.append(rl_id)

                # lane_change_softmax = np.exp(actions[1:4])
                # lane_change_softmax /= np.sum(lane_change_softmax)
                # lane_change_action = np.random.choice([-1, 0, 1],
                #                                       p=lane_change_softmax)

            self.k.vehicle.apply_acceleration(rl_ids, accels)
                # self.k.vehicle.apply_lane_change(rl_id, lane_change_action)

    def get_state(self):
        """See class definition."""
        rl_ids = self.get_sorted_rl_ids()
        veh_info = np.zeros(self.observation_space.shape[0])
        per_vehicle_obs = 3
        for i, rl_id in enumerate(rl_ids):
            speed = self.k.vehicle.get_speed(rl_id)
            lead_id = self.k.vehicle.get_leader(rl_id)
            if lead_id in ["", None]:
                # in case leader is not visible
                lead_speed = SPEED_SCALE
                headway = HEADWAY_SCALE
            else:
                lead_speed = self.k.vehicle.get_speed(lead_id)
                headway = self.k.vehicle.get_headway(rl_id)
            veh_info[i * per_vehicle_obs: (i+1) * per_vehicle_obs] = [speed / SPEED_SCALE,
                                                                      headway /HEADWAY_SCALE, lead_speed / SPEED_SCALE]
        return veh_info

    def compute_reward(self, rl_actions, **kwargs):
        """See class definition."""
        # in the warmup steps
        if rl_actions is None:
            return {}

        rl_ids = self.get_sorted_rl_ids()

        des_speed = self.env_params.additional_params["target_velocity"]
        rewards = np.nan_to_num(np.mean([(des_speed - np.abs(speed - des_speed))**2
                                              for speed in self.k.vehicle.get_speed(rl_ids)])) / (des_speed**2)
        return rewards

    def get_sorted_rl_ids(self):
        rl_ids = self.k.vehicle.get_rl_ids()
        rl_ids = sorted(rl_ids, key=lambda veh_id: self.k.vehicle.get_x_by_id(veh_id))[::-1]
        rl_ids = rl_ids[-MAX_NUM_VEHS:]
        return rl_ids

    def additional_command(self):
        """See parent class.

        Define which vehicles are observed for visualization purposes.
        """
        # specify observed vehicles
        for rl_id in self.k.vehicle.get_rl_ids():
            # leader
            lead_id = self.k.vehicle.get_leader(rl_id)
            if lead_id:
                self.k.vehicle.set_observed(lead_id)

    def state_util(self, rl_id):
        """Return an array of headway, tailway, leader speed, follower speed.

        Also return a 1 if leader is rl 0 otherwise, a 1 if follower is rl 0 otherwise.
        If there are fewer than MAX_LANES the extra
        entries are filled with -1 to disambiguate from zeros.
        """
        veh = self.k.vehicle
        lane_headways = veh.get_lane_headways(rl_id).copy()
        lane_tailways = veh.get_lane_tailways(rl_id).copy()
        lane_leader_speed = veh.get_lane_leaders_speed(rl_id).copy()
        lane_follower_speed = veh.get_lane_followers_speed(rl_id).copy()
        leader_ids = veh.get_lane_leaders(rl_id).copy()
        follower_ids = veh.get_lane_followers(rl_id).copy()
        rl_ids = self.k.vehicle.get_rl_ids()
        is_leader_rl = [1 if l_id in rl_ids else 0 for l_id in leader_ids]
        is_follow_rl = [1 if f_id in rl_ids else 0 for f_id in follower_ids]
        diff = MAX_LANES - len(is_leader_rl)
        if diff > 0:
            # the minus 1 disambiguates missing cars from missing lanes
            lane_headways += diff * [-1]
            lane_tailways += diff * [-1]
            lane_leader_speed += diff * [-1]
            lane_follower_speed += diff * [-1]
            is_leader_rl += diff * [-1]
            is_follow_rl += diff * [-1]
        lane_headways = np.asarray(lane_headways) / 1000
        lane_tailways = np.asarray(lane_tailways) / 1000
        lane_leader_speed = np.asarray(lane_leader_speed) / 100
        lane_follower_speed = np.asarray(lane_follower_speed) / 100
        return np.concatenate((lane_headways, lane_tailways, lane_leader_speed,
                               lane_follower_speed, is_leader_rl,
                               is_follow_rl))

    def veh_statistics(self, rl_id):
        """Return speed, edge information, and x, y about the vehicle itself."""
        speed = self.k.vehicle.get_speed(rl_id) / 100.0
        lane = (self.k.vehicle.get_lane(rl_id) + 1) / 10.0
        return np.array([speed, lane])


class SingleStraightRoad(I210SingleEnv):
    """Partially observable multi-agent environment for a straight road. Look at superclass for more information."""

    def __init__(self, env_params, sim_params, network, simulator):
        super().__init__(env_params, sim_params, network, simulator)
        self.max_lanes = 1

    def step(self, rl_actions):
        obs, rew, done, info = super().step(rl_actions)
        mean_speed = np.nan_to_num(np.mean(self.k.vehicle.get_speed(self.k.vehicle.get_ids())))
        if self.env_params.additional_params['terminate_on_wave'] and \
            mean_speed < self.env_params.additional_params['wave_termination_speed'] \
            and self.time_counter > self.env_params.additional_params['wave_termination_horizon'] \
                and len(self.k.vehicle.get_ids()) > 0:
            done = True

        return obs, rew, done, info
