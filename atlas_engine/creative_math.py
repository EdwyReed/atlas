"""Bounded graph exploration over opaque integer IDs and numerical evidence.

No vocabulary, categories, strings, policy, I/O or application imports. Stable
IDs are the tie-break identity: renaming display labels cannot change results.
"""
from dataclasses import dataclass
import hashlib
import math


@dataclass(frozen=True)
class Edge:
    source: int
    target: int
    support: float = 0.0  # conditional support in the exploration direction
    pair_count: int = 0
    pmi: float = 0.0
    frequency: int = 0


@dataclass(frozen=True)
class Step:
    node: int
    utility: float
    ancestry: tuple[int, ...]
    evidence: tuple[tuple[int, Edge], ...]


@dataclass(frozen=True)
class Path:
    nodes: tuple[int, ...]
    steps: tuple[Step, ...] = ()
    utility: float = 0.0


class Canceled(Exception):
    pass


def weight(edge):
    """Uncalibrated utility; source score is deliberately not reinterpreted."""
    support = max(0.0, min(1.0, edge.support))
    pmi = max(0.0, min(8.0, edge.pmi))
    frequency = max(0, edge.frequency)
    pairs = max(0, edge.pair_count)
    # Pair evidence grows sublinearly; marginal popularity discounts generic hubs.
    return (support * math.log1p(pairs) / 10 + pmi * .08) / (1 + math.log1p(frequency) * .04)


def _noise(seed, step, ancestry, node):
    payload = repr((seed, step, tuple(sorted(ancestry)), node)).encode('ascii')
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), 'big') / 2**64


def choose_start(nodes, seed):
    """Uniform seeded start over observed opaque IDs, independent of labels.

    The provider supplies the finite corpus population. This is a proposal prior,
    not compatibility evidence or a calibrated distribution of scenes.
    """
    population = tuple(sorted(set(nodes)))
    if not population:
        return None
    return population[min(len(population)-1, int(_noise(seed, 0, (), 0)*len(population)))]


def explore(anchors, adjacent, *, seed=0, budget=10, width=6, branch=3,
            one_hop=False, cancel=lambda: False, max_nodes=4096):
    """adjacent(id) yields (neighbor_id, Edge), already admitted by the caller.

    Traversal may use either direction, but Edge endpoints retain source direction.
    Each Step's evidence is local to its ancestry, never another beam branch.
    Duplicate observations and duplicate beam states cannot multiply support.
    """
    if not 0 <= budget <= 24 or not 1 <= width <= 6 or not 1 <= branch <= 3:
        raise ValueError('search bounds exceeded')
    if not 1 <= max_nodes <= 4096:
        raise ValueError('node bound exceeded')
    anchors = tuple(sorted(set(anchors)))
    if len(anchors) > 128:
        raise ValueError('anchor bound exceeded')
    cache = {}

    def check():
        if cancel():
            raise Canceled()

    def rows(node):
        check()
        if node not in cache:
            if len(cache) >= max_nodes:
                return ()
            # The application provider is bounded too; avoid trusting infinite input.
            found = set()
            for index, (target, edge) in enumerate(adjacent(node)):
                check()
                if index >= 200:
                    raise ValueError('adjacency bound exceeded')
                if edge.source != node and edge.target != node:
                    raise ValueError('nonincident evidence')
                other = edge.target if edge.source == node else edge.source
                if target != other:
                    raise ValueError('invalid neighbor endpoint')
                found.add((target, edge))
            cache[node] = tuple(sorted(found, key=lambda x: (x[0], x[1].source, x[1].target,
                                                          x[1].support, x[1].pair_count, x[1].pmi, x[1].frequency)))
        return cache[node]

    check()
    beam = [Path(anchors)]
    for depth in range(budget):
        check()
        expanded = {}
        for path in beam:
            evidence = {}
            for anchor in (anchors if one_hop else sorted(path.nodes)):
                for node, edge in rows(anchor):
                    if node not in path.nodes and weight(edge) > 0:
                        evidence.setdefault(node, set()).add((anchor, edge))
            ranked = []
            for node, observations in evidence.items():
                # One edge seen from two incident anchors must not double its weight.
                unique = {edge for _, edge in observations}
                score = math.fsum(sorted(weight(edge) for edge in unique))
                value = score / (1 + .15 * len(path.steps))
                rank = value * (.75 + .5 * _noise(seed, depth, path.nodes, node))
                ranked.append((rank, node, value, tuple(sorted(observations, key=lambda x: (x[0], x[1].source, x[1].target)))))
            ranked.sort(key=lambda x: (-x[0], x[1]))
            successors = []
            for _, node, value, observations in ranked[:branch]:
                step = Step(node, value, path.nodes, observations)
                successors.append(Path(path.nodes + (node,), path.steps + (step,), path.utility + value))
            for candidate in successors or [path]:
                key = tuple(sorted(candidate.nodes))
                prior = expanded.get(key)
                if prior is None or (-candidate.utility, candidate.nodes) < (-prior.utility, prior.nodes):
                    expanded[key] = candidate
        beam = sorted(expanded.values(), key=lambda x: (-x.utility, x.nodes))[:width]
        if all(len(path.steps) <= depth for path in beam):
            break
    check()
    return beam[0]


def diverse(paths, count, *, cancel=lambda: False):
    """MMR over additions, not shared mandatory anchors; exact duplicates collapse."""
    pool = {}
    for index, path in enumerate(paths):
        key = frozenset(path.nodes)
        if key not in pool or path.utility > pool[key][1].utility:
            pool[key] = (index, path)
    selected = []
    while pool and len(selected) < count:
        if cancel():
            raise Canceled()
        ranked = []
        for key, (index, path) in pool.items():
            additions = {s.node for s in path.steps}
            similarity = max((len(additions & other) / max(1, len(additions | other))
                              for _, _, _, other in selected), default=0.0)
            utility = path.utility / max(1, len(path.steps)) - 2 * similarity
            ranked.append((utility, index, key, path, additions))
        utility, index, key, path, additions = min(ranked, key=lambda x: (-x[0], x[1]))
        selected.append((index, path, utility, additions))
        del pool[key]
    return [(index, path, utility) for index, path, utility, _ in selected]
