"""
Run predictors on either the MatrLA or PemsBay traffic datasets.

The underlying graph is generated by using a thresholded Gaussian kernel on the geographic distance of the sensors.
"""

import os
import copy

import pytorch_lightning as pl
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger

from tsl.datasets import MetrLA, PemsBay
from tsl.data import SpatioTemporalDataset, SpatioTemporalDataModule
from tsl.data.preprocessing import StandardScaler
from tsl.nn.utils import casting
from tsl.predictors import Predictor
from tsl.utils import TslExperiment, ArgParser, parser_utils, numpy_metrics
from tsl.utils.parser_utils import str_to_bool
from tsl.utils.neptune_utils import TslNeptuneLogger

import tsl

from tsl.nn.metrics.metrics import MaskedMAE, MaskedMAPE, MaskedMSE

import torch
from torch.optim.lr_scheduler import CosineAnnealingLR

import numpy as np

import pathlib
import datetime
import yaml

from tsl.nn.models.stgn.dcrnn_model import DCRNNModel
from tsl.nn.models.stgn.stcn_model import STCNModel
from tsl.nn.models.stgn.graph_wavenet_model import GraphWaveNetModel
from tsl.nn.models.stgn.rnn2gcn_model import RNNEncGCNDecModel as Rnn2GcnModel
from tsl.nn.models.stgn.gated_gn_model import GatedGraphNetworkModel

from tsl.nn.models import RNNModel, TransformerModel, TCNModel, FCRNNModel

tsl.config.config_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config', 'traffic')


def get_model_class(model_str):
    if model_str == 'dcrnn':
        model = DCRNNModel
    elif model_str == 'stcn':
        model = STCNModel
    elif model_str == 'gwnet':
        model = GraphWaveNetModel
    elif model_str == 'rnn':
        model = RNNModel
    elif model_str == 'fcrnn':
        model = FCRNNModel
    elif model_str == 'tcn':
        model = TCNModel
    elif model_str == 'transformer':
        model = TransformerModel
    elif model_str == 'rnn2gcn':
        model = Rnn2GcnModel
    elif model_str == 'gatedgn':
        model = GatedGraphNetworkModel
    else:
        raise NotImplementedError(f'Model "{model_str}" not available.')
    return model


def get_dataset(dataset_name):
    if dataset_name == 'la':
        dataset = MetrLA(impute_zeros=True)
    elif dataset_name == 'bay':
        dataset = PemsBay()
    else:
        raise ValueError(f"Dataset {dataset_name} not available in this setting.")
    return dataset


def add_parser_arguments(parent):
    # Argument parser
    parser = ArgParser(strategy='random_search', parents=[parent], add_help=False)

    parser.add_argument('--seed', type=int, default=-1)
    parser.add_argument("--model-name", type=str, default='fcrnn')
    parser.add_argument("--dataset-name", type=str, default='la')
    parser.add_argument("--config", type=str, default='fcrnn.yaml')
    # training
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--l2-reg', type=float, default=0.),
    parser.add_argument('--epochs', type=int, default=300)
    parser.add_argument('--patience', type=int, default=50)
    parser.add_argument('--grad-clip-val', type=float, default=5.)
    parser.add_argument('--use-lr-schedule', type=str_to_bool, nargs='?', const=True, default=True)
    parser.add_argument('--val-len', type=float, default=0.1)
    parser.add_argument('--test-len', type=float, default=0.2)

    # logging
    parser.add_argument('--save-preds', action='store_true', default=False)
    parser.add_argument('--neptune-logger', action='store_true', default=False)
    parser.add_argument('--project-name', type=str, default="sandbox")
    parser.add_argument('--tags', type=str, default=tuple())

    known_args, _ = parser.parse_known_args()
    model_cls = get_model_class(known_args.model_name)
    parser = model_cls.add_model_specific_args(parser)
    parser = SpatioTemporalDataset.add_argparse_args(parser)
    parser = SpatioTemporalDataModule.add_argparse_args(parser)
    return parser


