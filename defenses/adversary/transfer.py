#!/usr/bin/python
"""This is a short description.
Replace this with a more detailed description of what this file contains.
"""
import argparse
import os.path as osp
import os
import sys
import pickle
import json
from datetime import datetime
import re
import time

import numpy as np

from tqdm import tqdm

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
import torchvision

sys.path.append(os.getcwd())
from defenses import datasets
import defenses.utils.utils as knockoff_utils
import defenses.config as cfg
from defenses.victim import *
from wb_recover import Table_Recover








class RandomAdversaryIters(object):
    def __init__(self, blackbox, label_recover, queryset, batch_size=8):
        self.blackbox = blackbox
        self.label_recover = label_recover
        self.queryset = queryset

        self.n_queryset = len(self.queryset)
        self.batch_size = batch_size
        self.idx_set = set()
        self.budget_idxs = set()

        self.transferset = []  # List of tuples [(img_path, output_probs)]
        self.call_times = []

        self._restart()

    def _get_idxset(self, budget):
        prev_state = np.random.get_state()
        np.random.seed(cfg.DEFAULT_SEED)
        self.idx_set = np.arange(len(self.queryset))
        np.random.shuffle(self.idx_set)
        self.idx_set = set(self.idx_set[:budget])
        np.random.set_state(prev_state)

    def _restart(self):
        np.random.seed(cfg.DEFAULT_SEED)
        torch.manual_seed(cfg.DEFAULT_SEED)
        torch.cuda.manual_seed(cfg.DEFAULT_SEED)

        self.all_idxs = np.arange(len(self.queryset))
        np.random.shuffle(self.all_idxs)

        self.transferset = []
        self.call_times = []

    def get_transferset(self, budget, niters=None, queries_per_image=1):
        """

        :param budget:  # unique images drawn from queryset
        :param niters:  # of queries to blackbox
        :return:
        """
        if niters is None:
            niters = budget
        start_B = 0
        end_B = niters
        self.idx_set = set(self.all_idxs[:budget])

        stat = (self.label_recover is not None) and (self.blackbox.log_path is not None)
        if stat:
            log_path = self.blackbox.log_path.replace('distance','gtdistance')
            if not osp.exists(log_path):
                with open(log_path, 'w') as wf:
                    columns = ['transferset size', 'l1_max', 'l1_mean', 'l1_std', 'l2_mean', 'l2_std', 'kl_mean', 'kl_std']
                    wf.write('\t'.join(columns) + '\n')

        print('Constructing transferset using: # unique images = {}, # queries = {}'.format(len(self.idx_set), niters))
        if niters > budget:
            print('!!! WARNING !!! niters ({}) > budget ({}). Images will be repeated.'.format(niters, budget))

        if queries_per_image > 1:
            print('=> Obtaining mean posteriors over {} predictions per image'.format(queries_per_image))

        num_queries = 0
        IMG,Y,Y_true = [],[],[]
        with tqdm(total=niters) as pbar:
            for t, B in enumerate(range(start_B, end_B, self.batch_size)):

                idxs = np.random.choice(list(self.idx_set), replace=False,
                                        size=min(self.batch_size, niters - num_queries))
                self.idx_set = self.idx_set - set(idxs)
                
                num_queries += len(idxs)*queries_per_image

                if len(self.idx_set) == 0:
                    # print('=> Query set exhausted. Now repeating input examples.')
                    self.idx_set = set(self.all_idxs[:budget])

                x_t = torch.stack([self.queryset[i][0] for i in idxs]).to(self.blackbox.device)

                y_t_list = []
                
                for i in range(queries_per_image):
                    t_start = time.time()
                    if stat and i == 0:
                        _y_t, y_t_true = self.blackbox(x_t,return_origin=True)
                    else:
                        _y_t = self.blackbox(x_t)
                    
                    t_end = time.time()
                    self.call_times.append(t_end - t_start)
                    y_t_list.append(_y_t)
                    # if stat:
                    #     origin_transfer_list.append([y_true.cpu().numpy(),_y_t.cpu().numpy()])
                y_t = torch.stack(y_t_list).mean(dim=0)    # Mean over queries
                Y.append(y_t)
                if stat:
                    Y_true.append(y_t_true)
                # if stat and num_queries%1000==0:
                #     l1_mean, l1_std, l2_mean, l2_std, kl_mean, kl_std = self.blackbox.calc_query_distances(origin_transfer_list)
                #     with open(log_path, 'a') as af:
                #         test_cols = [num_queries, l1_mean, l1_std, l2_mean, l2_std, kl_mean, kl_std]
                #         af.write('\t'.join([str(c) for c in test_cols]) + '\n')


                if hasattr(self.queryset, 'samples'):
                    # Any DatasetFolder (or subclass) has this attribute
                    # Saving image paths are space-efficient
                    img_t = [self.queryset.samples[i][0] for i in idxs]  # Image paths
                    
                else:
                    # Otherwise, store the image itself
                    # But, we need to store the non-transformed version
                    img_t = [self.queryset.data[i] for i in idxs]
                    if isinstance(self.queryset.data[0], torch.Tensor):
                        img_t = [x.numpy() for x in img_t]
                    
                IMG = IMG + img_t
                pbar.update(x_t.size(0))
        
        Y = torch.cat(Y,dim=0)
        
        if self.label_recover is not None:
            print("Start to recover the clean labels!")
            with tqdm(total=len(Y)) as pbar:
                Y = self.label_recover(Y,pbar) # recover the whole transfer set together to reduce cpu-gpu convert
            if stat:
                Y_true = torch.cat(Y_true,dim=0)
                l1_max, l1_mean, l1_std, l2_mean, l2_std, kl_mean, kl_std = self.blackbox.calc_query_distances([[Y_true,Y],])
                with open(log_path, 'a') as af:
                    test_cols = [num_queries, l1_max, l1_mean, l1_std, l2_mean, l2_std, kl_mean, kl_std]
                    af.write('\t'.join([str(c) for c in test_cols]) + '\n')
        
        for i in range(len(IMG)):
            img_i = IMG[i].squeeze() if isinstance(IMG[i], np.ndarray) else IMG[i]
            self.transferset.append((img_i, Y[i].cpu().squeeze()))

                
        return self.transferset


