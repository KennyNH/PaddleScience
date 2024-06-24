from os import path as osp
import os
import hydra
import numpy as np
import paddle
from omegaconf import DictConfig, OmegaConf
from paddle import nn

import examples.extformer_moe.enso_metric as enso_metric
import ppsci
from ppsci.data.dataset import ext_moe_enso_dataset as enso_dataset
from ppsci.utils import logger

try:
    import xarray as xr
except ModuleNotFoundError:
    raise ModuleNotFoundError("Please install xarray with `pip install xarray`.")


def get_parameter_names(model, forbidden_layer_types):
    result = []
    for name, child in model.named_children():
        result += [
            f"{name}.{n}"
            for n in get_parameter_names(child, forbidden_layer_types)
            if not isinstance(child, tuple(forbidden_layer_types))
        ]
    # Add model specific parameters (defined with nn.Parameter) since they are not in any child.
    result += list(model._parameters.keys())
    return result


def train(cfg: DictConfig):
    # set train dataloader config
    train_dataloader_cfg = {
        "dataset": {
            "name": "ExtMoEENSODataset",
            "data_dir": cfg.FILE_PATH,
            "input_keys": cfg.MODEL.input_keys,
            "label_keys": cfg.DATASET.label_keys,
            "in_len": cfg.DATASET.in_len,
            "out_len": cfg.DATASET.out_len,
            "in_stride": cfg.DATASET.in_stride,
            "out_stride": cfg.DATASET.out_stride,
            "train_samples_gap": cfg.DATASET.train_samples_gap,
            "eval_samples_gap": cfg.DATASET.eval_samples_gap,
            "normalize_sst": cfg.DATASET.normalize_sst,
        },
        "sampler": {
            "name": "BatchSampler",
            "drop_last": True,
            "shuffle": True,
        },
        "batch_size": cfg.TRAIN.batch_size,
        "num_workers": 8,
    }

    # set constraint
    sup_constraint = ppsci.constraint.SupervisedConstraint(
        train_dataloader_cfg,
        loss=ppsci.loss.FunctionalLoss(enso_metric.train_extformer_moe_func),
        name="Sup",
    )
    constraint = {sup_constraint.name: sup_constraint}
    
    # set iters_per_epoch by dataloader length
    ITERS_PER_EPOCH = len(sup_constraint.data_loader)
    # set eval dataloader config
    eval_dataloader_cfg = {
        "dataset": {
            "name": "ExtMoEENSODataset",
            "data_dir": cfg.FILE_PATH,
            "input_keys": cfg.MODEL.input_keys,
            "label_keys": cfg.DATASET.label_keys,
            "in_len": cfg.DATASET.in_len,
            "out_len": cfg.DATASET.out_len,
            "in_stride": cfg.DATASET.in_stride,
            "out_stride": cfg.DATASET.out_stride,
            "train_samples_gap": cfg.DATASET.train_samples_gap,
            "eval_samples_gap": cfg.DATASET.eval_samples_gap,
            "normalize_sst": cfg.DATASET.normalize_sst,
            "training": "eval",
        },
        "batch_size": cfg.EVAL.batch_size,
    }

    sup_validator = ppsci.validate.SupervisedValidator(
        eval_dataloader_cfg,
        loss=ppsci.loss.FunctionalLoss(enso_metric.train_extformer_moe_func),
        metric={
            "rmse": ppsci.metric.FunctionalMetric(enso_metric.eval_rmse_func),
        },
        name="Sup_Validator",
    )
    validator = {sup_validator.name: sup_validator}
    
    moe_config = OmegaConf.to_object(cfg.MOE)
    rnc_config = OmegaConf.to_object(cfg.RNC)
    model = ppsci.arch.ExtFormerMoECuboid(
        **cfg.MODEL, moe_config=moe_config, rnc_config=rnc_config
    )

    decay_parameters = get_parameter_names(model, [nn.LayerNorm])
    decay_parameters = [name for name in decay_parameters if "bias" not in name]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if n in decay_parameters],
            "weight_decay": cfg.TRAIN.wd,
        },
        {
            "params": [
                p for n, p in model.named_parameters() if n not in decay_parameters
            ],
            "weight_decay": 0.0,
        },
    ]

    # # init optimizer and lr scheduler
    lr_scheduler_cfg = dict(cfg.TRAIN.lr_scheduler)
    lr_scheduler = ppsci.optimizer.lr_scheduler.Cosine(
        **lr_scheduler_cfg,
        iters_per_epoch=ITERS_PER_EPOCH,
        eta_min=cfg.TRAIN.min_lr_ratio * cfg.TRAIN.lr_scheduler.learning_rate,
        warmup_epoch=int(0.2 * cfg.TRAIN.epochs),
    )()
    optimizer = paddle.optimizer.AdamW(
        lr_scheduler, parameters=optimizer_grouped_parameters
    )
    
    if cfg.RNC.use_rnc and cfg.RNC.rnc_pretrain:
        
        if not cfg.RNC.load_rnc_pretrain_params:
        
            print("============= RNC PRETRAIN =============")
            
            model.rnc_pretrain_flag = True

            rnc_sup_constraint = ppsci.constraint.SupervisedConstraint(
                train_dataloader_cfg,
                loss=ppsci.loss.FunctionalLoss(enso_metric.rnc_pretrain_func),
                name="RNCSup",
            )
            rnc_constraint = {rnc_sup_constraint.name: rnc_sup_constraint}
            
            rnc_eval_dataloader_cfg = {
                "dataset": {
                    "name": "ExtMoEENSODataset",
                    "data_dir": cfg.FILE_PATH,
                    "input_keys": cfg.MODEL.input_keys,
                    "label_keys": cfg.DATASET.label_keys,
                    "in_len": cfg.DATASET.in_len,
                    "out_len": cfg.DATASET.out_len,
                    "in_stride": cfg.DATASET.in_stride,
                    "out_stride": cfg.DATASET.out_stride,
                    "train_samples_gap": cfg.DATASET.train_samples_gap,
                    "eval_samples_gap": cfg.DATASET.eval_samples_gap,
                    "normalize_sst": cfg.DATASET.normalize_sst,
                    "training": "eval",
                },
                "sampler": {
                    "name": "BatchSampler",
                    "drop_last": True,
                    "shuffle": True,
                },
                "batch_size": cfg.TRAIN.batch_size,
            }
                
            rnc_sup_validator = ppsci.validate.SupervisedValidator(
                rnc_eval_dataloader_cfg,
                loss=ppsci.loss.FunctionalLoss(enso_metric.rnc_pretrain_func),
                metric={
                    "rnc_loss": ppsci.metric.FunctionalMetric(enso_metric.eval_rnc_pretrain_func),
                },
                name="RNCSup_Validator",
            )
            rnc_validator = {rnc_sup_validator.name: rnc_sup_validator}
            
            rnc_solver = ppsci.solver.Solver(
                model,
                rnc_constraint,
                os.path.join(cfg.output_dir, "rnc_pretrain"),
                optimizer,
                lr_scheduler,
                cfg.TRAIN.epochs,
                ITERS_PER_EPOCH,
                update_freq=cfg.RNC.pretrain_update_freq,
                eval_during_train=cfg.TRAIN.eval_during_train,
                seed=cfg.seed,
                device=f'gpu:{cfg.DEVICE}' if cfg.DEVICE != -1 else 'cpu', 
                validator=rnc_validator,
                compute_metric_by_batch=cfg.EVAL.compute_metric_by_batch,
                eval_with_no_grad=cfg.EVAL.eval_with_no_grad,
            )

            # train model
            rnc_solver.train()
            # evaluate after finished training
            rnc_solver.eval()
            
            print("============= RNC PRETRAIN END =============")

        print("============= LOAD RNC PRETRAIN PARAMs =============")
        pretrained_model_path = os.path.join(cfg.output_dir, "rnc_pretrain", "checkpoints") + "/best_model.pdparams" 
        print("====================================================")
    else:
        pretrained_model_path = None
        
    # initialize solver, eval_freq: int = 1
    model.rnc_pretrain_flag = False
    solver = ppsci.solver.Solver(
        model,
        constraint,
        cfg.output_dir,
        optimizer,
        lr_scheduler,
        cfg.TRAIN.epochs,
        ITERS_PER_EPOCH,
        update_freq=cfg.TRAIN.update_freq,
        eval_during_train=cfg.TRAIN.eval_during_train,
        seed=cfg.seed,
        device=f'gpu:{cfg.DEVICE}' if cfg.DEVICE != -1 else 'cpu', 
        validator=validator,
        pretrained_model_path=pretrained_model_path,
        compute_metric_by_batch=cfg.EVAL.compute_metric_by_batch,
        eval_with_no_grad=cfg.EVAL.eval_with_no_grad,
    )

    # train model
    solver.train()
    # evaluate after finished training
    solver.eval()


