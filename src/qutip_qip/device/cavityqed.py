import warnings
from copy import deepcopy

import numpy as np

from qutip import (
    tensor,
    identity,
    destroy,
    sigmax,
    sigmaz,
    basis,
    Qobj,
    QobjEvo,
)
from ..circuit import QubitCircuit
from ..operations import Gate
from .processor import Processor, Model
from .modelprocessor import ModelProcessor, _to_array
from ..operations import expand_operator
from ..pulse import Pulse
from ..compiler import GateCompiler, CavityQEDCompiler


__all__ = ["DispersiveCavityQED"]


class DispersiveCavityQED(ModelProcessor):
    """
    The processor based on the physical implementation of
    a dispersive cavity QED system.
    The available Hamiltonian of the system is predefined.
    For a given pulse amplitude matrix, the processor can
    calculate the state evolution under the given control pulse,
    either analytically or numerically.
    (Only additional attributes are documented here, for others please
    refer to the parent class :class:`.ModelProcessor`)

    Parameters
    ----------
    num_qubits: int
        The number of qubits in the system.

    num_levels: int, optional
        The number of energy levels in the resonator.

    correct_global_phase: float, optional
        Save the global phase, the analytical solution
        will track the global phase.
        It has no effect on the numerical solution.

    **params:
        Hardware parameters. See :obj:`CavityQEDModel`.
    """

    def __init__(
        self, num_qubits, num_levels=10, correct_global_phase=True, **params
    ):
        model = CavityQEDModel(
            num_qubits=num_qubits,
            num_levels=num_levels,
            **params,
        )
        super(DispersiveCavityQED, self).__init__(
            model=model, correct_global_phase=correct_global_phase
        )
        self.correct_global_phase = correct_global_phase
        self.num_levels = num_levels
        self.native_gates = ["SQRTISWAP", "ISWAP", "RX", "RZ"]
        self.spline_kind = "step_func"

    @property
    def sx_ops(self):
        """
        list: A list of sigmax Hamiltonians for each qubit.
        """
        return self.ctrls[0 : self.num_qubits]

    @property
    def sz_ops(self):
        """
        list: A list of sigmaz Hamiltonians for each qubit.
        """
        return self.ctrls[self.num_qubits : 2 * self.num_qubits]

    @property
    def cavityqubit_ops(self):
        """
        list: A list of interacting Hamiltonians between cavity and each qubit.
        """
        return self.ctrls[2 * self.num_qubits : 3 * self.num_qubits]

    @property
    def sx_u(self):
        """array-like: Pulse matrix for sigmax Hamiltonians."""
        return self.coeffs[: self.num_qubits]

    @property
    def sz_u(self):
        """array-like: Pulse matrix for sigmaz Hamiltonians."""
        return self.coeffs[self.num_qubits : 2 * self.num_qubits]

    @property
    def g_u(self):
        """
        array-like: Pulse matrix for interacting Hamiltonians
        between cavity and each qubit.
        """
        return self.coeffs[2 * self.num_qubits : 3 * self.num_qubits]

    def eliminate_auxillary_modes(self, U):
        """
        Eliminate the auxillary modes like the cavity modes in cqed.
        """
        psi_proj = tensor(
            [basis(self.num_levels, 0)]
            + [identity(2) for n in range(self.num_qubits)]
        )
        return psi_proj.dag() * U * psi_proj

    def load_circuit(self, qc, schedule_mode="ASAP", compiler=None):
        if compiler is None:
            compiler = CavityQEDCompiler(
                self.num_qubits, self.params, global_phase=0.0
            )
        tlist, coeff = super().load_circuit(
            qc, schedule_mode=schedule_mode, compiler=compiler
        )
        self.global_phase = compiler.global_phase
        return tlist, coeff


