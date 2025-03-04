from typing import Callable, Sequence, Tuple, Set, Optional
import numpy as np
from mdp_dp_rl.algorithms.td_algo_enum import TDAlgorithm
from mdp_dp_rl.algorithms.rl_func_approx.monte_carlo import MonteCarlo
from mdp_dp_rl.algorithms.rl_func_approx.td0 import TD0
from mdp_dp_rl.algorithms.rl_func_approx.tdlambda import TDLambda
from mdp_dp_rl.algorithms.rl_func_approx.tdlambda_exact import TDLambdaExact
from mdp_dp_rl.algorithms.rl_func_approx.lspi import LSPI
from mdp_dp_rl.examples.american_pricing.num_utils import get_future_price_mean_var
from mdp_dp_rl.processes.mdp_rep_for_rl_fa import MDPRepForRLFA
from mdp_dp_rl.algorithms.func_approx_spec import FuncApproxSpec
from mdp_dp_rl.examples.american_pricing.grid_pricing import GridPricing
from mdp_dp_rl.func_approx.dnn_spec import DNNSpec
from mdp_dp_rl.utils.gen_utils import memoize
from random import choice
# import matplotlib.pyplot as plt
from pathlib import Path

StateType = Tuple[int, np.ndarray]
ActionType = bool
ALMOSTONEGAMMA = 1. - 1e4