def main():
    parser = argparse.ArgumentParser(description='Construct transfer set')
    parser.add_argument('policy', metavar='PI', type=str, help='Policy to use while training',
                        choices=['random', 'adaptive'])
    parser.add_argument('victim_model_dir', metavar='PATH', type=str,
                        help='Path to victim model. Should contain files "model_best.pth.tar" and "params.json"')
    parser.add_argument('defense', metavar='TYPE', type=str, help='Type of defense to use',
                        choices=knockoff_utils.BBOX_CHOICES, default='none')
    parser.add_argument('defense_args', metavar='STR', type=str, help='Blackbox arguments in format "k1:v1,k2:v2,..."')
    parser.add_argument('--defense_aware',type=int,help="Whether using defense-aware attack",default = 0)
    parser.add_argument('--recover_table_size',type=int,default=1000000,help="Size of the recover table")
    parser.add_argument('--dir_mle_path',type=str,help="Whether using MLE to get the dirichelet distribution factor",default = None)
    parser.add_argument('--recover_norm',type=int,default=2,help = "Norm used in table recover")
    parser.add_argument('--recover_tolerance',type=float,default=1e-4,help = "Tolerance of equivalence in table recover")
    parser.add_argument('--recover_proc',type=int,default=1,help="Number of processes used to generate recover table")
    parser.add_argument('--quantize',type=int,help="Whether using quantized defense",default=0)
    parser.add_argument('--quantize_args',type=str,help='Quantization arguments in format "k1:v1,k2:v2,..."')
    parser.add_argument('--out_dir', metavar='PATH', type=str,
                        help='Destination directory to store transfer set', required=True)
    parser.add_argument('--budget', metavar='N', type=int, help='# images',
                        default=None)
    parser.add_argument('--nqueries', metavar='N', type=int, help='# queries to blackbox using budget images',
                        default=None)
    parser.add_argument('--qpi', metavar='N', type=int, help='# queries per image',
                        default=1)
    parser.add_argument('--queryset', metavar='TYPE', type=str, help='Adversary\'s dataset (P_A(X))', required=True)
    parser.add_argument('--batch_size', metavar='TYPE', type=int, help='Batch size of queries', default=1)
    # ----------- Other params
    parser.add_argument('-d', '--device_id', metavar='D', type=int, help='Device id', default=0)
    parser.add_argument('-w', '--nworkers', metavar='N', type=int, help='# Worker threads to load data', default=10)
    parser.add_argument('--train_transform', action='store_true', help='Perform data augmentation', default=False)
    args = parser.parse_args()
    params = vars(args)

    out_path = params['out_dir']
    knockoff_utils.create_dir(out_path)

    torch.manual_seed(cfg.DEFAULT_SEED)
    if params['device_id'] >= 0:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(params['device_id'])
        device = torch.device('cuda')
    else:
        device = torch.device('cpu')

    # ----------- Set up queryset
    queryset_name = params['queryset']
    valid_datasets = datasets.__dict__.keys()
    if queryset_name not in valid_datasets:
        raise ValueError('Dataset not found. Valid arguments = {}'.format(valid_datasets))
    modelfamily = datasets.dataset_to_modelfamily[queryset_name]
    transform_type = 'train' if params['train_transform'] else 'test'
    if params['train_transform']:
        print('=> Using data augmentation while querying')
    transform = datasets.modelfamily_to_transforms[modelfamily][transform_type]
    queryset = datasets.__dict__[queryset_name](train=True, transform=transform)
    if params['budget'] is None:
        params['budget'] = len(queryset)

    # ----------- Initialize blackbox
    blackbox_dir = params['victim_model_dir']
    defense_type = params['defense']
    if defense_type == 'rand_noise':
        BB = RandomNoise
    elif defense_type == 'rand_noise_wb':
        BB = RandomNoise_WB
    elif defense_type == 'mad':
        BB = MAD
    elif defense_type == 'mad_wb':
        BB = MAD_WB
    elif defense_type == 'mld':
        BB = MLD
    elif defense_type == 'reverse_sigmoid':
        BB = ReverseSigmoid
    elif defense_type == 'reverse_sigmoid_wb':
        BB = ReverseSigmoid_WB
    elif defense_type in ['none', 'topk', 'rounding']:
        BB = Blackbox
    else:
        raise ValueError('Unrecognized blackbox type')
    defense_kwargs = knockoff_utils.parse_defense_kwargs(params['defense_args'])
    defense_kwargs['log_prefix'] = 'transfer'
    print('=> Initializing BBox with defense {} and arguments: {}'.format(defense_type, defense_kwargs))
    blackbox = BB.from_modeldir(blackbox_dir, device, **defense_kwargs)
    quantize_blackbox = None
    if params['quantize']:
        quantize_kwargs = knockoff_utils.parse_defense_kwargs(params['quantize_args'])
        if quantize_kwargs['epsilon'] > 0.0:
            print('=> Initializing Quantizer with arguments: {}'.format(quantize_kwargs))
            quantize_blackbox = incremental_kmeans(blackbox,**quantize_kwargs)
    
    if params['defense_aware']:
        recover = Table_Recover(blackbox,table_size=params['recover_table_size'],batch_size=params['batch_size'],recover_mean=True,recover_norm=params['recover_norm'],
                                tolerance=params['recover_tolerance'],estimation_set=params['dir_mle_path'],num_proc=params['recover_proc'])
    else:
        recover = None

    for k, v in defense_kwargs.items():
        params[k] = v

    # ----------- Initialize adversary
    batch_size = params['batch_size']
    nworkers = params['nworkers']
    if params['policy'] == 'random':
        adversary = RandomAdversaryIters(quantize_blackbox if quantize_blackbox is not None else blackbox, recover, queryset, batch_size=batch_size)
    elif params['policy'] == 'adaptive':
        raise NotImplementedError()
    else:
        raise ValueError("Unrecognized policy")

    print('=> constructing transfer set...')
    transferset = adversary.get_transferset(params['budget'], params['nqueries'], queries_per_image=params['qpi'])
    transfer_out_path = osp.join(out_path, 'transferset.pickle')
    with open(transfer_out_path, 'wb') as wf:
        pickle.dump(transferset, wf)
    print('=> transfer set ({} samples) written to: {}'.format(len(transferset), transfer_out_path))

    # Store run times
    rt_mean, rt_std = np.mean(adversary.call_times), np.std(adversary.call_times)
    rt_dict = {'batch_size': batch_size, 'mean': rt_mean, 'std': rt_std}
    rt_out_path = osp.join(out_path, 'run_times_summary.json')
    with open(rt_out_path, 'w') as wf:
        json.dump(rt_dict, wf, indent=True)
    rt_out_path = osp.join(out_path, 'run_times.pickle')
    with open(rt_out_path, 'wb') as wf:
        pickle.dump(adversary.call_times, wf)
    print('=> run time (Batch size = {}): mean = {}\t std = {}'.format(batch_size, rt_mean, rt_std))

    # Store arguments
    params['created_on'] = str(datetime.now())
    params_out_path = osp.join(out_path, 'params_transfer.json')
    with open(params_out_path, 'w') as jf:
        json.dump(params, jf, indent=True)


if __name__ == '__main__':
    main()
