# stdlib
import importlib
from dataclasses import dataclass

# externals
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader, random_split
from torch.utils.tensorboard import SummaryWriter

# internals
from cassetta.core.typing import Union
from cassetta.core.utils import (
    refresh_experiment_dir,
    delete_files_with_pattern,
    find_checkpoint,
)
from cassetta.io.modules import LoadableModule, LoadableModuleDict
from cassetta.io.loadable import StateMixin, LoadableMixin


@dataclass
class TrainerConfig(StateMixin):
    """
    State configuration of the training process.

    Attributes
    ----------
    experiment_dir : str
        The filesystem path where model checkpoints and logs will be saved.
    nb_epochs : int, optional
        The number of epochs to train the model. Default is 100.
    batch_size : int, optional
        The number of samples per training batch. Default is 1.
    lr : float, optional
        The learning rate for the optimizer. Default is 1e-4.
    logging_verbosity : int, optional
        Optionally log training progress with a specified verbosity {0, 1, 2}.
        Default is 1.\n
        Options are:
            - 0: No logging to tensorboard.
            - 1: Basic logging to tensorboard (eval and train loss).
            - 2: Detailed logging to tensorboard, eval loss, training loss,
                parameter histograms, and model graph.
    early_stopping : float
    refresh_experiment_dir : bool
        Delete contents of `experiment_dir` when run starts.
    """
    experiment_dir: str
    nb_epochs: int = 100
    batch_size: int = 1
    lr: float = 1e-4
    logging_verbosity: int = 1
    early_stopping: float = 0
    refresh_experiment_dir: bool = False
    train_to_val: float = 0.8
    num_workers: int = 0


@dataclass
class TrainingState(StateMixin):
    """
    Tracks the current state of the training process.

    Attributes
    ----------
    current_epoch : int, optional
        The current epoch number being processed. Default is 0.
    current_step : int, optional
        The current step number within the current epoch. Default is 0.
    epoch_train_loss : float, optional
        The cumulative training loss for the current epoch. Default is 0.0.
    epoch_eval_loss : float, optional
        The cumulative evaluation loss for the current epoch. Default is 0.0.
    best_eval_loss : None, optional
        The best evaluation loss for the entire training process.
        Default is inf.
    """
    current_epoch: int = 0
    current_step: int = 0
    epoch_train_loss: float = 0.0
    epoch_eval_loss: float = 0.0
    best_eval_loss: float = float('inf')


