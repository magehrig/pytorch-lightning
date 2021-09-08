# Copyright The PyTorch Lightning team.
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
from typing import Any, Optional

from pytorch_lightning.loops import Loop
from pytorch_lightning.loops.closure import ClosureResult
from pytorch_lightning.loops.utilities import (
    _build_training_step_kwargs,
    _check_training_step_output,
    check_finite_loss,
    _extract_hiddens,
)


class ManualOptimization(Loop):
    """A special loop implementing what is known in Lightning as Manual Optimization where the optimization happens
    entirely in the :meth:`~pytorch_lightning.core.lightning.LightningModule.training_step` and therefore the user
    is responsible for back-propagating gradients and making calls to the optimizers.

    This loop is a trivial case because it performs only a single iteration (calling directly into the module's
    :meth:`~pytorch_lightning.core.lightning.LightningModule.training_step`) and passing through the output(s).
    """

    def __init__(self) -> None:
        super().__init__()
        self._done: bool = False
        self._hiddens: Optional[Any] = None
        self._output: Optional[ClosureResult] = None

    @property
    def done(self) -> bool:
        return self._done

    def reset(self) -> None:
        self._done = False

    def advance(self, batch: Any, batch_idx: int) -> None:  # type: ignore[override]
        """Performs the training step for manual optimization.

        Args:
            batch: the current tbptt split of the current batch
            batch_idx: the index of the current batch
        """
        assert self.trainer is not None
        ligtning_module = self.trainer.lightning_module

        with self.trainer.profiler.profile("model_forward"):

            step_kwargs = _build_training_step_kwargs(
                ligtning_module, self.trainer.optimizers, batch, batch_idx, opt_idx=None, hiddens=self._hiddens
            )

            # manually capture logged metrics
            ligtning_module._current_fx_name = "training_step"
            with self.trainer.profiler.profile("training_step"):
                training_step_output = self.trainer.accelerator.training_step(step_kwargs)
                self.trainer.accelerator.post_training_step()

            del step_kwargs

            training_step_output = self.trainer.call_hook("training_step_end", training_step_output)

            _check_training_step_output(ligtning_module, training_step_output)

            self._hiddens = _extract_hiddens(training_step_output)

            # TODO: do not use `ClosureResult
            result = ClosureResult.from_training_step_output(
                training_step_output, self.trainer.accumulate_grad_batches
            )

            if self.trainer.terminate_on_nan:
                check_finite_loss(result.closure_loss)

            if self.trainer.move_metrics_to_cpu:
                assert self.trainer._results is not None
                self.trainer._results.cpu()
                result.cpu()

        self._done = True
        self._output = result

    def on_run_end(self) -> ClosureResult:
        """Returns the result of this loop, i.e., the post-processed outputs from the training step."""
        output, self._output = self._output, None  # free memory
        assert output is not None, "`advance` should have been called"
        return output