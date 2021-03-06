# coding=utf-8
# Copyright (c) DIRECT Contributors
import argparse
import os
import pathlib
import sys
import torch
import direct.utils.logging

from direct.config.defaults import DefaultConfig, TrainingConfig
from direct.nn.rim.mri_models import MRIReconstruction
from direct.utils import communication, str_to_class
from collections import namedtuple

from omegaconf import OmegaConf

import logging

logger = logging.getLogger(__name__)


def load_model_config(cfg, model_name):
    model_name += "Config"
    module_path = f"direct.nn.{cfg.model_name.split('.')[0].lower()}.config"
    config_name = model_name.split(".")[-1]
    try:
        model_cfg = str_to_class(module_path, config_name)
    except (AttributeError, ModuleNotFoundError) as e:
        logger.error(
            f"Path {module_path} for config_name {config_name} does not exist (err = {e})."
        )
        sys.exit(-1)
    return model_cfg


def load_model_from_name(cfg, model_name):
    module_path = f"direct.nn.{cfg.model_name.split('.')[0].lower()}"
    module_name = model_name.split(".")[-1]
    try:
        model = str_to_class(module_path, module_name)
    except (AttributeError, ModuleNotFoundError) as e:
        logger.error(
            f"Path {module_path} for model_name {module_name} does not exist (err = {e})."
        )
        sys.exit(-1)

    return model


# TODO(jt): This needs to be merged with the main model as well.
def load_additional_models(cfg_from_file):
    # Parse config of additional models
    # TODO(jt): Merge this with the normal model config loading.
    additional_models_config = {}
    additional_models = {}
    if "additional_models" in cfg_from_file:
        for additional_model_name in cfg_from_file.additional_models:
            if (
                "model_name"
                not in cfg_from_file.additional_models[additional_model_name]
            ):
                logger.error(
                    f"Additional model {additional_model_name} has no model_name."
                )
                sys.exit(-1)
            model_name = cfg_from_file.additional_models[
                additional_model_name
            ].model_name
            model_cfg = load_model_config(
                cfg_from_file.additional_models[additional_model_name], model_name
            )
            model = load_model_from_name(
                cfg_from_file.additional_models[additional_model_name], model_name
            )
            additional_models_config[additional_model_name] = OmegaConf.structured(
                model_cfg
            )
            # Save the model itself
            additional_models[additional_model_name] = model
    additional_models_config = OmegaConf.merge(additional_models_config)

    return additional_models_config, additional_models


def count_parameters(models: dict) -> None:
    total_number_of_parameters = 0
    for model_name in models:
        n_params = sum(p.numel() for p in models[model_name].parameters())
        logger.info(
            f"Number of parameters model {model_name}: {n_params} ({n_params / 10.0 ** 3:.2f}k)."
        )
        logger.debug(models[model_name])
        total_number_of_parameters += n_params
    logger.info(
        f"Total number of parameters model: {total_number_of_parameters} "
        f"({total_number_of_parameters / 10.0 ** 3:.2f}k)."
    )