class LoadableTrainer(LoadableModule, save_args=False):
    """
    Base class for training models with serialization and state loading.

    This class provides a structure for managing model training with PyTorch.
    It supports the registration of models, optimizers, and loss functions,
    and allows saving/loading the complete training state to/from a file,
    making it suitable for resuming training or fine-tuning from a saved state.

    Attributes
    ----------
    models : LoadableModuleDict
        A loadable module dictionary for registered models, where each model
        should inherit from `LoadableMixin` to support serialization.
    optimizers : dict
        A dictionary containing the registered optimizers, with model names
        as keys and optimizer instances as values.
    losses : dict
        A dictionary containing registered loss functions for each model.
    training_state : TrainingState
        An object that tracks the training state, including metrics such as
        the best validation loss.

    Parameters
    ----------
    model : nn.Module, optional
        The main model to be used in training, by default None. It must inherit
        from `LoadableMixin` for serialization.
    optimizer : torch.optim.Optimizer, optional
        The optimizer associated with the main model, by default None.
    loss : nn.Module, optional
        The loss function associated with the main model, by default None.
    """

    def __init__(
        self,
        model=None,
        optimizer=None,
        loss=None,
        config: TrainerConfig = None,
    ):
        """
        Initializes the Trainer with optional model, optimizer, and loss.

        Parameters
        ----------
        model : nn.Module, optional
            The main model to be used in training, by default None. It must
            inherit from `LoadableMixin` for serialization.
        optimizer : torch.optim.Optimizer, optional
            The optimizer associated with the main model, by default None.
        loss : nn.Module, optional
            The loss function associated with the main model, by default None.
        """
        super().__init__()
        self.config = config
        self.training_state = TrainingState()
        self.models = LoadableModuleDict()
        self.optimizers = {}
        self.losses = LoadableModuleDict()
        self._handle_inputs(model, optimizer, loss)
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu"
        )

    def _handle_inputs(self, model, optimizer, loss) -> None:
        """
        Handles initial registration of the (main) model and associated
        optimizer and loss function, if provided.

        Parameters
        ----------
        model : nn.Module
            The main model to be registered.
        optimizer : torch.optim.Optimizer
            The main optimizer to be registered.
        loss : nn.Module
            The main loss function to be registered.
        """
        if model is not None:
            self.register_model('main', model)
        if optimizer is not None:
            self.register_optimizer('main', optimizer)
        if loss is not None:
            self.losses['main'] = loss

    def serialize(self) -> dict:
        """
        Custom `Trainer` serialization.

        Serializes the Trainer object to a dictionary, capturing the state
        of all models, optimizers, and trainer state for saving to a
        checkpoint.

        Returns
        -------
        dict
            Dictionary containing serialized object.
        """
        # Serialize all models (LoadableModuleDict handles this nicely :))
        state_dict = self.models.serialize()
        state_dict["losses"] = self.losses.serialize()
        # Serialize all optimizers
        state_dict["optimizers"] = self._get_components_state_dict(
            self.optimizers
        )
        state_dict["training_state"] = self.training_state.serialize()
        state_dict['config'] = self.config.serialize()
        return state_dict

    def _get_components_state_dict(self, component):
        """
        Serializes all members of a `Trainer` component (models, optimizers,
        etc...) into their own state dictionary for easy grouping in into
        multi-component training setups.

        Parameters
        ----------
        component : dict
            A dictionary containing the `Trainer` component to be serialized.

        Returns
        -------
        dict
            A dictionary containing the serialized state of the component's
            members.
        """
        component_state_dict = {
            name: member.serialize() for name, member in component.items()
        }
        return component_state_dict

    @classmethod
    def load(cls, state, checkpoint_type: str = 'best'):
        """
        Loads a Trainer object from a saved state, reconstructing models and
        optimizers based on the saved configuration and state dicts.

        Parameters
        ----------
        state : dict or str
            A dictionary containing the Trainer state, a path to a .pt file,
            or the directory of the experiment `version_*` from which the state
            can be loaded.
        checkpoint_type : str, optional
            The type of checkpoint to find, if `state` is a path. Options
            are either "last" or "best". Defaults to "best".

        Returns
        -------
        Trainer
            An instance of Trainer with loaded models, optimizers, and
            trainer state.
        """
        if not isinstance(state, dict):
            if not state.endswith(".pt"):
                state = find_checkpoint(state, checkpoint_type)
            state = torch.load(state, weights_only=True)

        # Init an instance of `Trainer`
        obj = cls()

        # TODO: This is not elegant. Figure out more elegant way to load.
        obj.models = LoadableMixin.load(state)
        obj.losses = LoadableMixin.load(state['losses'])
        # Unpacking `training_state` into the obj
        obj.training_state = TrainingState().load_state_dict(
            state['training_state']
        )
        obj.config = TrainerConfig(**state['config'])

        # Init optimizers with saved model weights and OG optimizer params.
        obj = cls._optimizers_from_state_dict(cls, obj, state)
        # Resetting the best eval loss so fine tuning isn't expected to perform
        # as well.
        obj.training_state.best_eval_loss = float('inf')

        return obj

    def _optimizers_from_state_dict(self, obj, state_dict: dict):
        """
        Deseralize optimizers from state dict with model parameters.

        This method builds uninitalized optimizers by loading the corresponding
        model and instantiating the optimizer by passing the loaded model's
        parameters. Args and kwargs, extracted from it's `state_dict`, are also
        passed to the optimizer on initialization, as they were not captured
        on the optimizer's initialization.

        Parameters
        ----------
        obj : Trainer
            Trainer instance
        state_dict : dict
            State dictionary for the optimizers.
        """
        # Get the optimizers state so we can iterate through them.
        optimizers_state = state_dict.get("optimizers", {})
        # Iterate through the optimizers
        for name, optimizer_state in optimizers_state.items():
            # Get the optimizer class and the module path
            module_path = optimizer_state['module']
            class_name = optimizer_state['qualname']
            # Get the repr of the module that holds the optimizer class
            module = importlib.import_module(module_path)
            # Build the uninitialized optimizer
            uninitialized_optimizer = getattr(module, class_name)
            # Initialize optimizer with model's parameters and saved
            # args and kwargs
            # Get the model so we can pass its params on optimizer init.
            model = getattr(obj.models, name)
            # Get _all_ the arguments from the saved optimizer.
            opt_args = state_dict[
                'optimizers'
                ][name]['state']['param_groups'][0]
            opt_args.pop('params')
            # Initialize the optimizer
            optimizer = uninitialized_optimizer(model.parameters(), **opt_args)
            # Register optimizer
            obj.optimizers[name] = optimizer
        return obj

    def register_model(self, name, model: nn.Module):
        """
        Register a model to the Trainer.

        Parameters
        ----------
        name : str
            The name of the model, used as the key in `self.models`.
        model : nn.Module
            The model to be registered, must inherit from `LoadableMixin`.
        """
        self.models[name] = model

    def register_optimizer(self, name, optim: torch.optim.Optimizer = None):
        """
        Register an optimizer to the trainer.

        Parameters
        ----------
        name : str
            The name of the model for corresponding to the optimizer. Used as
            the key in `self.models`.
        optim : nn.Module
            The optimizer to be registered.
        """
        self.optimizers[name] = optim