class CavityQEDModel(Model):
    """
    The physical model for a dispersive cavity-QED processor
    (:obj:`.DispersiveCavityQED`).

    Parameters
    ----------
    num_qubits : int
        The number of qubits.
    num_levels : int, optional
        The truncation level of the Hilbert space for the resonator.
    **params :
        Keyword arguments for hardware parameters, in the unit of GHz.
        Qubit parameters can either be a float or a list of the length
        ``num_qubits``.

        - deltamax: float or list, optional
            The pulse strength of sigma-x control, default ``1.0``.
        - epsmax: float or list, optional
            The pulse strength of sigma-z control, default ``9.5``.
        - eps: float or list, optional
            The bare transition frequency for each of the qubits,
            default ``9.5``.
        - delta : float or list, optional
            The coupling between qubit states, default ``0.0``.
        - g : float or list, optional
            The coupling strength between the resonator and the qubit,
            default ``1.0``.
        - w0 : float, optional
            The bare frequency of the resonator. Should only be a float,
            default ``0.01``.
        - t1 : float or list, optional
            Characterize the amplitude damping for each qubit.
        - t2 : list of list, optional
            Characterize the total dephasing for each qubit.

        The dressed qubit frequency is `wq` is computed by
        :math:`w_q=\sqrt{\epsilon^2+\delta^2}`
    """

    def __init__(self, num_qubits, num_levels=10, **params):
        self.num_qubits = num_qubits
        self.num_levels = num_levels
        self.dims = [num_levels] + [2] * num_qubits
        self.params = {  # default parameters
            "deltamax": 1.0,
            "epsmax": 9.5,
            "w0": 10,
            "eps": 9.5,
            "delta": 0.0,
            "g": 0.01,
        }
        self.params.update(deepcopy(params))
        self._drift = []
        self._controls = self._set_up_controls()
        self._compute_params()
        self._noise = []

    def get_all_drift(self):
        return self._drift

    @property
    def _old_index_label_map(self):
        num_qubits = self.num_qubits
        return (
            ["sx" + str(i) for i in range(num_qubits)]
            + ["sz" + str(i) for i in range(num_qubits)]
            + ["g" + str(i) for i in range(num_qubits)]
        )

    def _set_up_controls(self):
        """
        Generate the Hamiltonians for the cavity-qed model and save them in the
        attribute `ctrls`.

        Parameters
        ----------
        num_qubits: int
            The number of qubits in the system.
        """
        controls = {}
        num_qubits = self.num_qubits
        num_levels = self.num_levels
        # single qubit terms
        for m in range(num_qubits):
            controls["sx" + str(m)] = (2 * np.pi * sigmax(), [m + 1])
        for m in range(num_qubits):
            controls["sz" + str(m)] = (2 * np.pi * sigmaz(), [m + 1])
        # coupling terms
        a = tensor(
            [destroy(num_levels)] + [identity(2) for n in range(num_qubits)]
        )
        for n in range(num_qubits):
            # FIXME expanded?
            sm = tensor(
                [identity(num_levels)]
                + [
                    destroy(2) if m == n else identity(2)
                    for m in range(num_qubits)
                ]
            )
            controls["g" + str(n)] = (
                2 * np.pi * a.dag() * sm + 2 * np.pi * a * sm.dag(),
                list(range(num_qubits + 1)),
            )
        return controls

    def _compute_params(self):
        """
        Compute the qubit frequency and detune.
        """
        num_qubits = self.num_qubits
        w0 = self.params["w0"]  # only one resonator
        # same parameters for all qubits if it is not a list
        for name in ["epsmax", "deltamax", "eps", "delta", "g"]:
            self.params[name] = _to_array(self.params[name], num_qubits)

        # backward compatibility
        self.params["sz"] = self.params["epsmax"]
        self.params["sx"] = self.params["deltamax"]

        # computed
        wq = np.sqrt(self.params["eps"] ** 2 + self.params["delta"] ** 2)
        self.params["wq"] = wq
        self.params["Delta"] = wq - w0

        # rwa/dispersive regime tests
        if any(self.params["g"] / (w0 - wq) > 0.05):
            warnings.warn("Not in the dispersive regime")

        if any((w0 - wq) / (w0 + wq) > 0.05):
            warnings.warn(
                "The rotating-wave approximation might not be valid."
            )

    def get_control_latex(self):
        """
        Get the labels for each Hamiltonian.
        It is used in the method method :meth:`.Processor.plot_pulses`.
        It is a 2-d nested list, in the plot,
        a different color will be used for each sublist.
        """
        num_qubits = self.num_qubits
        return [
            {f"sx{m}": r"$\sigma_x^" + f"{m}$" for m in range(num_qubits)},
            {f"sz{m}": r"$\sigma_z^" + f"{m}$" for m in range(num_qubits)},
            {f"g{m}": f"$g^{m}$" for m in range(num_qubits)},
        ]
