import torch
import torch.optim.optimizer
#from utils.tensor import ManifoldParameter, ManifoldTensor
from manifolds import Euclidean, ManifoldParameter

_default_manifold = Euclidean()


class OptimMixin(object):
    _default_manifold = Euclidean()

    def __init__(self, *args, stabilize=None, **kwargs):
        self._stabilize = stabilize
        super().__init__(*args, **kwargs)

    def add_param_group(self, param_group: dict):
        param_group.setdefault("stabilize", self._stabilize)
        return super().add_param_group(param_group)

    def stabilize_group(self, group):
        pass

    def stabilize(self):
        """Stabilize parameters if they are off-manifold due to numerical reasons."""
        for group in self.param_groups:
            self.stabilize_group(group)


class RiemannianSGD(OptimMixin, torch.optim.Optimizer):
    r"""
    Riemannian Stochastic Gradient Descent with the same API as :class:`torch.optim.SGD`.
    Parameters
    ----------
    params : iterable
        iterable of parameters to optimize or dicts defining
        parameter groups
    lr : float
        learning rate
    momentum : float (optional)
        momentum factor (default: 0)
    weight_decay : float (optional)
        weight decay (L2 penalty) (default: 0)
    dampening : float (optional)
        dampening for momentum (default: 0)
    nesterov : bool (optional)
        enables Nesterov momentum (default: False)
    Other Parameters
    ----------------
    stabilize : int
        Stabilize parameters if they are off-manifold due to numerical
        reasons every ``stabilize`` steps (default: ``None`` -- no stabilize)
    """

    def __init__(
        self,
        params,
        lr,
        momentum=0,
        dampening=0,
        weight_decay=0,
        nesterov=False,
        stabilize=None,
    ):
        if lr < 0.0:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if momentum < 0.0:
            raise ValueError("Invalid momentum value: {}".format(momentum))
        if weight_decay < 0.0:
            raise ValueError("Invalid weight_decay value: {}".format(weight_decay))

        defaults = dict(
            lr=lr,
            momentum=momentum,
            dampening=dampening,
            weight_decay=weight_decay,
            nesterov=nesterov,
        )
        if nesterov and (momentum <= 0 or dampening != 0):
            raise ValueError("Nesterov momentum requires a momentum and zero dampening")
        super().__init__(params, defaults, stabilize=stabilize)

    def step(self, closure=None):
        loss = None
        if closure is not None:
            loss = closure()
        with torch.no_grad():
            for group in self.param_groups:
                if "step" not in group:
                    group["step"] = 0
                weight_decay = group["weight_decay"]
                momentum = group["momentum"]
                dampening = group["dampening"]
                nesterov = group["nesterov"]
                learning_rate = group["lr"]
                group["step"] += 1
                for point in group["params"]:
                    grad = point.grad
                    if grad is None:
                        continue
                    if grad.is_sparse:
                        raise RuntimeError(
                            "RiemannianSGD does not support sparse gradients, use SparseRiemannianSGD instead"
                        )
                    state = self.state[point]

                    # State initialization
                    if len(state) == 0:
                        if momentum > 0:
                            state["momentum_buffer"] = grad.clone()
                    if isinstance(point, ManifoldParameter):
                        manifold = point.manifold
                        c = point.c
                    else:
                        manifold = _default_manifold
                        c = None

                    grad.add_(point, alpha=weight_decay)
                    grad = manifold.egrad2rgrad(point, grad, c)
                    # TODO( ): this part does not work, fix it.
                    if momentum > 0:
                        momentum_buffer = state["momentum_buffer"]
                        momentum_buffer.mul_(momentum).add_(grad, alpha=1 - dampening)
                        if nesterov:
                            grad = grad.add_(momentum_buffer, alpha=momentum)
                        else:
                            grad = momentum_buffer
                        # we have all the things projected
                        new_point, new_momentum_buffer = manifold.retr_transp(
                            point, -learning_rate * grad, momentum_buffer
                        )
                        momentum_buffer.copy_(new_momentum_buffer)
                        # use copy only for user facing point
                        point.copy_(new_point)
                    else:
                        new_point = manifold.retr(point, -learning_rate * grad)
                        point.copy_(new_point)

                if (
                    group["stabilize"] is not None
                    and group["step"] % group["stabilize"] == 0
                ):
                    self.stabilize_group(group)
        return loss

    @torch.no_grad()
    def stabilize_group(self, group):
        for p in group["params"]:
            if not isinstance(p, ManifoldParameter):
                continue
            manifold = p.manifold
            momentum = group["momentum"]
            p.copy_(manifold.projx(p))
            if momentum > 0:
                param_state = self.state[p]
                if not param_state:  # due to None grads
                    continue
                if "momentum_buffer" in param_state:
                    buf = param_state["momentum_buffer"]
                    buf.copy_(manifold.proju(p, buf))