def run_experiment(args):
    # Set configuration and seed
    args = copy.deepcopy(args)
    if args.seed < 0:
        args.seed = np.random.randint(1e9)
    torch.set_num_threads(1)
    pl.seed_everything(args.seed)

    tsl.logger.info(f'SEED: {args.seed}')

    model_cls = get_model_class(args.model_name)
    dataset = get_dataset(args.dataset_name)

    tsl.logger.info(args)

    ########################################
    # create logdir and save configuration #
    ########################################

    exp_name = f"{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{args.seed}"
    logdir = os.path.join(tsl.config.log_dir,
                          args.dataset_name,
                          args.model_name,
                          exp_name)
    # save config for logging
    pathlib.Path(logdir).mkdir(parents=True)
    with open(os.path.join(logdir, 'tsl_config.yaml'), 'w') as fp:
        yaml.dump(parser_utils.config_dict_from_args(args), fp, indent=4, sort_keys=True)

    ########################################
    # data module                          #
    ########################################

    # encode time of the day and use it as exogenous variable.
    exog_vars = dataset.datetime_encoded('day').values
    exog_vars = {'u': exog_vars}

    adj = dataset.get_connectivity(method='distance', threshold=0.1,
                                   layout='edge_index')

    torch_dataset = SpatioTemporalDataset(*dataset.numpy(return_idx=True),
                                          connectivity=adj,
                                          mask=dataset.mask,
                                          horizon=args.horizon,
                                          window=args.window,
                                          stride=args.stride,
                                          covariates=exog_vars)

    dm_conf = parser_utils.filter_args(args, SpatioTemporalDataModule, return_dict=True)
    dm = SpatioTemporalDataModule(
        dataset=torch_dataset,
        scalers={'target': StandardScaler(axis=(0, 1))},
        splitter=dataset.get_splitter(val_len=args.val_len,
                                      test_len=args.test_len),
        **dm_conf
    )

    ########################################
    # predictor                            #
    ########################################

    additional_model_hparams = dict(n_nodes=torch_dataset.n_nodes,
                                    input_size=torch_dataset.n_channels,
                                    output_size=torch_dataset.n_channels,
                                    horizon=torch_dataset.horizon,
                                    exog_size=torch_dataset.input_map.u.shape[-1])

    model_kwargs = parser_utils.filter_args(args={**vars(args), **additional_model_hparams},
                                            target_cls=model_cls,
                                            return_dict=True)

    loss_fn = MaskedMAE(compute_on_step=True)

    metrics = {'mae': MaskedMAE(compute_on_step=False),
               'mse': MaskedMSE(compute_on_step=False),
               'mape': MaskedMAPE(compute_on_step=False),
               'mae_at_15': MaskedMAE(compute_on_step=False, at=2),
               'mae_at_30': MaskedMAE(compute_on_step=False, at=5),
               'mae_at_60': MaskedMAE(compute_on_step=False, at=11), }

    # setup predictor
    scheduler_class = CosineAnnealingLR if args.use_lr_schedule else None
    predictor = Predictor(
        model_class=model_cls,
        model_kwargs=model_kwargs,
        optim_class=torch.optim.Adam,
        optim_kwargs={'lr': args.lr,
                      'weight_decay': args.l2_reg},
        loss_fn=loss_fn,
        metrics=metrics,
        scheduler_class=scheduler_class,
        scheduler_kwargs={
            'eta_min': 0.0001,
            'T_max': args.epochs
        },
    )

    ########################################
    # logging options                      #
    ########################################

    # log number of parameters
    args.trainable_parameters = predictor.trainable_parameters

    # add tags
    tags = list(args.tags) + [args.model_name, args.dataset_name]

    if args.neptune_logger:
        logger = TslNeptuneLogger(api_key=tsl.config['neptune_token'],
                                  project_name=f"{tsl.config['neptune_username']}/{args.project_name}",
                                  experiment_name=exp_name,
                                  tags=tags,
                                  params=vars(args),
                                  offline_mode=False,
                                  upload_stdout=False)
    else:
        logger = TensorBoardLogger(
            save_dir=logdir,
            name=f'{exp_name}_{"_".join(tags)}',

        )
    ########################################
    # training                             #
    ########################################

    early_stop_callback = EarlyStopping(
        monitor='val_mae',
        patience=args.patience,
        mode='min'
    )

    checkpoint_callback = ModelCheckpoint(
        dirpath=logdir,
        save_top_k=1,
        monitor='val_mae',
        mode='min',
    )

    trainer = pl.Trainer(max_epochs=args.epochs,
                         default_root_dir=logdir,
                         logger=logger,
                         gpus=1 if torch.cuda.is_available() else None,
                         gradient_clip_val=args.grad_clip_val,
                         callbacks=[early_stop_callback, checkpoint_callback])

    trainer.fit(predictor, datamodule=dm)

    ########################################
    # testing                              #
    ########################################

    predictor.load_model(checkpoint_callback.best_model_path)

    predictor.freeze()
    trainer.test(predictor, datamodule=dm)

    output = trainer.predict(predictor, dataloaders=dm.test_dataloader())
    output = casting.numpy(output)
    y_hat, y_true, mask = output['y_hat'], \
                          output['y'], \
                          output['mask']
    res = dict(test_mae=numpy_metrics.masked_mae(y_hat, y_true, mask),
               test_rmse=numpy_metrics.masked_rmse(y_hat, y_true, mask),
               test_mape=numpy_metrics.masked_mape(y_hat, y_true, mask))

    output = trainer.predict(predictor, dataloaders=dm.val_dataloader())
    output = casting.numpy(output)
    y_hat, y_true, mask = output['y_hat'], \
                          output['y'], \
                          output['mask']
    res.update(dict(val_mae=numpy_metrics.masked_mae(y_hat, y_true, mask),
                    val_rmse=numpy_metrics.masked_rmse(y_hat, y_true, mask),
                    val_mape=numpy_metrics.masked_mape(y_hat, y_true, mask)))
    if args.neptune_logger:
        logger.finalize('success')
    return tsl.logger.info(res)

if __name__ == '__main__':
    parser = ArgParser(add_help=False)
    parser = add_parser_arguments(parser)
    exp = TslExperiment(run_fn=run_experiment, parser=parser)
    exp.run()