class AmericanPricing:
    """
        In the risk-neutral measure, the underlying price x_t
        follows the Ito process: dx_t = r(t) x_t dt + dispersion(t, x_t) dz_t
        spot_price is x_0
        In this module, we only allow two types of dispersion functions,
        Type 1 (a.k.a. "lognormal") : dx_t = r(t) x_t dt + sigma(t) x_t dz_t
        Type 2 (a.k.a. "normal"): dx_t = r(t) x_t dt + sigma(t) dz_t
        payoff is a function from (t, x_t) to payoff (eg: x_t - K)
        expiry is the time to expiry of american option (in years)
        lognormal is a bool that defines whether our dispersion function
        amounts to Type 1(lognormal) or Type 2(normal)
        r(t) is a function from (time t) to risk-free rate
        sigma(t) is a function from (time t) to (sigma at time t)
        We don't provide r(t) and sigma(t) as arguments
        Instead we provide their appropriate integrals as arguments
        Specifically, we provide ir(t) and isig(t) as arguments (as follows):
        ir(t) = \int_0^t r(u) du, so discount D_t = e^{- ir(t)}
        isig(t) = \int 0^t sigma^2(u) du if lognormal == True
        else \int_0^t sigma^2(u) e^{-\int_0^u 2 r(s) ds} du
    """

    def __init__(
        self,
        spot_price: float,
        payoff: Callable[[float, np.ndarray], float],
        expiry: float,
        lognormal: bool,
        ir: Callable[[float], float],
        isig: Callable[[float], float]
    ) -> None:
        self.spot_price: float = spot_price
        self.payoff: Callable[[float, np.ndarray], float] = payoff
        self.expiry: float = expiry
        self.lognormal: bool = lognormal
        self.ir: Callable[[float], float] = ir
        self.isig: Callable[[float], float] = isig

    @memoize
    def get_all_paths(
        self,
        spot_pct_noise: float,
        num_paths: int,
        num_dt: int
    ) -> np.ndarray:
        dt = self.expiry / num_dt
        paths = np.empty([num_paths, num_dt + 1])
        spot = self.spot_price
        for i in range(num_paths):
            start = max(0.001, np.random.normal(spot, spot * spot_pct_noise))
            paths[i, 0] = start
            for t in range(num_dt):
                m, v = get_future_price_mean_var(
                    paths[i, t],
                    t,
                    dt,
                    self.lognormal,
                    self.ir,
                    self.isig
                )
                norm_draw = np.random.normal(m, np.sqrt(v))
                paths[i, t + 1] = np.exp(norm_draw) if self.lognormal else norm_draw
        return paths

    def get_ls_price(
        self,
        num_dt: int,
        num_paths: int,
        feature_funcs: Sequence[Callable[[int, np.ndarray], float]]
    ) -> float:
        paths = self.get_all_paths(0.0, num_paths, num_dt)
        cashflow = np.array([max(self.payoff(self.expiry, paths[i, :]), 0.)
                             for i in range(num_paths)])
        dt = self.expiry / num_dt

        stprcs = np.arange(100.)
        final = [(p, self.payoff(self.expiry, np.append(np.zeros(num_dt), p))) for p in stprcs]
        ex_boundary = [max(p for p, e in final if e > 0)]

        for step in range(num_dt - 1, 0, -1):
            """
            For each time slice t
            Step 1: collect X as features of (t, [S_0,.., S_t]) for those paths
            for which payoff(t, [S_0, ...., S_t]) > 0, and corresponding Y as
            the time-t discounted future actual cash flow on those paths.
            Step 2: Do the (X,Y) regression. Denote Y^ as regression-prediction.
            Compare Y^ versus payoff(t, [S_0, ..., S_t]). If payoff is higher,
            set cashflow at time t on that path to be the payoff, else set 
            cashflow at time t on that path to be the time-t discounted future
            actual cash flow on that path.
            """
            t = step * dt
            disc = np.exp(self.ir(t) - self.ir(t + dt))
            cashflow = cashflow * disc
            payoff = np.array([self.payoff(t, paths[i, :(step + 1)]) for
                               i in range(num_paths)])
            indices = [i for i in range(num_paths) if payoff[i] > 0]
            if len(indices) > 0:
                x_vals = np.array([[f(step, paths[i, :(step + 1)]) for f in
                                    feature_funcs] for i in indices])
                y_vals = np.array([cashflow[i] for i in indices])
                weights = np.linalg.lstsq(x_vals, y_vals, rcond=None)[0]
                estimate = x_vals.dot(weights)
                # plt.scatter([paths[i, t] for i in indices], y_vals, c='r')
                # plt.scatter([paths[i, t] for i in indices], estimate, c='b')
                # plt.show()

                for i, ind in enumerate(indices):
                    if payoff[ind] > estimate[i]:
                        cashflow[ind] = payoff[ind]

                prsqs = [np.append(np.zeros(step), s) for s in stprcs]
                cp = [weights.dot([f(step, prsq) for f in feature_funcs]) for prsq in prsqs]
                ep = [self.payoff(t, prsq) for prsq in prsqs]
                ll = [p for p, c, e in zip(stprcs, cp, ep) if e > c]
                if len(ll) == 0:
                    num = 0.
                else:
                    num = max(ll)
                ex_boundary.append(num)
                # if step == int(num_dt / 10) or step == num_dt - int(num_dt / 10) \
                #         or step == int(num_dt / 2):
                #     plt.title("LS Time = %.3f" % t)
                #     plt.plot(stprcs, cp, 'r', stprcs, ep, 'b')
                #     plt.show()

        # plt.plot([t * dt for t in range(1, num_dt + 1)], ex_boundary[::-1])
        # plt.title("LS Boundary")
        # plt.savefig(str(Path.home()) + "/Downloads/LSBoundary.png")

        return max(
            self.payoff(0., np.array([self.spot_price])),
            np.average(cashflow * np.exp(-self.ir(dt)))
        )


    def state_reward_gen(
        self,
        state: StateType,
        action: ActionType,
        num_dt: int
    ) -> Tuple[StateType, float]:
        ind, price_arr = state
        delta_t = self.expiry / num_dt
        t = ind * delta_t
        reward = (np.exp(-self.ir(t)) * self.payoff(t, price_arr)) if\
            (action and ind <= num_dt) else 0.
        m, v = get_future_price_mean_var(
            price_arr[-1],
            t,
            delta_t,
            self.lognormal,
            self.ir,
            self.isig
        )
        norm_draw = np.random.normal(m, np.sqrt(v))
        next_price = np.exp(norm_draw) if self.lognormal else norm_draw
        price1 = np.append(price_arr, next_price)
        next_ind = (num_dt if action else ind) + 1
        return (next_ind, price1), reward

    def get_rl_fa_price(
        self,
        num_dt: int,
        method: str,
        exploring_start: bool,
        algorithm: TDAlgorithm,
        softmax: bool,
        epsilon: float,
        epsilon_half_life: float,
        lambd: float,
        num_paths: int,
        batch_size: int,
        feature_funcs: Sequence[Callable[[Tuple[StateType, ActionType]], float]],
        neurons: Optional[Sequence[int]],
        learning_rate: float,
        learning_rate_decay: float,
        adam: Tuple[bool, float, float],
        offline: bool
    ) -> float:
        dt = self.expiry / num_dt

        def sa_func(_: StateType) -> Set[ActionType]:
            return {True, False}

        # noinspection PyShadowingNames
        def terminal_state(
            s: StateType,
            num_dt=num_dt
        ) -> bool:
            return s[0] > num_dt

        # noinspection PyShadowingNames
        def sr_func(
            s: StateType,
            a: ActionType,
            num_dt=num_dt
        ) -> Tuple[StateType, float]:
            return self.state_reward_gen(s, a, num_dt)

        def init_s() -> StateType:
            return 0, np.array([self.spot_price])

        def init_sa() -> Tuple[StateType, ActionType]:
            return init_s(), choice([True, False])

        # noinspection PyShadowingNames
        mdp_rep_obj = MDPRepForRLFA(
            state_action_func=sa_func,
            gamma=ALMOSTONEGAMMA,
            terminal_state_func=terminal_state,
            state_reward_gen_func=sr_func,
            init_state_gen=init_s,
            init_state_action_gen=init_sa
        )

        fa_spec = FuncApproxSpec(
            state_feature_funcs=[],
            sa_feature_funcs=feature_funcs,
            dnn_spec=(None if neurons is None else (DNNSpec(
                neurons=neurons,
                hidden_activation=DNNSpec.log_squish,
                hidden_activation_deriv=DNNSpec.log_squish_deriv,
                output_activation=DNNSpec.pos_log_squish,
                output_activation_deriv=DNNSpec.pos_log_squish_deriv
            ))),
            learning_rate=learning_rate,
            adam_params=adam,
            add_unit_feature=False
        )

        if method == "MC":
            rl_fa_obj = MonteCarlo(
                mdp_rep_for_rl=mdp_rep_obj,
                exploring_start=exploring_start,
                softmax=softmax,
                epsilon=epsilon,
                epsilon_half_life=epsilon_half_life,
                num_episodes=num_paths,
                max_steps=num_dt + 2,
                fa_spec=fa_spec
            )
        elif method == "TD0":
            rl_fa_obj = TD0(
                mdp_rep_for_rl=mdp_rep_obj,
                exploring_start=exploring_start,
                algorithm=algorithm,
                softmax=softmax,
                epsilon=epsilon,
                epsilon_half_life=epsilon_half_life,
                num_episodes=num_paths,
                max_steps=num_dt + 2,
                fa_spec=fa_spec
            )
        elif method == "TDL":
            rl_fa_obj = TDLambda(
                mdp_rep_for_rl=mdp_rep_obj,
                exploring_start=exploring_start,
                algorithm=algorithm,
                softmax=softmax,
                epsilon=epsilon,
                epsilon_half_life=epsilon_half_life,
                lambd=lambd,
                num_episodes=num_paths,
                batch_size=batch_size,
                max_steps=num_dt + 2,
                fa_spec=fa_spec,
                offline=offline
            )
        elif method == "TDE":
            rl_fa_obj = TDLambdaExact(
                mdp_rep_for_rl=mdp_rep_obj,
                exploring_start=exploring_start,
                algorithm=algorithm,
                softmax=softmax,
                epsilon=epsilon,
                epsilon_half_life=epsilon_half_life,
                lambd=lambd,
                num_episodes=num_paths,
                batch_size=batch_size,
                max_steps=num_dt + 2,
                state_feature_funcs=[],
                sa_feature_funcs=feature_funcs,
                learning_rate=learning_rate,
                learning_rate_decay=learning_rate_decay
            )
        else:
            rl_fa_obj = LSPI(
                mdp_rep_for_rl=mdp_rep_obj,
                exploring_start=exploring_start,
                softmax=softmax,
                epsilon=epsilon,
                epsilon_half_life=epsilon_half_life,
                num_episodes=num_paths,
                batch_size=batch_size,
                max_steps=num_dt + 2,
                state_feature_funcs=[],
                sa_feature_funcs=feature_funcs
            )

        qvf = rl_fa_obj.get_qv_func_fa(None)
        # init_s = (0, np.array([self.spot_price]))
        # val_exec = qvf(init_s)(True)
        # val_cont = qvf(init_s)(False)
        # true_false_spot_max = max(val_exec, val_cont)

        all_paths = self.get_all_paths(0.0, num_paths, num_dt)
        prices = np.zeros(num_paths)

        for path_num, path in enumerate(all_paths):
            steps = 0
            while steps <= num_dt:
                price_seq = path[:(steps + 1)]
                state = (steps, price_seq)
                exercise_price = np.exp(-self.ir(dt * steps)) *\
                    self.payoff(dt * steps, price_seq)
                continue_price = qvf(state)(False)
                steps += 1
                if exercise_price > continue_price:
                    prices[path_num] = exercise_price
                    steps = num_dt + 1
                    # print(state)
                    # print(exercise_price)
                    # print(continue_price)
                    # print(qvf(state)(True))

        return np.average(prices)

    def get_price_from_paths_and_params(
        self,
        paths: np.ndarray,
        params: np.ndarray,
        num_dt: int,
        feature_funcs: Sequence[Callable[[int, np.ndarray], float]]
    ) -> float:
        num_paths = paths.shape[0]
        prices = np.zeros(num_paths)
        dt = self.expiry / num_dt
        for path_num, path in enumerate(paths):
            step = 0
            while step <= num_dt:
                t = dt * step
                price_seq = path[:(step + 1)]
                exercise_price = self.payoff(t, price_seq)
                if step == num_dt:
                    continue_price = 0.
                else:
                    continue_price = params.dot([f(step, price_seq) for f in
                                                 feature_funcs])
                step += 1
                if exercise_price > continue_price:
                    prices[path_num] = np.exp(-self.ir(t)) * exercise_price
                    step = num_dt + 1

                # if step == num_dt + 1:
                #     print("Time = %.2f, Stock = %.3f, Exercise Price = %.3f, Continue Price = %.3f" %
                #           (t, stock_price, exercise_price, continue_price))


        # ex_boundary = []
        # stprcs = np.arange(100.)
        # for step in range(num_dt):
        #     t = dt * step
        #     prsqs = [np.append(np.zeros(step), s) for s in stprcs]
        #     cp = [params.dot([f(step, prsq) for f in feature_funcs]) for prsq in prsqs]
        #     ep = [self.payoff(t, prsq) for prsq in prsqs]
        #     ll = [p for p, c, e in zip(stprcs, cp, ep) if e > c]
        #     if len(ll) == 0:
        #         num = 0.
        #     else:
        #         num = max(ll)
        #     ex_boundary.append(num)
        #     if step == int(num_dt / 10) or step == num_dt - int(num_dt / 10)\
        #             or step == int(num_dt / 2):
        #         plt.title("Time = %.3f" % t)
        #         plt.plot(stprcs, cp, 'r', stprcs, ep, 'b')
        #         plt.show()
        # final = [(p, self.payoff(self.expiry, np.append(np.zeros(num_dt), p))) for p in stprcs]
        # ex_boundary.append(max(p for p, e in final if e > 0))
        # plt.plot([t * dt for t in range(num_dt + 1)], ex_boundary)
        # plt.title("LSPI Boundary")
        # plt.savefig(str(Path.home()) + "/Downloads/LSPIBoundary.png")

        return np.average(prices)

    def get_lspi_price(
        self,
        num_dt: int,
        num_paths: int,
        feature_funcs: Sequence[Callable[[int, np.ndarray], float]],
        num_iters: int,
        epsilon: float,
        spot_pct_noise: float
    ) -> float:
        features = len(feature_funcs)
        params = np.zeros(features)
        paths = self.get_all_paths(spot_pct_noise, num_paths, num_dt)
        iter_steps = num_paths * num_dt
        dt = self.expiry / num_dt

        for _ in range(num_iters):
            a_mat = np.zeros((features, features))
            b_vec = np.zeros(features)

            for path_num, path in enumerate(paths):

                for step in range(num_dt):
                    t = step * dt
                    disc = np.exp(self.ir(t) - self.ir(t + dt))
                    phi_s = np.array([f(step, path[:(step + 1)]) for f in
                                      feature_funcs])
                    local_path = path[:(step + 2)]
                    phi_sp = np.zeros(features)
                    reward = 0.
                    next_payoff = self.payoff(t + dt, local_path)

                    if step == num_dt - 1:
                        reward = next_payoff
                    else:
                        next_phi = np.array([f(step + 1, local_path)
                                             for f in feature_funcs])
                        if next_payoff > params.dot(next_phi):
                            reward = next_payoff
                        else:
                            phi_sp = next_phi

                    a_mat += np.outer(
                        phi_s,
                        phi_s - phi_sp * disc
                    )
                    b_vec += reward * disc * phi_s

            a_mat /= iter_steps
            a_mat += epsilon * np.eye(features)
            b_vec /= iter_steps
            params = np.linalg.inv(a_mat).dot(b_vec)
            # print(params)

        return self.get_price_from_paths_and_params(
            self.get_all_paths(0.0, num_paths, num_dt),
            params,
            num_dt,
            feature_funcs
        )

    def get_fqi_price(
        self,
        num_dt: int,
        num_paths: int,
        feature_funcs: Sequence[Callable[[int, np.ndarray], float]],
        num_iters: int,
        epsilon: float,
        spot_pct_noise: float
    ) -> float:
        features = len(feature_funcs)
        params = np.zeros(features)
        paths = self.get_all_paths(spot_pct_noise, num_paths, num_dt)
        iter_steps = num_paths * num_dt
        dt = self.expiry / num_dt

        for _ in range(num_iters):
            a_mat = np.zeros((features, features))
            b_vec = np.zeros(features)

            for path_num, path in enumerate(paths):

                for step in range(num_dt):
                    t = step * dt
                    disc = np.exp(self.ir(t) - self.ir(t + dt))
                    phi_s = np.array([f(step, path[:(step + 1)]) for f in
                                      feature_funcs])
                    local_path = path[:(step + 2)]

                    next_payoff = self.payoff(t + dt, local_path)
                    if step == num_dt - 1:
                        max_val = next_payoff
                    else:
                        next_phi = np.array([f(step + 1, local_path)
                                             for f in feature_funcs])
                        max_val = max(next_payoff, params.dot(next_phi))

                    a_mat += np.outer(phi_s, phi_s)
                    b_vec += phi_s * disc * max_val

            a_mat /= iter_steps
            a_mat += epsilon * np.eye(features)
            b_vec /= iter_steps
            params = np.linalg.inv(a_mat).dot(b_vec)
            # print(params)

        return self.get_price_from_paths_and_params(
            self.get_all_paths(0.0, num_paths, num_dt),
            params,
            num_dt,
            feature_funcs
        )