def evaluate(cfg: DictConfig):
    # set eval dataloader config
    eval_dataloader_cfg = {
        "dataset": {
            "name": "ExtMoEENSODataset",
            "data_dir": cfg.FILE_PATH,
            "input_keys": cfg.MODEL.input_keys,
            "label_keys": cfg.DATASET.label_keys,
            "in_len": cfg.DATASET.in_len,
            "out_len": cfg.DATASET.out_len,
            "in_stride": cfg.DATASET.in_stride,
            "out_stride": cfg.DATASET.out_stride,
            "train_samples_gap": cfg.DATASET.train_samples_gap,
            "eval_samples_gap": cfg.DATASET.eval_samples_gap,
            "normalize_sst": cfg.DATASET.normalize_sst,
            "training": "test",
        },
        "batch_size": cfg.EVAL.batch_size,
    }

    sup_validator = ppsci.validate.SupervisedValidator(
        eval_dataloader_cfg,
        loss=ppsci.loss.FunctionalLoss(enso_metric.train_extformer_moe_func),
        metric={
            "rmse": ppsci.metric.FunctionalMetric(enso_metric.eval_rmse_func),
        },
        name="Sup_Validator",
    )
    validator = {sup_validator.name: sup_validator}

    moe_config = OmegaConf.to_object(cfg.MOE)
    rnc_config = OmegaConf.to_object(cfg.RNC)
    model = ppsci.arch.ExtFormerMoECuboid(
        **cfg.MODEL, moe_config=moe_config, rnc_config=rnc_config
    )

    # initialize solver
    solver = ppsci.solver.Solver(
        model,
        output_dir=cfg.output_dir,
        log_freq=cfg.log_freq,
        seed=cfg.seed,
        device=f'gpu:{cfg.DEVICE}' if cfg.DEVICE != -1 else 'cpu', 
        validator=validator,
        pretrained_model_path=cfg.EVAL.pretrained_model_path,
        compute_metric_by_batch=cfg.EVAL.compute_metric_by_batch,
        eval_with_no_grad=cfg.EVAL.eval_with_no_grad,
    )
    # evaluate
    solver.eval()


@hydra.main(
    version_base=None,
    config_path="./conf",
    config_name="extformer_moe_enso_pretrain.yaml",
)
def main(cfg: DictConfig):
    paddle.device.set_device(f'gpu:{cfg.DEVICE}' if cfg.DEVICE != -1 else 'cpu')
    if cfg.mode == "train":
        train(cfg)
    elif cfg.mode == "eval":
        evaluate(cfg)
    else:
        raise ValueError(
            f"cfg.mode should in ['train', 'eval'], but got '{cfg.mode}'"
        )


if __name__ == "__main__":
    main()