def setup_environment(
    run_name,
    base_directory,
    cfg_filename,
    device,
    machine_rank,
    mixed_precision,
    debug=False,
):
    experiment_dir = base_directory / run_name

    if communication.get_local_rank() == 0:
        # Want to prevent multiple workers from trying to write a directory
        # This is required in the logging below
        experiment_dir.mkdir(parents=True, exist_ok=True)
    communication.synchronize()  # Ensure folders are in place.

    # Load configs from YAML file to check which model needs to be loaded.
    cfg_from_file = OmegaConf.load(cfg_filename)

    # Load the configuration for the main model
    model_name = cfg_from_file.model_name
    model_cfg = load_model_config(cfg_from_file, model_name)

    additional_models_config, additional_models = load_additional_models(cfg_from_file)

    # Load the default configs to ensure type safety
    base_cfg = OmegaConf.structured(DefaultConfig)
    base_cfg.model = model_cfg
    base_cfg.training = TrainingConfig
    base_cfg.additional_models = additional_models_config
    cfg = OmegaConf.merge(base_cfg, cfg_from_file)

    # Check if the file exists in the project directory
    config_file_in_project_folder = experiment_dir / "config.yaml"
    if config_file_in_project_folder.exists():
        if dict(OmegaConf.load(config_file_in_project_folder)) != dict(cfg):
            pass
            # raise ValueError(
            #     f"This project folder exists and has a config.yaml, "
            #     f"yet this does not match with the one the model was built with."
            # )
    else:
        if communication.get_local_rank() == 0:
            with open(config_file_in_project_folder, "w") as f:
                f.write(OmegaConf.to_yaml(cfg))
        communication.synchronize()

    # Make configuration read only.
    # TODO(jt): Does not work when indexing config lists.
    # OmegaConf.set_readonly(cfg, True)
    # Setup logging
    log_file = (
        experiment_dir / f"log_{machine_rank}_{communication.get_local_rank()}.txt"
    )
    direct.utils.logging.setup(
        use_stdout=communication.get_local_rank() == 0 or debug,
        filename=log_file,
        log_level=("INFO" if not debug else "DEBUG"),
    )
    logger.info(f"Machine rank: {machine_rank}.")
    logger.info(f"Local rank: {communication.get_local_rank()}.")
    logger.info(f"Logging: {log_file}.")
    logger.info(f"Saving to: {experiment_dir}.")
    logger.info(f"Run name: {run_name}.")
    logger.info(f"Config file: {cfg_filename}.")
    logger.info(f"Python version: {sys.version.strip()}.")
    logger.info(f"PyTorch version: {torch.__version__}.")  # noqa
    logger.info(f"DIRECT version: {direct.__version__}.")  # noqa
    git_hash = direct.utils.git_hash()
    logger.info(f"Git hash: {git_hash if git_hash else 'N/A'}.")  # noqa
    logger.info(f"CUDA {torch.version.cuda} - cuDNN {torch.backends.cudnn.version()}.")
    logger.info(f"Configuration: {OmegaConf.to_yaml(cfg)}.")

    # Get the operators
    forward_operator = str_to_class(
        f"direct.data.transforms", cfg.modality.forward_operator
    )
    backward_operator = str_to_class(
        f"direct.data.transforms", cfg.modality.backward_operator
    )

    # Create the model
    logger.info("Building model.")
    model = MRIReconstruction(forward_operator, backward_operator, 2, **cfg.model).to(
        device
    )

    for k, v in additional_models.items():
        # Remove model_name key
        curr_kwargs = {
            kk: vv for kk, vv in cfg.additional_models[k].items() if kk != "model_name"
        }
        additional_models[k] = v(**curr_kwargs)

    # Log total number of parameters
    count_parameters({model_name: model, **additional_models})

    # Setup engine.
    # There is a bit of repetition here, but the warning provided is more descriptive
    # TODO(jt): Try to find a way to combine this with the setup above.
    engine_name = cfg_from_file.model_name + "Engine"
    try:
        engine_class = str_to_class(
            f"direct.nn.{cfg_from_file.model_name.lower()}.{cfg_from_file.model_name.lower()}_engine",
            engine_name,
        )
    except (AttributeError, ModuleNotFoundError) as e:
        logger.error(
            f"Engine does not exist for {cfg_from_file.model_name} (err = {e})."
        )
        sys.exit(-1)

    # TODO(jt): Log parameters for other model too.

    engine = engine_class(
        cfg, model, device=device, mixed_precision=mixed_precision, **additional_models,
    )

    environment = namedtuple(
        "environment",
        ["cfg", "experiment_dir", "forward_operator", "backward_operator", "engine"],
    )
    return environment(cfg, experiment_dir, forward_operator, backward_operator, engine)


class Args(argparse.ArgumentParser):
    """
    Defines global default arguments.
    """

    def __init__(self, epilog=None, **overrides):
        """
        Args:
            **overrides (dict, optional): Keyword arguments used to override default argument values
        """
        super().__init__(
            epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter
        )

        self.add_argument(
            "--debug", action="store_true", help="If set debug output will be shown."
        )
        self.add_argument(
            "--device",
            type=str,
            default="cuda",
            help='Which device to train on. Set to "cuda" to use the GPU.',
        )
        self.add_argument(
            "--seed", default=42, type=int, help="Seed for random number generators."
        )
        self.add_argument(
            "--num-workers", type=int, default=4, help="Number of workers."
        )
        self.add_argument(
            "--cfg",
            dest="cfg_file",
            help="Config file for training and testing.",
            required=True,
            type=pathlib.Path,
        )
        self.add_argument(
            "--name", help="Run name, if None use configs name.", default=None, type=str
        )

        self.add_argument(
            "--mixed-precision", help="Use mixed precision.", action="store_true"
        )

        self.add_argument("--num-gpus", type=int, default=1, help="# GPUs per machine.")
        self.add_argument("--num-machines", type=int, default=1, help="# of machines.")
        self.add_argument(
            "--machine-rank",
            type=int,
            default=0,
            help="the rank of this machine (unique per machine).",
        )

        # Taken from: https://github.com/facebookresearch/detectron2/blob/bd2ea475b693a88c063e05865d13954d50242857/detectron2/engine/defaults.py#L49 # noqa
        # PyTorch still may leave orphan processes in multi-gpu training.
        # Therefore we use a deterministic way to obtain port,
        # so that users are aware of orphan processes by seeing the port occupied.
        port = 2 ** 15 + 2 ** 14 + hash(os.getuid()) % 2 ** 14
        self.add_argument(
            "--dist-url",
            default=f"tcp://127.0.0.1:{port}",
            help="initialization URL for pytorch distributed backend. See "
            "https://pytorch.org/docs/stable/distributed.html for details.",
        )

        self.set_defaults(**overrides)