class SimpleSupervisedTrainer(LoadableTrainer):

    def __init__(
        self,
        dataset: Union[Dataset, DataLoader] = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.dataset = dataset
        self._get_loaders(self.dataset)

    @property
    def model(self):
        return self.models["main"]

    @property
    def optimizer(self):
        return self.optimizers["main"]

    @property
    def loss(self):
        return self.losses["main"]

    def serialize(self) -> dict:
        """
        Override the serialize method to exclude any PyTorch dataset
        instances in both 'state['kwargs']' and 'state['args']'.
        """
        state = super().serialize()

        # Exclude any instance of PyTorch Dataset in `state['kwargs']`
        keys_to_pop = [
            key for key, value in state['kwargs'].items()
            if isinstance(value, torch.utils.data.Dataset)
        ]

        for key in keys_to_pop:
            state['kwargs'].pop(key)

        # Exclude any instance of PyTorch Dataset in `state['args']`
        if isinstance(state['args'], (list, tuple)):
            state['args'] = [
                arg for arg in state['args']
                if not isinstance(arg, torch.utils.data.Dataset)
            ]

        return state

    def _get_loaders(self, dataset):
        """
        Get training and evaluation dataloaders from the dataset.

        Parameters
        ----------
        dataset : Dataset
            Dataset contining the data to be used for training and evaluation.
        """
        if dataset is not None:
            seed = torch.Generator().manual_seed(42)
            train_set_size = round(
                len(dataset) * self.config.train_to_val
            )
            val_set_size = len(dataset) - train_set_size

            train_set, eval_set = random_split(
                dataset, [train_set_size, val_set_size], seed
            )
            self.train_loader = DataLoader(
                        dataset=train_set,
                        batch_size=self.config.batch_size,
                        shuffle=True,
                        num_workers=self.config.num_workers
                        )
            self.eval_loader = DataLoader(
                dataset=eval_set,
                batch_size=1,
                shuffle=False,
                num_workers=self.config.num_workers
            )

    def train_step(self, minibatch):
        """
        Perform a single training step.

        This method performs a single training step by unpacking the minibatch
        into supervised pairs (x and y), zeroing gradients of the optimizer,
        and passing the x data into the model. The loss is then calculated
        between y's and the outputs of the model, and then added to the epoch's
        total training loss. Gradients are backpropogated, and the optimizer
        is stepped.

        Parameters
        ----------
        minibatch : torch.Tensor
            Training minibatch extracted from the dataloader. Assumed to have
            batch and channel dimensions.
        """
        # Unpack minibatch
        x, y = minibatch
        # Zero optimizer gradients
        self.optimizer.zero_grad()
        # Forward pass
        outputs = self.model(x)
        # Calculate loss
        _loss = self.loss(y, outputs)
        # Update trainer state
        self.training_state.epoch_train_loss += _loss.item()
        # Optionally log.
        # TODO: incorporate logger
        if self.config.logging_verbosity >= 1:
            self.log_metric('train', _loss.item(), 'step')
        # Backward pass
        _loss.backward()
        # Step optimizer
        self.optimizer.step()
        # Increment current step
        self.training_state.current_step += 1

    def eval_step(self, minibatch):
        """
        Perform a single evaluation (validation) step.

        This method performs a single step for evaluating the model on the
        evaluation set. Inference is performed without tracking gradients by
        first unpacking the minibatch and computing the loss between the y's
        and the model's output. The loss is calculated and accumulated into
        the epoch's total evaluation loss.

        Parameters
        ----------
        minibatch : torch.Tensor
            Evaluation minibatch extracted from the dataloader. Assumed to have
            batch and channel dimensions.
        """
        # Do not keep track of gradients
        with torch.no_grad():
            # Unpack minibatch
            x, y = minibatch
            # Forward pass
            outputs = self.model(x)
            # Calculate los`s
            _loss = self.loss(y, outputs)
            self.training_state.epoch_eval_loss += _loss.item()

    def log_loss(self, name, value):
        raise NotImplementedError

    def log_metric(
        self,
        resolution: str,
        scalar_value: float,
        timestep: int
    ):
        """
        Logs a scalar metric value at a specified timestep.

        Parameters
        ----------
        resolution : str
            Resolution of the logging interval. Options are {'epoch', 'step'}
        scalar_value : float
            The value/magnitude of the metric to be logged.
        timestep : int
            The current time step for logging the metric.
        """
        if timestep == 'epoch':
            timestep = self.training_state.current_epoch
        elif timestep == 'step':
            timestep = self.training_state.current_step
        else:
            raise f'Invalid timestep {timestep}. Must be "step" or "epoch".'

        self.writer.add_scalar(
            tag=f'{resolution}_loss',
            scalar_value=scalar_value,
            global_step=timestep
            )

    def train_epoch(self):
        """
        Train the model on an entire iteration of the training set.
        """
        # Set model to train mode
        self.model.train()
        # For sanity check dataset, must load minibatches like this or else
        # it will go to infinity.
        self.training_state.epoch_train_loss = 0
        for minibatch in self.train_loader:
            self.train_step(minibatch)
        # Average train epoch loss
        self.training_state.epoch_train_loss /= len(self.train_loader)
        if self.config.logging_verbosity >= 1:
            self.log_metric(
                'train_epoch',
                self.training_state.epoch_train_loss,
                'epoch')
        if self.config.logging_verbosity >= 2:
            self.log_parameter_hist()

    def eval_epoch(self):
        """
        Evaluate the model on an entire iteration of the evaluation set.
        """
        # Set model to eval mode
        self.model.eval()
        # Reset eval loss
        self.training_state.epoch_eval_loss = 0
        # Iterate through eval set
        for minibatch in self.eval_loader:
            self.eval_step(minibatch)
        # Average eval epoch loss
        self.training_state.epoch_eval_loss /= len(self.eval_loader)
        # Optionally log to tensorboard
        if self.config.logging_verbosity >= 1:
            self.log_metric(
                'eval_epoch',
                self.training_state.epoch_eval_loss,
                'epoch')
        # If this is best checkpoint...
        if self.training_state.epoch_eval_loss < (
            self.training_state.best_eval_loss
                ):

            self.training_state.best_eval_loss = (
                self.training_state.epoch_eval_loss
                )

            self.save_checkpoint('best')
        self.save_checkpoint('last')

    def train(self):
        """
        Train the model for `self.config.nb_epochs` epochs.
        """
        if self.config.logging_verbosity >= 1:
            self.writer = SummaryWriter(self.config.experiment_dir)
            if self.config.logging_verbosity >= 2:
                self.log_model_graph()
        if self.config.refresh_experiment_dir:
            refresh_experiment_dir(self.config.experiment_dir)
        for i in range(self.config.nb_epochs):
            self.train_epoch()
            if self.eval_loader:
                self.eval_epoch()
            # Increment current epoch
            self.training_state.current_epoch += 1

    def register_model(self, name, model):
        super().register_model(name, model)
        if self.config.logging_verbosity >= 2:
            self.log_model_graph()

    def log_model_graph(self) -> SummaryWriter:
        """
        Logs the model graph to TensorBoard.

        Returns
        -------
        SummaryWriter
            TensorBoard SummaryWriter object.
        """
        # Initialize writer
        sample_inputs, _ = next(iter(self.train_loader))
        # print(sample_inputs.shape)
        self.writer.add_graph(self.model, sample_inputs)

    def log_parameter_hist(self) -> None:
        """
        Log histograms of model parameters and gradients for TensorBoard.
        """
        for name, param in self.model.named_parameters():
            self.writer.add_histogram(
                name,
                param.to(torch.uint32),
                self.training_state.current_epoch
                )
            if param.grad is not None:
                self.writer.add_histogram(
                    tag=f'{name}.grad',
                    values=param.grad,
                    global_step=self.training_state.current_epoch
                )

    def save_checkpoint(self, type: str = 'last'):
        """
        Save a checkpoint.

        type : str
            Type of checkpoint to save {`last`, `best`}
        """
        checkpoint_dir = f'{self.config.experiment_dir}/checkpoints'
        if type == 'last':
            delete_files_with_pattern(checkpoint_dir, '*last*')
            self.save(
                f'{checkpoint_dir}/last-{self.training_state.current_epoch}.pt'
                )
        if type == 'best':
            delete_files_with_pattern(checkpoint_dir, '*best*')
            self.save(
                f'{checkpoint_dir}/best-{self.training_state.current_epoch}.pt'
                )
