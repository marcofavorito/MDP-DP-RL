from processes.mdp_refined import MDPRefined
from processes.det_policy import DetPolicy
from typing import List, Mapping, Tuple, NoReturn


def get_lily_pads_mdp(n: int) -> MDPRefined:
    data = {
        i: {
            'A': {
                i - 1: (i / n, 0.),
                i + 1: (1. - i / n, 1. if i == n - 1 else 0.)
            },
            'B': {
                j: (1 / n, 1. if j == n else 0.)
                for j in range(n + 1) if j != i
            }
        } for i in range(1, n)
    }
    data[0] = {'A': {0: (1., 0.)}, 'B': {0: (1., 0.)}}
    data[n] = {'A': {n: (1., 0.)}, 'B': {n: (1., 0.)}}

    gamma = 1.0
    return MDPRefined(data, gamma)


def get_sorted_q_val(
    q_val: Mapping[int, Mapping[int, float]]
) -> List[Tuple[float, float]]:
    d = sorted([(s, (t['A'], t['B'])) for s, t in q_val.items()], key=lambda x: x[0])
    return [z for _, z in d[1:-1]]


def graph_q_func(a: List[Tuple[float, float]]) -> NoReturn:
    import matplotlib.pyplot as plt
    x_vals = range(1, len(a) + 1)
    plt.plot(x_vals, [x for x, _ in a], "r", label="Q* for Action A")
    plt.plot(x_vals, [y for _, y in a], "b", label="Q* for Action B")
    plt.xlabel("Lilypad Number")
    plt.ylabel("Value")
    plt.title("Optimal Action Value Function")
    plt.xlim(xmin=x_vals[0], xmax=x_vals[-1])
    plt.ylim(ymin=0.6, ymax=0.8)
    plt.xticks(x_vals)
    plt.grid(True)
    plt.legend(loc='lower right')
    plt.show()


if __name__ == '__main__':
    pads: int = 10
    mdp: MDPRefined = get_lily_pads_mdp(pads)
    pol = mdp.get_optimal_policy(1e-8)
    print(pol.policy_data)
    print(mdp.get_value_func_dict(pol))
    qv = mdp.get_act_value_func_dict(pol)
    graph_q_func(get_sorted_q_val(qv))