if __name__ == '__main__':
    is_call_val = False
    spot_price_val = 80.0
    strike_val = 80.0
    expiry_val = 1.0
    lognormal_val = True
    r_val = 0.03
    sigma_val = 0.3
    num_dt_val = 100
    num_paths_val = 10000
    num_laguerre_val = 3
    num_iters_val = 15
    epsilon_val = 1e-3
    spot_pct_noise_val = 0.25

    from mdp_dp_rl.examples.american_pricing.bs_pricing import EuropeanBSPricing
    ebsp = EuropeanBSPricing(
        is_call=is_call_val,
        spot_price=spot_price_val,
        strike=strike_val,
        expiry=expiry_val,
        r=r_val,
        sigma=sigma_val
    )
    print("European Price = %.3f" % ebsp.option_price)

    # noinspection PyShadowingNames
    ir_func = lambda t, r_val=r_val: r_val * t
    # noinspection PyShadowingNames
    isig_func = lambda t, sigma_val=sigma_val: sigma_val * sigma_val * t

    def vanilla_american_payoff(_: float, x: np.ndarray) -> float:
        if is_call_val:
            ret = max(x[-1] - strike_val, 0.)
        else:
            ret = max(strike_val - x[-1], 0.)
        return ret

    # noinspection PyShadowingNames
    amp = AmericanPricing(
        spot_price=spot_price_val,
        payoff=lambda t, x: vanilla_american_payoff(t, x),
        expiry=expiry_val,
        lognormal=lognormal_val,
        ir=ir_func,
        isig=isig_func
    )

    ident = np.eye(num_laguerre_val)

    from numpy.polynomial.laguerre import lagval

    # noinspection PyShadowingNames
    def laguerre_feature_func(
        x: float,
        i: int
    ) -> float:
        # noinspection PyTypeChecker
        xp = x / strike_val
        return np.exp(-xp / 2) * lagval(xp, ident[i])

    def rl_feature_func(
        ind: int,
        x: float,
        i: int
    ) -> float:
        dt = expiry_val / num_dt_val
        t = ind * dt
        if i == 0:
            ret = 1.
        elif i < num_laguerre_val + 1:
            ret = laguerre_feature_func(x, i - 1)
        elif i == num_laguerre_val + 1:
            ret = np.sin(-t * np.pi / (2. * expiry_val) + np.pi / 2.)
        elif i == num_laguerre_val + 2:
            ret = np.log(expiry_val - t)
        else:
            rat = t / expiry_val
            ret = rat * rat
        return ret

    lspi_price = amp.get_lspi_price(
        num_dt=num_dt_val,
        num_paths=num_paths_val,
        feature_funcs=[lambda t, x, i=i: rl_feature_func(t, x[-1], i) for i in
                       range(num_laguerre_val + 4)],
        num_iters=num_iters_val,
        epsilon=epsilon_val,
        spot_pct_noise=spot_pct_noise_val
    )
    print("LSPI Price = %.3f" % lspi_price)

    fqi_price = amp.get_fqi_price(
        num_dt=num_dt_val,
        num_paths=num_paths_val,
        feature_funcs=[lambda t, x, i=i: rl_feature_func(t, x[-1], i) for i in
                       range(num_laguerre_val + 4)],
        num_iters=num_iters_val,
        epsilon=epsilon_val,
        spot_pct_noise=spot_pct_noise_val
    )
    print("FQI Price = %.3f" % fqi_price)

    ls_price = amp.get_ls_price(
        num_dt=num_dt_val,
        num_paths=num_paths_val,
        feature_funcs=[lambda _, x: 1.] +
                      [(lambda _, x, i=i: laguerre_feature_func(x[-1], i)) for i in
                       range(num_laguerre_val)]
    )
    print("Longstaff-Schwartz Price = %.3f" % ls_price)

    expiry_mean, expiry_var = get_future_price_mean_var(
        spot_price_val,
        0.,
        expiry_val,
        lognormal_val,
        ir_func,
        isig_func
    )

    grid_price = GridPricing(
        spot_price=spot_price_val,
        payoff=lambda _, x: (1. if is_call_val else -1.) * (x - strike_val),
        expiry=expiry_val,
        lognormal=lognormal_val,
        ir=ir_func,
        isig=isig_func
    ).get_price(
        num_dt=num_dt_val,
        num_dx=100,
        center=expiry_mean,
        width=np.sqrt(expiry_var) * 4.
    )

    print("Grid Price = %.3f" % grid_price)

    print("European Price = %.3f" % ebsp.option_price)
